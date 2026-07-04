"""C2 enrichment: map 337 distinct combos onto the ontology via Claude.

Design constraints from Docs/ontology-v0.1.md (locked):
  * Operates on distinct combos only — never per-record calls (rule 1).
  * Controlled vocabulary enforced twice: JSON-schema-constrained output
    (the API guarantees enum membership) plus Python-side cross-field
    checks (failure_mode/domain consistency, non-empty agencies). The
    combo_facets CHECK constraints are a third net.
  * Parse/validation failure → one retry with the error appended; a second
    failure writes a needs_review row for Archy instead of a silent guess.
  * Resumable: reruns skip combos already in combo_facets (per-row commit).

Cost: ~337 calls to claude-opus-4-8, ~$5 one-time — well under the $20
budget. Runs with 4 concurrent workers, ~10 minutes wall clock.

Usage:
    python enrich.py [--limit N] [--retry-failed]
"""

import argparse
import concurrent.futures
import json
import os
import sys
import threading

import anthropic
import psycopg

MODEL = "claude-opus-4-8"
PROMPT_VERSION = "v0.1"
PROMPT_PATH = os.path.join(os.path.dirname(__file__), f"prompt_{PROMPT_VERSION}.md")

DOMAINS = ["drainage", "water_supply", "sanitation", "street_infrastructure", "other"]
FAILURE_MODES = ["blockage", "overflow", "structural_damage", "odor", "debris", "service_gap"]
AGENCIES = ["DSNY", "DEP", "DOT", "HPD", "NYPD", "DOHMH", "DPR"]

# Schema-constrained output: the API guarantees enum membership and shape,
# so Python-side validation only needs the cross-field rules the schema
# can't express (failure_mode presence vs domain, agency de-duplication).
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "problem_domain": {"type": "string", "enum": DOMAINS},
        "failure_mode": {
            "anyOf": [{"type": "null"}, {"type": "string", "enum": FAILURE_MODES}]
        },
        "agencies_involved": {
            "type": "array",
            "items": {"type": "string", "enum": AGENCIES},
        },
        "rationale": {"type": "string"},
    },
    "required": ["problem_domain", "failure_mode", "agencies_involved", "rationale"],
    "additionalProperties": False,
}

EMBED_TEXT_SQL = """
concat_ws('. ',
    nullif(trim(complaint_type), ''),
    nullif(trim(descriptor), ''),
    nullif(trim(additional_details), '')
)
""".strip()

print_lock = threading.Lock()


def get_conn():
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "semanticlens"),
        user=os.environ.get("POSTGRES_USER", "semanticlens"),
        password=os.environ.get("POSTGRES_PASSWORD", "changeme"),
    )


def load_pending_combos(conn, retry_failed, limit):
    with conn.cursor() as cur:
        if retry_failed:
            cur.execute("DELETE FROM combo_facets WHERE needs_review")
            conn.commit()
        rows = cur.execute(
            f"""
            SELECT
                {EMBED_TEXT_SQL} AS embed_text,
                complaint_type, descriptor, additional_details,
                count(*) AS record_count,
                string_agg(DISTINCT agency, ', ' ORDER BY agency) AS raw_agencies
            FROM raw_311_requests
            GROUP BY 1, 2, 3, 4
            HAVING {EMBED_TEXT_SQL} NOT IN (SELECT embed_text FROM combo_facets)
            ORDER BY count(*) DESC
            {f"LIMIT {int(limit)}" if limit else ""}
            """
        ).fetchall()
    return rows


def cross_field_errors(result):
    errors = []
    domain = result["problem_domain"]
    mode = result["failure_mode"]
    agencies = result["agencies_involved"]
    if domain in ("drainage", "sanitation") and mode is None:
        errors.append("failure_mode is required when problem_domain is drainage or sanitation")
    if domain not in ("drainage", "sanitation") and mode is not None:
        errors.append(f"failure_mode must be null for domain '{domain}' (ontology facet 2)")
    if not agencies:
        errors.append("agencies_involved must contain at least one agency")
    return errors


