"""C6 acceptance tests — integration, needs live ES + Postgres.

Run inside the api container: docker compose run --rm api pytest test_c6.py -v -s

Covers, in SPEC C6's own terms:
  1. Semantic path: a query with no category words retrieves Sewer /
     Catch Basin / Street Flooding records.
  2. Exact-term path: "catch basin clogged" recalls via BM25.
  3. Regression: "stormwater not draining" must NOT surface "No Water"
     (kNN-only failure mode caught during C5; RRF+BM25 is the fix).
  4. The five chief-DS questions produce their aggregation shapes —
     including Q1's TWO-STAGE drill-down (buckets stage, then expand).
  5. End-to-end p95 latency <= 2s.
"""

import os
import statistics
import time

import pytest
from elasticsearch import Elasticsearch
from fastapi.testclient import TestClient

import main
from main import app

es = Elasticsearch(os.environ.get("ES_HOST", "http://localhost:9200"))
pytestmark = pytest.mark.skipif(not es.ping(), reason="needs live ES")

# Lifespan (model + pattern store) only runs inside the context manager.
_client_cm = TestClient(app)
_client = None


def setup_module():
    global _client
    _client = _client_cm.__enter__()


def teardown_module():
    _client_cm.__exit__(None, None, None)


def results(q, **params):
    r = _client.get("/search", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


# --- 1. semantic path --------------------------------------------------------

def test_semantic_no_category_words():
    body = results("stormwater drainage problems")
    descriptors = " | ".join(
        f"{h['complaint_type']} {h['descriptor']}" for h in body["results"]
    ).lower()
    assert any(term in descriptors for term in
               ("catch basin", "street flooding", "sewer")), descriptors
    domains = [h["problem_domain"] for h in body["results"]]
    assert domains.count("drainage") >= 7, domains


# --- 2. exact-term path ------------------------------------------------------

def test_exact_term_bm25():
    body = results("catch basin clogged")
    top = body["results"][0]
    assert "Catch Basin Clogged" in top["descriptor"], top


# --- 3. No Water regression --------------------------------------------------

def test_no_water_regression():
    body = results("stormwater not draining", explain="true")
    top3 = body["results"][:3]
    assert all(h["problem_domain"] == "drainage" for h in top3), top3
    assert not any("No Water" in h["descriptor"] for h in top3), top3


# --- 4. the five questions, literally ---------------------------------------

def test_q1_distribution_two_stage_drilldown():
    q = "Where in Brooklyn is stormwater not draining?"
    body = results(q, explain="true")
    agg = body["aggregation"]
    assert agg["type"] == "distribution"
    assert agg["buckets"], "expected district buckets"
    first = agg["buckets"][0]
    # Stage 1 is buckets with categories + an expand HINT — no records inline.
    assert "categories" in first and "expand" in first
    assert "records" not in first, "drill-down must be two-stage, not flattened"
    assert first["district"].endswith("BROOKLYN"), first["district"]

    # Stage 2: follow the hint; NOW records arrive, all from that district.
    stage2 = results(q, expand_group=first["expand"]["expand_group"])
    agg2 = stage2["aggregation"]
    assert agg2["type"] == "records"
    assert agg2["records"], "expand returned no records"
    assert all(r["community_board"] == first["district"] for r in agg2["records"])


def test_q2_agency_facets():
    body = results("Which agencies does a single catch-basin complaint actually involve?")
    agg = body["aggregation"]
    assert agg["type"] == "agency_facets"
    involved = {b["agency"] for b in agg["involved_agencies"]}
    # The killer feature: the canonical catch-basin mapping surfaces all three.
    assert {"DEP", "DSNY", "DOT"} <= involved, involved
    assert agg["multi_agency_pattern_count"] >= 1
    routed = {b["agency"] for b in agg["routed_agency"]}
    assert involved - routed or len(involved) > len(routed) or involved != routed


def test_q3_trend():
    body = results("Which district's drainage complaints grew fastest last year?")
    agg = body["aggregation"]
    assert agg["type"] == "trend"
    assert agg["buckets"], "expected ranked districts"
    top = agg["buckets"][0]
    assert "growth" in top and "monthly" in top
    growths = [b["growth"] for b in agg["buckets"]]
    assert growths == sorted(growths, reverse=True), "must be ranked by growth"


def test_q4_topn():
    body = results("Top 10 districts for catch-basin clogging citywide?")
    agg = body["aggregation"]
    assert agg["type"] == "topn"
    assert agg["n"] == 10
    assert 1 <= len(agg["buckets"]) <= 10
    counts = [b["count"] for b in agg["buckets"]]
    assert counts == sorted(counts, reverse=True)


def test_q5_cooccurrence():
    body = results("What problems tend to co-occur with catch-basin clogging?", explain="true")
    agg = body["aggregation"]
    assert agg["type"] == "cooccurrence"
    assert agg["cells_used"] > 0
    assert agg["buckets"], "expected co-occurring patterns"
    # The topic's own patterns must be excluded from co-occurrence results.
    topic_patterns = {p["pattern"] for p in body["explain"]["selected_patterns"]}
    for b in agg["buckets"]:
        assert b["pattern"] not in topic_patterns, f"topic pattern leaked: {b['pattern']}"


# --- 5. latency --------------------------------------------------------------

def test_p95_latency_under_2s():
    queries = [
        "Where in Brooklyn is stormwater not draining?",
        "Which agencies does a single catch-basin complaint actually involve?",
        "Which district's drainage complaints grew fastest last year?",
        "Top 10 districts for catch-basin clogging citywide?",
        "What problems tend to co-occur with catch-basin clogging?",
    ] * 4
    times = []
    for q in queries:
        start = time.perf_counter()
        results(q)
        times.append(time.perf_counter() - start)
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    print(f"\nlatency p50={statistics.median(times)*1000:.0f}ms "
          f"p95={p95*1000:.0f}ms over {len(times)} requests")
    assert p95 <= 2.0, f"p95 {p95:.2f}s exceeds 2s budget"
