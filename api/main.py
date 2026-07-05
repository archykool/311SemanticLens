"""FastAPI service — C5 query understanding + C6 hybrid retrieval/aggregation.

/search request flow:
  parse (rules, C5) -> embed topic (bge + query prefix) -> select patterns
  (semantic, 337 in-memory vectors) -> hybrid RRF results + aggregation
  shape if one was parsed. geo/time ride along as pre-filters everywhere.

Drill-down (Q1's two-stage contract): aggregation responses contain
`expand` hints per bucket; the client re-requests with expand_group=... to
get the records behind that bucket. Stage 1 never inlines records.

?explain=true attaches the parsed query, selected patterns, and RRF leg
ranks — Archy's live demo narration channel.
"""

import os
from contextlib import asynccontextmanager

import os.path

from elasticsearch import Elasticsearch
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from aggregations import run_aggregation
from embedder import embed_query
from parser import parse_query
from retrieval import PatternStore, filters_from, hybrid_search
from schema import Expand, ParsedQuery

INDEX = "nyc311"

es = Elasticsearch(os.environ.get("ES_HOST", "http://localhost:9200"),
                   request_timeout=30)
pattern_store: PatternStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pattern_store
    embed_query("warmup")           # load bge weights
    pattern_store = PatternStore()  # load 337 pattern vectors + facets
    yield


app = FastAPI(title="311SemanticLens", lifespan=lifespan)


@app.get("/parse", response_model=ParsedQuery)
def parse(q: str = Query(..., min_length=1)):
    return parse_query(q)


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    size: int = 10,
    explain: bool = False,
    # Stage 2 of drill-down: identify the bucket whose records to fetch.
    expand_group: str | None = None,
    expand_size: int = 20,
):
    parsed = parse_query(q)
    if expand_group and parsed.aggregation:
        parsed.aggregation.expand = Expand(group_value=expand_group, size=expand_size)

    query_vec = embed_query(parsed.topic)
    patterns = pattern_store.select(query_vec, parsed.topic)
    filters = filters_from(parsed)

    hits, rrf_debug = hybrid_search(
        es, INDEX, parsed.topic, query_vec, filters, size=size, explain=explain,
        pattern_store=pattern_store,
    )
    body = {"results": hits}

    if parsed.aggregation is not None and patterns:
        body["aggregation"] = run_aggregation(es, INDEX, parsed, patterns, pattern_store)

    if explain:
        body["explain"] = {
            "parsed": parsed.model_dump(),
            "selected_patterns": patterns,
            "rrf": rrf_debug,
        }
    return body


@app.get("/health")
def health():
    return {"ok": True, "es": es.ping(),
            "patterns_loaded": len(pattern_store.texts) if pattern_store else 0}


# C8 demo page — mounted last so API routes take precedence. html=True
# serves static/index.html at "/".
app.mount("/", StaticFiles(
    directory=os.path.join(os.path.dirname(__file__), "static"), html=True))
