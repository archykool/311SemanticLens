"""C5 acceptance tests.

Gate 1: the five canonical chief-DS questions parse perfectly (SPEC locked).
Gate 2: >= 80% of 20 paraphrased variants (EN + zh) parse correctly.
Gate 3: single-parse latency <= 500ms (measured over 1000 iterations).

"Parses correctly" = aggregation type, geo essentials, and time presence
match expectations. Topic wording is NOT asserted beyond a keyword — the
topic goes to embedding+BM25, which tolerates phrasing (that's the point
of the fuzzy/structured split).
"""

import time

from parser import parse_query


def check(q, agg_type, borough=None, scope=None, group_by=None,
          has_time=False, top_n=None, topic_contains=None):
    p = parse_query(q)
    errors = []
    got_agg = p.aggregation.type if p.aggregation else None
    if got_agg != agg_type:
        errors.append(f"agg {got_agg} != {agg_type}")
    if borough and p.geo.borough != borough:
        errors.append(f"borough {p.geo.borough} != {borough}")
    if scope and p.geo.scope != scope:
        errors.append(f"scope {p.geo.scope} != {scope}")
    if group_by and p.geo.group_by != group_by:
        errors.append(f"geo.group_by {p.geo.group_by} != {group_by}")
    if has_time and p.time_range is None:
        errors.append("expected time_range, got None")
    if top_n and (p.aggregation is None or p.aggregation.top_n != top_n):
        errors.append(f"top_n != {top_n}")
    if topic_contains and topic_contains.lower() not in p.topic.lower():
        errors.append(f"topic {p.topic!r} missing {topic_contains!r}")
    return errors, p


# --- Gate 1: the five locked questions (must be 5/5) ------------------------

CANONICAL = [
    ("Where in Brooklyn is stormwater not draining?",
     dict(agg_type="distribution", borough="BROOKLYN", group_by="community_board",
          topic_contains="stormwater")),
    ("Which agencies does a single catch-basin complaint actually involve?",
     dict(agg_type="agency_facets", topic_contains="catch-basin")),
    ("Which district's drainage complaints grew fastest last year?",
     dict(agg_type="trend", group_by="community_board", has_time=True,
          topic_contains="drainage")),
    ("Top 10 districts for catch-basin clogging citywide?",
     dict(agg_type="topn", scope="citywide", top_n=10, topic_contains="catch-basin")),
    ("What problems tend to co-occur with catch-basin clogging?",
     dict(agg_type="cooccurrence", topic_contains="catch-basin clogging")),
]

# --- Gate 2: 20 paraphrases, 4 per question (need >= 16 passing) ------------

PARAPHRASES = [
    # Q1 variants
    ("Which parts of Brooklyn have standing water after rain?",
     dict(agg_type="distribution", borough="BROOKLYN")),
    ("Brooklyn districts with poor stormwater drainage",
     dict(agg_type="distribution", borough="BROOKLYN")),
    ("布鲁克林哪里雨水排不出去", dict(agg_type="distribution", borough="BROOKLYN")),
    ("Where does rainwater pool in Brooklyn?",
     dict(agg_type="distribution", borough="BROOKLYN")),
    # Q2 variants
    ("Which departments does a clogged catch basin actually touch?",
     dict(agg_type="agency_facets")),
    ("Who handles a clogged catch basin?", dict(agg_type="agency_facets")),
    ("一个雨水口堵塞投诉实际涉及哪些部门", dict(agg_type="agency_facets")),
    ("What agencies are involved in catch basin complaints?",
     dict(agg_type="agency_facets")),
    # Q3 variants
    ("Which district saw drainage complaints grow fastest over the past year?",
     dict(agg_type="trend", has_time=True)),
    ("Fastest growing drainage complaint district, last 12 months",
     dict(agg_type="trend", has_time=True)),
    ("过去一年哪个区排水投诉增长最快", dict(agg_type="trend", has_time=True)),
    ("Drainage complaint trend by district since last year",
     dict(agg_type="trend", has_time=True)),
    # Q4 variants
    ("Top 10 districts for clogged catch basins",
     dict(agg_type="topn", top_n=10)),
    ("Rank districts by catch basin clogging citywide",
     dict(agg_type="topn", scope="citywide")),
    ("全市雨水口堵塞最多的前十个区", dict(agg_type="topn", top_n=10)),
    ("Which 10 districts have the most catch basin clogs?",
     dict(agg_type="topn", top_n=10)),
    # Q5 variants
    ("What issues show up together with catch basin clogging?",
     dict(agg_type="cooccurrence")),
    ("Problems that accompany clogged catch basins",
     dict(agg_type="cooccurrence")),
    ("雨水口堵塞通常伴随哪些问题", dict(agg_type="cooccurrence")),
    ("Which complaints co-occur with basin backups?",
     dict(agg_type="cooccurrence")),
]


def test_canonical_five():
    failures = []
    for q, expect in CANONICAL:
        errors, parsed = check(q, **expect)
        if errors:
            failures.append(f"{q!r}: {errors} (rules: {parsed.matched_rules})")
    assert not failures, "canonical questions must be 5/5:\n" + "\n".join(failures)


def test_paraphrases_80_percent():
    passed, report = 0, []
    for q, expect in PARAPHRASES:
        errors, parsed = check(q, **expect)
        if errors:
            report.append(f"FAIL {q!r}: {errors}")
        else:
            passed += 1
    rate = passed / len(PARAPHRASES)
    print(f"\nparaphrase accuracy: {passed}/{len(PARAPHRASES)} = {rate:.0%}")
    print("\n".join(report) if report else "all paraphrases passed")
    assert rate >= 0.8, f"below 80% gate:\n" + "\n".join(report)


def test_latency_under_500ms():
    start = time.perf_counter()
    n = 1000
    for i in range(n):
        parse_query(CANONICAL[i % 5][0])
    per_parse_ms = (time.perf_counter() - start) / n * 1000
    print(f"\nper-parse latency: {per_parse_ms:.3f}ms")
    assert per_parse_ms < 500


def test_unparseable_degrades_not_errors():
    # SPEC C5 boundary: junk input still yields a usable object (plain
    # retrieval), never an exception.
    p = parse_query("asdf qwerty zzz")
    assert p.aggregation is None
    assert p.topic  # non-empty topic still goes to hybrid retrieval
