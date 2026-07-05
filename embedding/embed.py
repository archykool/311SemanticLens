"""C3 embedding pipeline: distinct texts -> bge-small-en-v1.5 -> Postgres.

Key design decisions (SPEC C3):

* Dedupe before encode. The 766K Phase 1 rows contain only ~337 distinct
  (complaint_type, descriptor, additional_details) texts, so the pipeline
  embeds distinct texts into embedding_text_cache and then fans vectors out
  to record_embeddings with one INSERT...SELECT join. Encoding cost is
  seconds on CPU instead of hours, and the join is pure SQL.

* The document text is built IN SQL (EMBED_TEXT_SQL below), not in Python.
  The same expression is used both when selecting texts to embed and when
  joining vectors back to records, so the two can never drift apart.
  !! C5 must reproduce this exact construction for query embedding — same
  model, same vector space (SPEC C3 boundary) — and should additionally
  prefix queries with BGE_QUERY_PREFIX (bge's recommended query-side prefix;
  documents are embedded WITHOUT it).

* Vectors are L2-normalized at encode time. Everything downstream assumes
  it: ES will use cosine/dot_product and C7's FAISS baseline is IndexFlatIP,
  which only equals cosine similarity on unit vectors.

* Resumable/idempotent: the cache table is the checkpoint. Each batch is
  committed as it finishes; a restarted run re-selects only texts with no
  cache row yet, and both inserts are ON CONFLICT DO NOTHING.

Usage:
    python embed.py [--batch-size 512]
"""

import argparse
import os

import numpy as np
import psycopg

# The model/prefix/text-construction contract shared with C5's query
# embedding lives in shared/semantic_contract.py — same vector space or
# kNN is meaningless.
from shared.semantic_contract import DIM, EMBED_TEXT_SQL, MODEL_NAME  # noqa: F401


def get_conn():
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "semanticlens"),
        user=os.environ.get("POSTGRES_USER", "semanticlens"),
        password=os.environ.get("POSTGRES_PASSWORD", "changeme"),
    )


def encode_missing_texts(conn, model, batch_size):
    """Embed every distinct document text that has no cache row yet."""
    with conn.cursor() as cur:
        rows = cur.execute(
            f"""
            SELECT DISTINCT {EMBED_TEXT_SQL} AS embed_text
            FROM raw_311_requests
            WHERE {EMBED_TEXT_SQL} <> ''
            EXCEPT
            SELECT embed_text FROM embedding_text_cache WHERE model = %s
            """,
            (MODEL_NAME,),
        ).fetchall()
    texts = [r[0] for r in rows]
    if not texts:
        print("cache already complete — nothing to encode")
        return 0

    print(f"encoding {len(texts)} distinct texts (batch size {batch_size})")
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors = model.encode(
            batch,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype(np.float32)
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO embedding_text_cache (model, embed_text, embedding, dim)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (model, embed_text) DO NOTHING
                """,
                [
                    (MODEL_NAME, text, vec.tobytes(), DIM)
                    for text, vec in zip(batch, vectors)
                ],
            )
        # Commit per batch: this IS the checkpoint. A killed run loses at
        # most one batch of encoding work.
        conn.commit()
        print(f"  cached {min(start + batch_size, len(texts))}/{len(texts)}")
    return len(texts)


def materialize_record_vectors(conn):
    """Fan cached vectors out to one row per record, entirely in SQL."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO record_embeddings (unique_key, model, embedding)
            SELECT r.unique_key, c.model, c.embedding
            FROM raw_311_requests r
            JOIN embedding_text_cache c
              ON c.model = %s
             AND c.embed_text = {EMBED_TEXT_SQL}
            ON CONFLICT (unique_key) DO NOTHING
            """,
            (MODEL_NAME,),
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


def verify(conn):
    """C3 acceptance #3: vector row count must exactly match record count."""
    with conn.cursor() as cur:
        records = cur.execute("SELECT count(*) FROM raw_311_requests").fetchone()[0]
        vectors = cur.execute("SELECT count(*) FROM record_embeddings").fetchone()[0]
        cached = cur.execute(
            "SELECT count(*) FROM embedding_text_cache WHERE model = %s",
            (MODEL_NAME,),
        ).fetchone()[0]
    print(f"records={records} vectors={vectors} distinct_texts_cached={cached}")
    if vectors != records:
        raise SystemExit(
            f"MISMATCH: {records} records but {vectors} vectors — "
            "likely records whose embed text is empty; investigate before C4."
        )
    print("OK: vector count exactly matches record count")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=512)
    args = parser.parse_args()

    # Imported late: sentence_transformers takes ~10s to import and pulls in
    # torch, so --help and connection failures stay fast.
    from sentence_transformers import SentenceTransformer

    print(f"loading {MODEL_NAME} (downloads to HF_HOME on first run)")
    model = SentenceTransformer(MODEL_NAME, device="cpu")

    with get_conn() as conn:
        encode_missing_texts(conn, model, args.batch_size)
        inserted = materialize_record_vectors(conn)
        print(f"materialized {inserted} new record vectors")
        verify(conn)


if __name__ == "__main__":
    main()
