"""C4: build the single ES index from Postgres (the system of record).

Everything in ES is derived: this script can rebuild the index from scratch
at any time (SPEC C4 boundary — no data may exist only in ES). Rebuilds are
zero-downtime via an alias swap: documents load into a timestamped physical
index (nyc311_<epoch>), and only after the doc count exactly matches
Postgres does the `nyc311` alias atomically move to it; old indices are
then deleted. A failed build leaves the live index untouched.

Mapping decisions (interview-defensible, see README):
  * dense_vector uses int8_hnsw quantization: 766K x 384 float32 = ~1.2GB
    of raw vectors, but int8 quantization cuts the HNSW-resident copy to
    ~300MB with negligible recall loss at this scale — that's what keeps
    the whole stack inside a 16GB machine (C4 acceptance #3). C7 measures
    the actual recall cost against the FAISS exact baseline.
  * similarity=dot_product, valid ONLY because C3 L2-normalizes vectors
    (dot product == cosine on unit vectors, and dot_product skips the
    per-comparison normalization cosine pays).
  * embedding is excluded from _source: it lives in the HNSW structure for
    search; storing 384 floats per doc in _source would roughly double
    index size for nothing (Postgres is the vector system of record).
  * text fields use the english analyzer so "flooding"/"flooded"/"floods"
    stem together — these BM25 fields are the exact-term retrieval path
    (C6 acceptance #2).

Usage:
    python build_index.py
"""

import os
import sys
import time

import numpy as np
import psycopg
from elasticsearch import Elasticsearch, helpers

ALIAS = "nyc311"
DIM = 384
BATCH = 1000

MAPPING = {
    "settings": {
        "number_of_shards": 1,      # single node, no HA (C4 boundary)
        "number_of_replicas": 0,
        "refresh_interval": "-1",   # disabled during bulk load; restored after
    },
    "mappings": {
        # Vectors are searchable but not stored per-doc; rebuildable from PG.
        "_source": {"excludes": ["embedding"]},
        "properties": {
            "unique_key": {"type": "long"},
            "created_date": {"type": "date"},
            "closed_date": {"type": "date"},
            "status": {"type": "keyword"},
            # BM25 path. complaint_type/descriptor also get keyword subfields
            # for aggregations (Q1 category distribution drill-down).
            "complaint_type": {
                "type": "text", "analyzer": "english",
                "fields": {"raw": {"type": "keyword"}},
            },
            "descriptor": {
                "type": "text", "analyzer": "english",
                "fields": {"raw": {"type": "keyword"}},
            },
            "additional_details": {"type": "text", "analyzer": "english"},
            # The exact canonical string that was embedded (C3) — one field
            # for whole-document BM25 matching.
            "full_text": {"type": "text", "analyzer": "english"},
            # Structured filters (C5 query object pushes these down as
            # pre-filters before BM25/kNN run).
            "agency": {"type": "keyword"},
            "borough": {"type": "keyword"},
            "community_board": {"type": "keyword"},
            "council_district": {"type": "keyword"},
            "incident_zip": {"type": "keyword"},
            # C2 facets — agencies_involved is the killer-feature field (Q2).
            "problem_domain": {"type": "keyword"},
            "failure_mode": {"type": "keyword"},
            "agencies_involved": {"type": "keyword"},
            "location": {"type": "geo_point"},
            "embedding": {
                "type": "dense_vector",
                "dims": DIM,
                "index": True,
                "similarity": "dot_product",
                "index_options": {
                    # m/ef_construction are ES defaults (16/100) — fine at
                    # 766K vectors; ef_search is a query-time knob C7 tunes
                    # if recall@10 misses the 0.95 gate.
                    "type": "int8_hnsw",
                    "m": 16,
                    "ef_construction": 100,
                },
            },
        },
    },
}

