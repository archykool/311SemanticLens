"""Prefix smoke test (Archy's C5 constraint 3).

Runs ONE query embedded two ways — with and without BGE_QUERY_PREFIX — and
prints both top-10s side by side. Documents were embedded without the
prefix; bge-small was trained so that prefixed queries retrieve unprefixed
passages. This makes the difference observable so we know the prefix is
actually live in the serving path.

Run: docker compose run --rm api python smoke_prefix.py ["your query"]
"""

import os
import sys

from elasticsearch import Elasticsearch

from embedder import embed_query

QUERY = sys.argv[1] if len(sys.argv) > 1 else "stormwater drainage problems"


def top10(es, vector):
    # Records sharing a (complaint_type, descriptor, details) pattern share a
    # vector, so a raw top-10 is 10 copies of one pattern. Collapse on the
    # pattern so the ranking DIFFERENCE between the two embeddings is visible.
    res = es.search(
        index="nyc311",
        knn={"field": "embedding", "query_vector": vector, "k": 400,
             "num_candidates": 800},
        collapse={"field": "descriptor.raw"},
        source=["complaint_type", "descriptor", "problem_domain"],
        size=10,
    )
    return [
        (round(h["_score"], 4),
         f"{h['_source']['complaint_type']} | {h['_source']['descriptor']}"
         f" [{h['_source']['problem_domain']}]")
        for h in res["hits"]["hits"]
    ]


def main():
    es = Elasticsearch(os.environ.get("ES_HOST", "http://localhost:9200"))
    print(f"query: {QUERY!r}\n")
    with_prefix = top10(es, embed_query(QUERY, with_prefix=True))
    without = top10(es, embed_query(QUERY, with_prefix=False))

    print(f"{'WITH prefix (serving path)':<70} | WITHOUT prefix")
    print("-" * 140)
    for (s1, d1), (s2, d2) in zip(with_prefix, without):
        print(f"{s1:.4f}  {d1:<60} | {s2:.4f}  {d2}")

    overlap = len({d for _, d in with_prefix} & {d for _, d in without})
    print(f"\npattern overlap in top-10: {overlap}/10")
    print("scores and/or ordering should differ — if both columns are byte-"
          "identical, the prefix is not being applied.")


if __name__ == "__main__":
    main()