def classify_combo(client, system_prompt, combo):
    """One combo -> validated facet dict, or raises after the single retry."""
    embed_text, ctype, desc, details, record_count, raw_agencies = combo
    user_msg = (
        f"complaint_type: {ctype}\n"
        f"descriptor: {desc}\n"
        f"additional_details: {details or '(none)'}\n"
        f"raw 311 assigned agency: {raw_agencies}\n"
        f"records with this pattern in 2025: {record_count}"
    )

    messages = [{"role": "user", "content": user_msg}]
    last_errors = None
    for attempt in range(2):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=system_prompt,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            raise ValueError("model refused the request")
        text = next(b.text for b in response.content if b.type == "text")
        result = json.loads(text)
        result["agencies_involved"] = sorted(set(result["agencies_involved"]))
        errors = cross_field_errors(result)
        if not errors:
            result["usage"] = response.usage
            return result
        last_errors = errors
        messages = messages + [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": "Your previous answer violated these ontology rules: "
                + "; ".join(errors)
                + ". Produce a corrected classification.",
            },
        ]
    raise ValueError("validation failed after retry: " + "; ".join(last_errors))


def save_result(conn, embed_text, result):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO combo_facets
                (embed_text, problem_domain, failure_mode, agencies_involved,
                 rationale, model, prompt_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (embed_text) DO NOTHING
            """,
            (
                embed_text,
                result["problem_domain"],
                result["failure_mode"],
                result["agencies_involved"],
                result["rationale"],
                MODEL,
                PROMPT_VERSION,
            ),
        )
    conn.commit()


def save_needs_review(conn, embed_text, reason):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO combo_facets
                (embed_text, model, prompt_version, needs_review, review_reason)
            VALUES (%s, %s, %s, true, %s)
            ON CONFLICT (embed_text) DO NOTHING
            """,
            (embed_text, MODEL, PROMPT_VERSION, reason),
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Process only the first N pending combos")
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Delete needs_review rows first so they are re-attempted",
    )
    args = parser.parse_args()

    with open(PROMPT_PATH, encoding="utf-8") as f:
        system_prompt = f.read()

    client = anthropic.Anthropic()
    conn = get_conn()
    combos = load_pending_combos(conn, args.retry_failed, args.limit)
    if not combos:
        print("nothing to do — all combos already enriched")
        return
    print(f"enriching {len(combos)} combos with {MODEL} (prompt {PROMPT_VERSION})")

    total_in = total_out = done = flagged = 0
    # Writes go through this single connection from the main thread; workers
    # only make API calls, so no per-thread connections are needed.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(classify_combo, client, system_prompt, combo): combo
            for combo in combos
        }
        for future in concurrent.futures.as_completed(futures):
            combo = futures[future]
            embed_text = combo[0]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — any failure goes to review, per ontology note 4
                flagged += 1
                save_needs_review(conn, embed_text, str(exc))
                with print_lock:
                    print(f"  NEEDS REVIEW: {embed_text!r} — {exc}")
                continue
            usage = result.pop("usage")
            total_in += usage.input_tokens
            total_out += usage.output_tokens
            save_result(conn, embed_text, result)
            done += 1
            if done % 25 == 0:
                with print_lock:
                    print(f"  {done}/{len(combos)} done")

    # Opus 4.8: $5/M input, $25/M output.
    cost = total_in * 5 / 1e6 + total_out * 25 / 1e6
    print(
        f"finished: {done} classified, {flagged} flagged for review; "
        f"{total_in} in / {total_out} out tokens ≈ ${cost:.2f}"
    )
    if flagged:
        print("review queue: SELECT embed_text, review_reason FROM combo_facets WHERE needs_review;")
        sys.exit(1)


if __name__ == "__main__":
    main()
