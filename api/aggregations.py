"""C6 aggregations: the five shapes the question set demands — no more.

Every shape runs over the SAME base set: records whose pattern was
semantically selected (retrieval.PatternStore) AND pass the geo/time
pre-filters. That base filter is what makes "aggregate over the topic"
well-defined at 766K-record scale.

## Q1's drill-down is TWO-STAGE — do not flatten it

Stage 1 (distribution) returns buckets only, each carrying an `expand`
hint. Stage 2 is a SECOND request with expand params, returning the actual
records behind one bucket. This group-then-expand contract was forced by
Q1 ("category distribution → drill down to records", SPEC C5 table) — the
UI (C8) renders buckets, the user clicks one, and only then do records
load. A flat "buckets + records in one response" would break that
interaction and re-couple aggregation size to record payload size.
"""

from retrieval import filters_from

MONTH_FMT = "yyyy-MM"


def _base_filters(parsed, patterns):
    return filters_from(parsed) + [
        {"terms": {"full_text.raw": [p["pattern"] for p in patterns]}}
    ]


def _expand_hint(agg_type, group_value):
    """What the client sends back to drill into this bucket (stage 2)."""
    return {"expand_group": group_value, "agg_type": agg_type}


def expand_records(es, index, parsed, patterns, expand, extra_term=None):
    """Stage 2 of drill-down: the records behind one bucket."""
    filters = _base_filters(parsed, patterns)
    filters.append({"term": {"community_board": expand.group_value}})
    if extra_term:  # e.g. Q1 drill narrowed to one category
        filters.append({"term": extra_term})
    res = es.search(
        index=index, track_total_hits=True,
        query={"bool": {"filter": filters}},
        sort=[{"created_date": "desc"}],
        size=expand.size,
        source=["unique_key", "created_date", "complaint_type", "descriptor",
                "borough", "community_board", "agency", "problem_domain",
                "agencies_involved", "status"],
    )
    return {
        "type": "records",
        "group_value": expand.group_value,
        "total": res["hits"]["total"]["value"],
        "records": [h["_source"] for h in res["hits"]["hits"]],
    }


def distribution(es, index, parsed, patterns):
    """Q1: WHERE is it happening — districts ranked, categories within."""
    res = es.search(
        index=index, size=0, track_total_hits=True,
        query={"bool": {"filter": _base_filters(parsed, patterns)}},
        aggs={"by_district": {
            "terms": {"field": "community_board", "size": 30},
            "aggs": {"by_category": {
                "terms": {"field": "complaint_type.raw", "size": 8}}},
        }},
    )
    buckets = [
        {
            "district": b["key"],
            "count": b["doc_count"],
            "categories": [
                {"category": c["key"], "count": c["doc_count"]}
                for c in b["by_category"]["buckets"]
            ],
            # stage-2 pointer — records load only when this is followed
            "expand": _expand_hint("distribution", b["key"]),
        }
        for b in res["aggregations"]["by_district"]["buckets"]
    ]
    return {"type": "distribution", "group_by": "community_board",
            "total": res["hits"]["total"]["value"], "buckets": buckets}


def agency_facets(es, index, parsed, patterns, pattern_store):
    """Q2: which agencies does this problem actually implicate (killer feature)."""
    res = es.search(
        index=index, size=0, track_total_hits=True,
        query={"bool": {"filter": _base_filters(parsed, patterns)}},
        aggs={
            "by_agency_facet": {"terms": {"field": "agencies_involved", "size": 7}},
            "by_routed_agency": {"terms": {"field": "agency", "size": 7}},
        },
    )
    total = res["hits"]["total"]["value"]
    # Multi-agency share computed from pattern metadata (C2 facets).
    multi_patterns = [p for p in patterns
                      if len(pattern_store.agencies.get(p["pattern"], [])) >= 2]
    return {
        "type": "agency_facets",
        "total": total,
        # The before/after picture: who 311 routed it to vs who the problem touches.
        "routed_agency": [
            {"agency": b["key"], "count": b["doc_count"]}
            for b in res["aggregations"]["by_routed_agency"]["buckets"]
        ],
        "involved_agencies": [
            {"agency": b["key"], "count": b["doc_count"]}
            for b in res["aggregations"]["by_agency_facet"]["buckets"]
        ],
        "patterns": [
            {"pattern": p["pattern"],
             "agencies": pattern_store.agencies.get(p["pattern"], [])}
            for p in patterns[:10]
        ],
        "multi_agency_pattern_count": len(multi_patterns),
    }


def trend(es, index, parsed, patterns):
    """Q3: growth by district — monthly series, first-half vs second-half."""
    res = es.search(
        index=index, size=0, track_total_hits=True,
        query={"bool": {"filter": _base_filters(parsed, patterns)}},
        aggs={"by_district": {
            "terms": {"field": "community_board", "size": 60, "min_doc_count": 30},
            "aggs": {"monthly": {"date_histogram": {
                "field": "created_date", "calendar_interval": "month",
                "format": MONTH_FMT}}},
        }},
    )
    ranked = []
    for b in res["aggregations"]["by_district"]["buckets"]:
        series = [(m["key_as_string"], m["doc_count"]) for m in b["monthly"]["buckets"]]
        if len(series) < 4:
            continue  # not enough months to call a trend
        half = len(series) // 2
        h1 = sum(c for _, c in series[:half])
        h2 = sum(c for _, c in series[half:])
        ranked.append({
            "district": b["key"], "count": b["doc_count"],
            "first_half": h1, "second_half": h2,
            "growth": round((h2 - h1) / max(h1, 1), 3),
            "monthly": [{"month": m, "count": c} for m, c in series],
            "expand": _expand_hint("trend", b["key"]),
        })
    ranked.sort(key=lambda r: -r["growth"])
    return {"type": "trend", "group_by": "community_board",
            "metric": "second_half_vs_first_half_growth", "buckets": ranked[:15]}