FETCH_SQL = """
SELECT
    r.unique_key,
    r.created_date,
    r.closed_date,
    r.status,
    r.complaint_type,
    r.descriptor,
    r.additional_details,
    concat_ws('. ',
        nullif(trim(r.complaint_type), ''),
        nullif(trim(r.descriptor), ''),
        nullif(trim(r.additional_details), '')
    ) AS full_text,
    r.agency,
    r.borough,
    r.community_board,
    r.council_district,
    r.incident_zip,
    r.latitude,
    r.longitude,
    f.problem_domain,
    f.failure_mode,
    f.agencies_involved,
    e.embedding
FROM raw_311_requests r
JOIN record_embeddings e ON e.unique_key = r.unique_key
LEFT JOIN record_facets f ON f.unique_key = r.unique_key
"""


def get_pg():
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "semanticlens"),
        user=os.environ.get("POSTGRES_USER", "semanticlens"),
        password=os.environ.get("POSTGRES_PASSWORD", "changeme"),
    )


def get_es():
    return Elasticsearch(
        os.environ.get("ES_HOST", "http://localhost:9200"),
        request_timeout=120,
    )


def doc_actions(conn, index_name):
    # Named (server-side) cursor: streams 766K rows without loading them all.
    with conn.cursor(name="index_stream") as cur:
        cur.itersize = BATCH
        cur.execute(FETCH_SQL)
        for row in cur:
            (unique_key, created, closed, status, ctype, desc, details,
             full_text, agency, borough, cb, cd, zip_, lat, lon,
             domain, mode, agencies, emb) = row
            doc = {
                "unique_key": unique_key,
                "created_date": created.isoformat() if created else None,
                "closed_date": closed.isoformat() if closed else None,
                "status": status,
                "complaint_type": ctype,
                "descriptor": desc,
                "additional_details": details,
                "full_text": full_text,
                "agency": agency,
                "borough": borough,
                "community_board": cb,
                "council_district": cd,
                "incident_zip": zip_,
                "problem_domain": domain,
                "failure_mode": mode,
                "agencies_involved": agencies,
                "embedding": np.frombuffer(emb, dtype=np.float32).tolist(),
            }
            if lat is not None and lon is not None:
                doc["location"] = {"lat": lat, "lon": lon}
            yield {"_index": index_name, "_id": unique_key, "_source": doc}


def main():
    pg = get_pg()
    es = get_es()

    expected = pg.execute("SELECT count(*) FROM raw_311_requests").fetchone()[0]
    vectors = pg.execute("SELECT count(*) FROM record_embeddings").fetchone()[0]
    if expected != vectors:
        sys.exit(f"records ({expected}) != vectors ({vectors}) — run embedding first")

    index_name = f"{ALIAS}_{int(time.time())}"
    es.indices.create(index=index_name, **MAPPING)
    print(f"created {index_name}, streaming {expected} docs from Postgres")

    start = time.time()
    ok = 0
    for success, item in helpers.streaming_bulk(
        es, doc_actions(pg, index_name), chunk_size=BATCH, request_timeout=120,
        raise_on_error=True,
    ):
        ok += 1
        if ok % 100000 == 0:
            print(f"  {ok}/{expected} ({ok / (time.time() - start):.0f} docs/s)")

    es.indices.put_settings(index=index_name, settings={"refresh_interval": "1s"})
    es.indices.refresh(index=index_name)
    got = es.count(index=index_name)["count"]
    print(f"indexed {got} docs in {(time.time() - start) / 60:.1f} min")

    # C4 acceptance #2: exact match, or we do NOT go live.
    if got != expected:
        sys.exit(f"ABORT: ES has {got} docs, Postgres has {expected} — alias not moved")

    # Atomic swap: point the alias at the new index, drop old generations.
    old = list(es.indices.get_alias(name=ALIAS).keys()) if es.indices.exists_alias(name=ALIAS) else []
    actions = [{"add": {"index": index_name, "alias": ALIAS}}]
    actions += [{"remove": {"index": o, "alias": ALIAS}} for o in old]
    es.indices.update_aliases(actions=actions)
    for o in old:
        es.indices.delete(index=o)
        print(f"deleted old index {o}")
    print(f"alias '{ALIAS}' -> {index_name}; rebuild complete")


if __name__ == "__main__":
    main()
