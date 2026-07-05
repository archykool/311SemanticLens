"""C5 query understanding: pure-rules parser (Archy's Option A decision).

Why rules, not an LLM (interview narrative): parse latency is ~microseconds
against a 500ms budget; zero recurring cost (SPEC §5 DoD); and the serving
path stays fully local — `docker compose up` on a fresh machine works with
no API key (SPEC global boundary 1). The question set is closed-world (five
intents), so lexicons cover it; anything the rules can't classify leaves
aggregation=None, which C6 treats as plain hybrid retrieval (SPEC C5
boundary: degrade gracefully, never error).

Parsing is subtractive: recognized geo/time/aggregation phrases are REMOVED
from the query and whatever survives becomes `topic` — the semantic residue
that goes to BM25 + query embedding. The topic side is fuzzy by design, so
the rules only need to be precise about structure, not vocabulary.

Every fired rule is recorded in matched_rules for --explain.
"""

import re
from datetime import date, timedelta

from schema import Aggregation, Geo, ParsedQuery, TimeRange

# --- geo -------------------------------------------------------------------

BOROUGHS = {
    "brooklyn": "BROOKLYN", "布鲁克林": "BROOKLYN",
    "bronx": "BRONX", "布朗克斯": "BRONX",
    "queens": "QUEENS", "皇后区": "QUEENS", "昆斯": "QUEENS",
    "manhattan": "MANHATTAN", "曼哈顿": "MANHATTAN",
    "staten island": "STATEN ISLAND", "史泰登岛": "STATEN ISLAND", "斯塔滕岛": "STATEN ISLAND",
}

CITYWIDE_RE = re.compile(r"citywide|across the (?:whole )?city|all boroughs|全市|全城", re.I)

# Interrogatives/groupers that mean "break results down by district".
DISTRICT_RE = re.compile(
    r"which districts?|by district|per district|each district|district'?s?\b"
    r"|which (?:parts|areas|neighborhoods)|where\b|districts\b"
    r"|哪个区|哪些区|各区|按区|按社区|哪里|哪些地方",
    re.I,
)

# --- time ------------------------------------------------------------------

PAST_YEAR_RE = re.compile(
    r"(?:past|last) (?:year|12 months|twelve months)|去年|过去一年|过去12个月|最近一年", re.I
)
PAST_MONTHS_RE = re.compile(r"(?:past|last) (\d+) months?|过去(\d+)个月|最近(\d+)个月", re.I)

# --- aggregation intents (checked in priority order) ------------------------
# Priority matters: Q3 contains both "which district" (distribution-ish) and
# "grew fastest" (trend) — the more specific analytical intent wins, and the
# district phrase still contributes geo.group_by.

COOCCUR_RE = re.compile(
    r"co-?\s?occur\w*|together with|show up together|accompan\w+|alongside"
    r"|tend to (?:come|appear) with|伴随|共现|同时出现",
    re.I,
)
AGENCY_RE = re.compile(
    r"(?:which|what) (?:agencies|departments)|agencies .{0,20}involved?"
    r"|who (?:handles|deals with|is responsible)|besides \w+ who"
    r"|涉及.{0,6}(?:部门|机构)|哪些部门|哪些机构",
    re.I,
)
TREND_RE = re.compile(
    r"fastest|grew|grow(?:th|ing|s)?\b|increas\w+|rising|trend\w*|surge\w*"
    r"|增长|趋势|上升|涨得?最快",
    re.I,
)
TOPN_RE = re.compile(r"top\s*(\d+)|前\s*(\d+)|前十|前五|rank(?:ing)?s?\b|排名|(\d+)\s+districts?\s.{0,30}most|most affected", re.I)
DISTRIBUTION_RE = re.compile(r"where\b|which (?:parts|areas|neighborhoods|districts)|distribution|分布|哪里|哪些地方|哪个区", re.I)

CJK_NUM = {"十": 10, "五": 5}

# Filler stripped from the edges of the residual topic. Interior words are
# kept — the embedding model handles natural phrasing better than keyword soup.
EDGE_FILLER_RE = re.compile(
    r"^(?:is|are|does|do|did|a|an|the|in|for|of|with|what|which|that|tend to|问题|投诉)\s+"
    r"|\s+(?:is|are|does|do|a|an|the|in|for|of|with|actually|involve[sd]?|complaints?|问题|投诉)$",
    re.I,
)


