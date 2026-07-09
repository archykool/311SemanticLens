"""Minimal CLI proof of the C1-C6 system. No UI, no styling, ~40 lines out.

Proves: (1) one enriched index answers unrelated topics with zero per-topic
rework; (2) retrieval is semantic, not keyword; (3) the enrichment carries a
cross-agency signal the raw 311 taxonomy cannot express.

Run: docker compose run --rm api python demo/prove_it.py
Reuses the serving modules (parser/embedder/retrieval/aggregations) — no
parallel pipeline. All numbers come from the live index.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from elasticsearch import Elasticsearch

from aggregations import distribution
from embedder import embed_query
from parser import parse_query
from retrieval import PatternStore

INDEX = "nyc311"
es = Elasticsearch(os.environ.get("ES_HOST", "http://localhost:9200"))
store = PatternStore()


def short(pattern):
    """'Sewer. Catch Basin Clogged/... (SC). SC' -> 'Sewer / Catch Basin Clogged/...'"""
    parts = pattern.split(". ")
    return " / ".join(parts[:2])


def section1():
    print("SECTION 1 — Same system, any topic, zero rework")
    print("(no per-topic dictionaries: the same enriched index answers all three)")
    for q in ("trash-clogged catch basins", "noise at night",
              "rat sightings near restaurants"):
        parsed = parse_query(q)
        patterns = store.select(embed_query(parsed.topic), parsed.topic)
        print(f'\n  query: "{q}"')
        if not patterns:
            print("    no patterns above similarity threshold — "
                  "this corpus slice has no such complaints (honest empty)")
            continue
        names = list(dict.fromkeys(short(p["pattern"]) for p in patterns[:5]))
        print("    patterns: " + "; ".join(names[:3]))
        agg = distribution(es, INDEX, parsed, patterns)
        for i, b in enumerate(agg["buckets"][:3], 1):
            print(f"    {i}. {b['district']}  {b['count']:,} records")


def section2():
    q = "blocked drain"
    print(f'\nSECTION 2 — Semantic match, not keyword match  (query: "{q}")')
    patterns = store.select(embed_query(q), q)
    for p in patterns[:6]:
        text = p["pattern"].lower()
        has_words = [w for w in ("blocked", "drain") if w in text]
        flag = f"query words present: {','.join(has_words)}" if has_words \
            else "query words present: NONE -> semantic match"
        print(f"  sim {p['sim']:.2f}  {short(p['pattern']):<52} [{flag}]")


def section3():
    print("\nSECTION 3 — Cross-agency signal the old taxonomy can't show")
    catch_basin = next(t for t in store.texts if "Catch Basin Clogged" in t)
    res = es.search(index=INDEX, size=0, track_total_hits=True,
                    query={"term": {"full_text.raw": catch_basin}},
                    aggs={"routed": {"terms": {"field": "agency", "size": 3}}})
    routed = [b["key"] for b in res["aggregations"]["routed"]["buckets"]]
    n = res["hits"]["total"]["value"]
    print(f"  pattern: {short(catch_basin)}  ({n:,} records)")
    print(f"    311 routes it to:        {'+'.join(routed)}")
    print(f"    actually involves (C2):  {'+'.join(store.agencies[catch_basin])}")
    with psycopg.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            dbname=os.environ.get("POSTGRES_DB", "semanticlens"),
            user=os.environ.get("POSTGRES_USER", "semanticlens"),
            password=os.environ.get("POSTGRES_PASSWORD", "changeme")) as pg:
        multi, total = pg.execute(
            "SELECT count(*) FILTER (WHERE array_length(agencies_involved,1) >= 2),"
            "       count(*) FROM record_facets").fetchone()
    print(f"  headline: {multi:,} of {total:,} records ({multi / total:.1%}) "
          "carry a >=2-agency signal invisible to the single-label taxonomy")


if __name__ == "__main__":
    section1()
    section2()
    section3()
