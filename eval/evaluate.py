"""C7 evaluation harness — one command, versioned report.

Two measurements, per SPEC C7:

1. **recall@10 (ES approximate vs FAISS exact)** — label-free, runs always.
   FAISS IndexFlatIP over the 337 distinct pattern vectors is the exact
   ground truth; ES searches the 766K duplicated vectors with int8-HNSW
   and collapses to patterns. Measured at the PATTERN level because
   record-level top-10 is tie-breaking noise: thousands of records share
   identical vectors, so exact and approximate top-10 records differ
   arbitrarily even at perfect vector recall. Pattern granularity is also
   what serving actually consumes.

2. **precision@10 (serving hybrid vs human golden labels)** — runs when
   eval/golden_v0.csv exists (Archy's labels over the pooled candidates;
   facets are never used as a relevance proxy). Unlabeled patterns in a
   top-10 count as not-relevant and are reported, so gaps in pooling
   surface instead of inflating the score.

FAISS lives ONLY in this container — never in the serving path (SPEC
global boundary 5).

Usage: python evaluate.py   (writes reports/eval_<utc-timestamp>.md + .json)
"""

import csv
import datetime
import json
import os

import faiss
import httpx
import numpy as np
import psycopg
from elasticsearch import Elasticsearch

from embed_util import embed_query
from shared.semantic_contract import DIM, MODEL_NAME

API_URL = os.environ.get("API_URL", "http://localhost:8000")
HERE = os.path.dirname(__file__)
QUERIES_CSV = os.path.join(HERE, "queries_v0.csv")
GOLDEN_CSV = os.path.join(HERE, "golden_v0.csv")
REPORT_DIR = os.path.join(HERE, "reports")

K = 10
RECALL_GATE = 0.95   # SPEC §5 (v1.1 target; v0 records the baseline)
PRECISION_GATE = 0.8


def load_queries():
    with open(QUERIES_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_pattern_vectors():
    conn = psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "semanticlens"),
        user=os.environ.get("POSTGRES_USER", "semanticlens"),
        password=os.environ.get("POSTGRES_PASSWORD", "changeme"),
    )
    rows = conn.execute(
        "SELECT embed_text, embedding FROM embedding_text_cache WHERE model = %s",
        (MODEL_NAME,),
    ).fetchall()
    conn.close()
    texts = [r[0] for r in rows]
    matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    return texts, matrix


def recall_eval(queries):
    """ES int8-HNSW pattern top-10 vs FAISS exact pattern top-10."""
    texts, matrix = load_pattern_vectors()
    index = faiss.IndexFlatIP(DIM)     # brute-force exact inner product
    index.add(matrix)                  # unit vectors -> IP == cosine
    es = Elasticsearch(os.environ.get("ES_HOST", "http://localhost:9200"))

    per_query = []
    for q in queries:
        vec = np.asarray([embed_query(q["query"])], dtype=np.float32)
        _, exact_idx = index.search(vec, K)
        exact = {texts[i] for i in exact_idx[0]}

        # The serving kNN leg: pattern index (_id == embed_text).
        res = es.search(
            index="nyc311_patterns",
            knn={"field": "embedding", "query_vector": vec[0].tolist(),
                 "k": K, "num_candidates": 337},
            size=K, _source=False,
        )
        approx = {h["_id"] for h in res["hits"]["hits"]}
        per_query.append({"query_id": q["query_id"], "query": q["query"],
                          "recall_at_10": len(exact & approx) / K})

    mean = sum(r["recall_at_10"] for r in per_query) / len(per_query)
    return {"mean": round(mean, 4),
            "min": min(r["recall_at_10"] for r in per_query),
            "gate": RECALL_GATE, "passes_gate": mean >= RECALL_GATE,
            "per_query": per_query}


def precision_eval(queries):
    """Serving hybrid top-10 vs the human golden labels. None if unlabeled."""
    if not os.path.exists(GOLDEN_CSV):
        return None
    labels = {}
    with open(GOLDEN_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["label"].strip() in ("0", "1"):
                labels[(row["query_id"], row["pattern"])] = int(row["label"])

    per_query, total_unjudged = [], 0
    for q in queries:
        r = httpx.get(f"{API_URL}/search",
                      params={"q": q["query"], "size": K, "explain": "true"},
                      timeout=60)
        r.raise_for_status()
        served = [row["pattern"] for row in
                  r.json()["explain"]["rrf"]["per_hit_leg_ranks"]][:K]
        relevant = sum(labels.get((q["query_id"], p), 0) for p in served)
        unjudged = sum(1 for p in served if (q["query_id"], p) not in labels)
        total_unjudged += unjudged
        per_query.append({"query_id": q["query_id"], "query": q["query"],
                          "precision_at_10": relevant / K,
                          "served": len(served), "unjudged": unjudged})

    mean = sum(r["precision_at_10"] for r in per_query) / len(per_query)
    return {"mean": round(mean, 4), "gate": PRECISION_GATE,
            "passes_gate": mean >= PRECISION_GATE,
            "labeled_pairs": len(labels), "unjudged_served": total_unjudged,
            "per_query": per_query}


def write_report(recall, precision):
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {"timestamp": stamp, "model": MODEL_NAME, "k": K,
               "recall_vs_faiss_exact": recall,
               "precision_vs_golden": precision}
    with open(os.path.join(REPORT_DIR, f"eval_{stamp}.json"), "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    lines = [
        f"# Evaluation report — {stamp}",
        "",
        f"Model: {MODEL_NAME} · k={K} · granularity: pattern "
        "(record-level is tie-noise; see evaluate.py docstring)",
        "",
        "## recall@10 — ES int8-HNSW vs FAISS IndexFlatIP exact",
        "",
        f"**mean {recall['mean']:.4f}** (min {recall['min']:.2f}) — "
        f"gate {recall['gate']}: {'PASS' if recall['passes_gate'] else 'MISS'}",
        "",
    ]
    misses = [r for r in recall["per_query"] if r["recall_at_10"] < 1.0]
    if misses:
        lines.append("Queries below 1.0:")
        lines += [f"- {r['query_id']} ({r['query']}): {r['recall_at_10']:.2f}"
                  for r in misses]
    else:
        lines.append("All queries at 1.00.")
    lines += ["", "## precision@10 — serving hybrid vs golden labels", ""]
    if precision is None:
        lines.append("PENDING — eval/golden_v0.csv not present yet "
                     "(labeling sheet with Archy).")
    else:
        lines.append(
            f"**mean {precision['mean']:.4f}** — gate {precision['gate']}: "
            f"{'PASS' if precision['passes_gate'] else 'MISS'} · "
            f"{precision['labeled_pairs']} labeled pairs · "
            f"{precision['unjudged_served']} served-but-unjudged (counted 0)")
        lines.append("")
        lines += [f"- {r['query_id']} ({r['query']}): {r['precision_at_10']:.1f}"
                  for r in precision["per_query"]]
    path = os.path.join(REPORT_DIR, f"eval_{stamp}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def main():
    queries = load_queries()
    print(f"{len(queries)} queries loaded")
    recall = recall_eval(queries)
    print(f"recall@10 vs FAISS exact: mean={recall['mean']} min={recall['min']}")
    precision = precision_eval(queries)
    if precision:
        print(f"precision@10 vs golden: mean={precision['mean']}")
    else:
        print("precision@10: pending golden labels")
    path = write_report(recall, precision)
    print(f"report: {path}")


if __name__ == "__main__":
    main()