def topn(es, index, parsed, patterns):
    """Q4: top-N districts by volume."""
    n = (parsed.aggregation.top_n or 10) if parsed.aggregation else 10
    res = es.search(
        index=index, size=0, track_total_hits=True,
        query={"bool": {"filter": _base_filters(parsed, patterns)}},
        aggs={"by_district": {"terms": {"field": "community_board", "size": n}}},
    )
    return {
        "type": "topn", "group_by": "community_board", "n": n,
        "total": res["hits"]["total"]["value"],
        "buckets": [
            {"rank": i + 1, "district": b["key"], "count": b["doc_count"],
             "expand": _expand_hint("topn", b["key"])}
            for i, b in enumerate(res["aggregations"]["by_district"]["buckets"])
        ],
    }


def cooccurrence(es, index, parsed, patterns):
    """Q5: what else happens where/when the topic happens.

    Two-step spatial-temporal join: (1) find the (district x month) cells
    where topic records concentrate; (2) inside exactly those cells, count
    NON-topic patterns and compare their share against the citywide
    baseline (lift). Lift > 1 = over-represented alongside the topic.
    """
    topic_terms = {"terms": {"full_text.raw": [p["pattern"] for p in patterns]}}
    base = filters_from(parsed)

    # Step 1: where/when does the topic cluster?
    cells_res = es.search(
        index=index, size=0,
        query={"bool": {"filter": base + [topic_terms]}},
        aggs={"cells": {
            "composite": {"size": 100, "sources": [
                {"cb": {"terms": {"field": "community_board"}}},
                {"month": {"date_histogram": {
                    "field": "created_date", "calendar_interval": "month",
                    "format": MONTH_FMT}}},
            ]},
        }},
    )
    cells = [c for c in cells_res["aggregations"]["cells"]["buckets"]
             if c["doc_count"] >= 5]
    cells.sort(key=lambda c: -c["doc_count"])
    cells = cells[:100]
    if not cells:
        return {"type": "cooccurrence", "buckets": [], "cells_used": 0}

    cell_clauses = [
        {"bool": {"filter": [
            {"term": {"community_board": c["key"]["cb"]}},
            {"range": {"created_date": {
                "gte": c["key"]["month"], "lt": c["key"]["month"] + "||+1M",
                "format": MONTH_FMT}}},
        ]}}
        for c in cells
    ]

    # Step 2: non-topic problems inside those cells vs citywide baseline.
    in_cells = {
        "query": {"bool": {
            "filter": base + [{"bool": {"should": cell_clauses, "minimum_should_match": 1}}],
            "must_not": [topic_terms],
        }},
        "size": 0, "track_total_hits": True,
        "aggs": {"patterns": {"terms": {"field": "full_text.raw", "size": 15}},
                 "domains": {"terms": {"field": "problem_domain", "size": 6}}},
    }
    citywide = {
        "query": {"bool": {"must_not": [topic_terms]}},
        "size": 0, "track_total_hits": True,
        "aggs": {"patterns": {"terms": {"field": "full_text.raw", "size": 300}}},
    }
    resp = es.msearch(searches=[{"index": index}, in_cells,
                                {"index": index}, citywide])
    cell_aggs, city_aggs = resp["responses"][0], resp["responses"][1]

    cell_total = cell_aggs["hits"]["total"]["value"]
    city_total = city_aggs["hits"]["total"]["value"]
    city_share = {b["key"]: b["doc_count"] / city_total
                  for b in city_aggs["aggregations"]["patterns"]["buckets"]}

    buckets = []
    for b in cell_aggs["aggregations"]["patterns"]["buckets"]:
        share = b["doc_count"] / max(cell_total, 1)
        baseline = city_share.get(b["key"])
        buckets.append({
            "pattern": b["key"], "count": b["doc_count"],
            "share_in_cells": round(share, 4),
            "lift_vs_citywide": round(share / baseline, 2) if baseline else None,
        })
    # "Tends to co-occur" is a lift question, not a popularity question —
    # raw counts just re-rank citywide volume. Lift-less rows sink.
    buckets.sort(key=lambda b: -(b["lift_vs_citywide"] or 0))
    return {
        "type": "cooccurrence", "cells_used": len(cells),
        "cell_definition": "community_board x calendar month, >=5 topic records",
        "buckets": buckets,
        "domains_in_cells": [
            {"domain": b["key"], "count": b["doc_count"]}
            for b in cell_aggs["aggregations"]["domains"]["buckets"]
        ],
    }


DISPATCH = {
    "distribution": distribution,
    "trend": trend,
    "topn": topn,
    "cooccurrence": cooccurrence,
}


def run_aggregation(es, index, parsed, patterns, pattern_store):
    agg = parsed.aggregation
    if agg.expand is not None:
        return expand_records(es, index, parsed, patterns, agg.expand)
    if agg.type == "agency_facets":
        return agency_facets(es, index, parsed, patterns, pattern_store)
    return DISPATCH[agg.type](es, index, parsed, patterns)