def _consume(pattern, text, matched, rule_name, groups=False):
    """Delete pattern matches from text; log the rule if anything matched."""
    hit = pattern.search(text)
    if not hit:
        return text, None
    matched.append(rule_name)
    result = hit.groups() if groups else hit.group(0)
    return pattern.sub(" ", text), result


def parse_query(q: str, today: date | None = None) -> ParsedQuery:
    today = today or date.today()
    matched: list[str] = []
    text = q.strip()

    # geo — borough names, citywide, district grouping
    geo = Geo()
    lowered = text.lower()
    for name, canonical in BOROUGHS.items():
        if name in lowered:
            geo.scope, geo.borough = "borough", canonical
            matched.append(f"geo:borough={canonical}")
            text = re.sub(re.escape(name), " ", text, flags=re.I)
            break
    text, hit = _consume(CITYWIDE_RE, text, matched, "geo:citywide")
    if hit and geo.scope is None:
        geo.scope = "citywide"

    # time — defaultable; absent means "all of the corpus"
    time_range = None
    text, hit = _consume(PAST_YEAR_RE, text, matched, "time:past_year")
    if hit:
        time_range = TimeRange(gte=(today - timedelta(days=365)).isoformat())
    else:
        text, groups = _consume(PAST_MONTHS_RE, text, matched, "time:past_n_months", groups=True)
        if groups:
            months = int(next(g for g in groups if g))
            time_range = TimeRange(gte=(today - timedelta(days=30 * months)).isoformat())

    # aggregation — first matching intent wins (priority order documented above)
    aggregation = None
    text, hit = _consume(COOCCUR_RE, text, matched, "agg:cooccurrence")
    if hit:
        aggregation = Aggregation(type="cooccurrence", group_by="problem_domain")
    if aggregation is None:
        text, hit = _consume(AGENCY_RE, text, matched, "agg:agency_facets")
        if hit:
            aggregation = Aggregation(type="agency_facets", group_by="agencies_involved")
    if aggregation is None:
        text, hit = _consume(TREND_RE, text, matched, "agg:trend")
        if hit:
            aggregation = Aggregation(type="trend", group_by="community_board")
            # Q3's shape: trend BY district over the past year unless stated
            if time_range is None:
                time_range = TimeRange(gte=(today - timedelta(days=365)).isoformat())
                matched.append("time:default_past_year_for_trend")
    if aggregation is None:
        text, groups = _consume(TOPN_RE, text, matched, "agg:topn", groups=True)
        if groups is not None:
            n = next((int(g) for g in groups if g and g.isdigit()), None)
            if n is None:  # 前十 / 前五 / bare "rank"
                n = next((v for k, v in CJK_NUM.items() if k in q), 10)
            aggregation = Aggregation(type="topn", group_by="community_board", top_n=n)

    # district grouping — feeds geo AND (if nothing more specific matched)
    # implies the Q1 distribution shape
    text, hit = _consume(DISTRICT_RE, text, matched, "geo:group_by_district")
    if hit:
        geo.group_by = "community_board"
    if aggregation is None and hit:
        matched.append("agg:distribution")
        aggregation = Aggregation(type="distribution", group_by="complaint_type")
    elif aggregation is None:
        text, hit = _consume(DISTRIBUTION_RE, text, matched, "agg:distribution")
        if hit:
            aggregation = Aggregation(type="distribution", group_by="complaint_type")

    # topic — the untranslated residue; C6 embeds it (with BGE_QUERY_PREFIX)
    # and BM25-matches it. If aggregation is None this is the whole query
    # minus geo/time, and C6 falls back to plain hybrid retrieval.
    topic = re.sub(r"[?？,，。.!！]", " ", text)
    # orphaned possessives left where a phrase was consumed ("district's" -> "'s")
    topic = re.sub(r"(?<!\w)'s\b", " ", topic)
    topic = re.sub(r"\s+", " ", topic).strip()
    for _ in range(4):  # strip layered edge filler ("is the", "for a", ...)
        topic = EDGE_FILLER_RE.sub("", topic).strip()
        topic = topic.strip("'\"- ")
    if not topic:
        topic = q.strip()  # never send an empty topic downstream
        matched.append("topic:fallback_full_query")

    return ParsedQuery(
        topic=topic, geo=geo, time_range=time_range,
        aggregation=aggregation, matched_rules=matched,
    )
