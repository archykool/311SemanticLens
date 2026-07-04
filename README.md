# 311SemanticLens

Semantic retrieval and signal-analysis prototype over NYC 311 complaint data.
Full spec: [Docs/spec-311-semantic-en.md](Docs/spec-311-semantic-en.md).

Status: **C1 (ingestion) done.** See the spec's §6 build order for what's next.

## Setup

```bash
cp .env.example .env       # defaults are fine for local dev
docker compose up -d postgres elasticsearch
```

Postgres schema (`sql/001_schema.sql`) applies automatically on first boot
via `docker-entrypoint-initdb.d`.

## Ingestion (C1)

Postgres is the system of record; see `Docs/spec-311-semantic-en.md` §1.2.
Phase 1 corpus: NYC 311 complaints for 2025, filtered to
DSNY/HPD/DEP/DOT/NYPD/DOHMH/DPR (~766K rows) — the agencies that plausibly
touch catch-basin/drainage problems, since the same underlying issue shows
up under different complaint types depending on which agency responded.

One-shot bulk load from the local CSV export:

```bash
docker compose build ingestion
docker compose run --rm ingestion python ingest.py bulk-csv --csv /data/311_2025_CloggedBasin.csv
```

Daily incremental sync against the live Socrata API (same agency filter,
resumes from `ingestion_watermark`):

```bash
docker compose run --rm ingestion python ingest.py incremental
```

Both modes are idempotent — safe to rerun after an interrupted pull. See
`ingestion/ingest.py` and `sql/001_schema.sql` for why (staging table +
upsert).

> Windows/Git Bash note: prefix commands with `MSYS_NO_PATHCONV=1` — Git
> Bash otherwise rewrites `/data/...`-style container paths into Windows
> paths before Docker ever sees them.
