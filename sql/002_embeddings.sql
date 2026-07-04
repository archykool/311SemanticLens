-- C3 embedding storage. Two-level design driven by a data fact: the 766K
-- rows contain only ~337 distinct (complaint_type, descriptor,
-- additional_details) texts, so we embed each distinct text once and fan
-- the vectors out to records with a join — never 766K model calls.
--
-- Vectors are BYTEA: 384 float32 little-endian (1536 bytes). pgvector is
-- deliberately NOT used (SPEC §4.5: Postgres is the system of record only;
-- similarity search happens in ES / FAISS, never in Postgres).

-- One row per distinct embed text. This table doubles as the job's
-- checkpoint: the pipeline commits after every batch, and a restarted run
-- only embeds texts not already present.
CREATE TABLE IF NOT EXISTS embedding_text_cache (
    model       TEXT NOT NULL,
    embed_text  TEXT NOT NULL,
    embedding   BYTEA NOT NULL,
    dim         INT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model, embed_text)
);

-- One vector per record (SPEC C3 output), materialized from the cache.
-- ~1.2GB for Phase 1 — the spec's expected volume. C4 reads this table
-- when building the ES index; C7 reads it to build the FAISS baseline.
CREATE TABLE IF NOT EXISTS record_embeddings (
    unique_key  BIGINT PRIMARY KEY REFERENCES raw_311_requests (unique_key),
    model       TEXT NOT NULL,
    embedding   BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
