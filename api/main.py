"""FastAPI service — C5 (query understanding) + the retrieval entrypoint.

C5 scope: /parse (structured query object) and /search with the parse
attached when ?explain=true (Archy's demo requirement). /search currently
runs kNN-only retrieval as a placeholder — C6 replaces its internals with
BM25 + kNN RRF fusion, filter push-down, and the aggregation shapes. The
C5 contract (parse -> ParsedQuery -> retrieval, explain plumbing, graceful
degrade) is final.
"""

import os
from contextlib import asynccontextmanager

from elasticsearch import Elasticsearch
from fastapi import FastAPI, Query

from embedder import embed_query
from parser import parse_query
from schema import ParsedQuery

INDEX = "nyc311"

es = Elasticsearch(os.environ.get("ES_HOST", "http://localhost:9200"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    embed_query("warmup")  # load bge weights before first request
    yield


app = FastAPI(title="311SemanticLens", lifespan=lifespan)


@app.get("/parse", response_model=ParsedQuery)
def parse(q: str = Query(..., min_length=1)):
    """The C5 structured query object, always with matched_rules (explain)."""
    return parse_query(q)


@app.get("/search")
def search(q: str = Query(..., min_length=1), size: int = 10, explain: bool = False):
    parsed = parse_query(q)
    vector = embed_query(parsed.topic)

    # C6 TODO: RRF fusion with BM25, structured pre-filters from parsed.geo /
    # parsed.time_range, and the aggregation shapes. kNN-only until then.
    res = es.search(
        index=INDEX,
        knn={"field": "embedding", "query_vector": vector, "k": size,
             "num_candidates": max(100, size * 10)},
        source=["unique_key", "created_date", "complaint_type", "descriptor",
                "borough", "community_board", "agency", "problem_domain",
                "failure_mode", "agencies_involved"],
        size=size,
    )
    hits = [
        {"score": h["_score"], **h["_source"]}
        for h in res["hits"]["hits"]
    ]
    body = {"results": hits}
    if explain:
        body["explain"] = parsed.model_dump()
    return body


@app.get("/health")
def health():
    return {"ok": True, "es": es.ping()}
