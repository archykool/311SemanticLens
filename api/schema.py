"""C5 structured query object: {topic, geo, time_range, aggregation}.

The four dimensions are locked by back-reasoning from the chief-DS question
set (SPEC C5) — no speculative dimensions. time_range is optional/defaultable.
Aggregation is an object, not a flat enum: the optional `expand` member is
the drill-down hook (group-then-expand) that Q1 requires — C6/C8 populate it
when the user clicks into a group.
"""

from typing import Literal, Optional

from pydantic import BaseModel

AggType = Literal["distribution", "agency_facets", "trend", "topn", "cooccurrence"]


class Geo(BaseModel):
    # "citywide" is explicit scope; a named borough sets borough; group_by
    # community_board covers Q1/Q3/Q4's per-district shapes.
    scope: Optional[Literal["citywide", "borough"]] = None
    borough: Optional[str] = None
    group_by: Optional[Literal["community_board"]] = None


class TimeRange(BaseModel):
    gte: Optional[str] = None  # ISO date; open-ended when None
    lte: Optional[str] = None


class Expand(BaseModel):
    """Drill-down: fetch the records behind one group of an aggregation."""
    group_value: str
    size: int = 20


class Aggregation(BaseModel):
    type: AggType
    # What the buckets are: complaint categories (Q1), districts (Q3/Q4),
    # agency facets (Q2), co-occurring domains (Q5).
    group_by: Optional[str] = None
    top_n: Optional[int] = None
    expand: Optional[Expand] = None


class ParsedQuery(BaseModel):
    topic: str
    geo: Geo = Geo()
    time_range: Optional[TimeRange] = None  # defaultable per SPEC C5
    aggregation: Optional[Aggregation] = None  # None => plain hybrid retrieval

    # --explain payload: which rules fired, so demo-time parse quality is
    # visible and degradation is narratable (Archy's C5 addition).
    matched_rules: list[str] = []
