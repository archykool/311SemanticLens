"""Build the pooled labeling sheet for the golden set (C7).

Pooling per query (sources hidden from the judge, per protocol):
  * BM25 top-15 patterns   (lexical system, queried directly)
  * kNN top-15 patterns    (semantic system, queried directly)
  * serving hybrid top-10  (the actual /search ranking under evaluation —
                            pooled so every served result gets judged)
  * 5 random patterns      (judge calibration; seeded for reproducibility)

Rows are deduped and shuffled per query. The source mapping is written to
a separate meta file for post-hoc analysis; it never appears in the sheet.
Facet columns are deliberately absent — facets are system output, not
relevance ground truth (C7 constraint).

Usage: python pool.py   (writes /data/golden_labeling_sheet_v0.csv + meta)
"""

import csv
import os
import random

import httpx
from elasticsearch import Elasticsearch

from embed_util import embed_query

API_URL = os.environ.get("API_URL", "http://localhost:8000")
OUT_DIR = os.environ.get("OUT_DIR", "/data")
QUERIES_CSV = os.path.join(os.path.dirname(__file__), "queries_v0.csv")

BM25_DEPTH = 15
KNN_DEPTH_PATTERNS = 15
RANDOM_N = 5
SEED = 311


def load_queries():
    with open(QUERIES_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def all_patterns(es):
    res = es.search(index="nyc311", size=0, aggs={
        "p": {"terms": {"field": "full_text.raw", "size": 500}}})
    return [b["key"] for b in res["aggregations"]["p"]["buckets"]]


def bm25_leg(es, query):
    res = es.search(
        index="nyc311",
        query={"multi_match": {"query": query,
                               "fields": ["full_text^2", "complaint_type",
                                          "descriptor", "additional_details"]}},
        collapse={"field": "full_text.raw"},
        size=BM25_DEPTH, _source=False, fields=["full_text.raw"],
    )
    return [h["fields"]["full_text.raw"][0] for h in res["hits"]["hits"]]


def knn_leg(es, vector):
    # Serving's semantic leg: the 337-doc pattern index (_id == embed_text).
    res = es.search(
        index="nyc311_patterns",
        knn={"field": "embedding", "query_vector": vector,
             "k": KNN_DEPTH_PATTERNS, "num_candidates": 337},
        size=KNN_DEPTH_PATTERNS, _source=False,
    )
    return [h["_id"] for h in res["hits"]["hits"]]


def hybrid_leg(query):
    r = httpx.get(f"{API_URL}/search",
                  params={"q": query, "size": 10, "explain": "true"},
                  timeout=60)
    r.raise_for_status()
    debug = r.json().get("explain", {}).get("rrf") or {}
    return [row["pattern"] for row in debug.get("per_hit_leg_ranks", [])]


def main():
    es = Elasticsearch(os.environ.get("ES_HOST", "http://localhost:9200"))
    rng = random.Random(SEED)
    universe = all_patterns(es)
    queries = load_queries()

    sheet_rows, meta_rows = [], []
    for q in queries:
        vector = embed_query(q["query"])
        sources = {}
        for pattern in bm25_leg(es, q["query"]):
            sources.setdefault(pattern, set()).add("bm25")
        for pattern in knn_leg(es, vector):
            sources.setdefault(pattern, set()).add("knn")
        for pattern in hybrid_leg(q["query"]):
            sources.setdefault(pattern, set()).add("hybrid")
        pool_so_far = set(sources)
        for pattern in rng.sample([p for p in universe if p not in pool_so_far],
                                  RANDOM_N):
            sources.setdefault(pattern, set()).add("random")

        candidates = list(sources)
        rng.shuffle(candidates)
        for pattern in candidates:
            sheet_rows.append({"query_id": q["query_id"], "query": q["query"],
                               "pattern": pattern, "label": ""})
            meta_rows.append({"query_id": q["query_id"], "pattern": pattern,
                              "sources": "+".join(sorted(sources[pattern]))})

    os.makedirs(OUT_DIR, exist_ok=True)
    sheet = os.path.join(OUT_DIR, "golden_labeling_sheet_v0.csv")
    with open(sheet, "w", newline="", encoding="utf-8-sig") as f:  # BOM for Excel
        w = csv.DictWriter(f, fieldnames=["query_id", "query", "pattern", "label"])
        w.writeheader()
        w.writerows(sheet_rows)
    meta = os.path.join(OUT_DIR, "golden_pool_meta_v0.csv")
    with open(meta, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["query_id", "pattern", "sources"])
        w.writeheader()
        w.writerows(meta_rows)

    per_q = len(sheet_rows) / len(queries)
    print(f"wrote {sheet}: {len(sheet_rows)} rows "
          f"({len(queries)} queries, avg {per_q:.1f} candidates each)")
    print(f"wrote {meta} (sources — do not open while labeling)")


if __name__ == "__main__":
    main()
