# 311SemanticLens

Semantic retrieval and signal-analysis prototype over NYC 311 complaint data.
Full spec: [Docs/spec-311-semantic-en.md](Docs/spec-311-semantic-en.md).

Status: **C1–C7 done** (C7 recall track complete; precision track pending
Archy's golden labels). Next: C8 (demo frontend). See the spec's §6 build
order.

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

## Embeddings (C3)

Documents are embedded with `BAAI/bge-small-en-v1.5` (384-dim, L2-normalized).
The pipeline exploits a data fact: the 766K rows contain only ~337 distinct
(complaint_type, descriptor, additional_details) texts, so it embeds each
distinct text once into `embedding_text_cache` (committing per batch — that
table is also the resume checkpoint) and fans vectors out to
`record_embeddings` with a single SQL join. Seconds of CPU encoding instead
of the 2-hour budget in the spec.

```bash
docker compose build embedding
docker compose run --rm embedding
```

Safe to rerun anytime; a rerun encodes only missing texts and inserts only
missing vectors. The document text is constructed **in SQL** (see
`EMBED_TEXT_SQL` in `embedding/embed.py`) so the embed and join steps can't
drift; C5 must reuse the same model plus `BGE_QUERY_PREFIX` for queries.

## Semantic enrichment (C2)

The ontology ([Docs/ontology-v0.1.md](Docs/ontology-v0.1.md), human-designed
and locked) remaps the 337 distinct complaint patterns onto three facets:
`problem_domain`, `failure_mode`, and `agencies_involved` — the last being
the cross-agency signal the demo is built around (e.g. a clogged catch basin
maps to DEP+DSNY+DOT, not just the agency 311 routed it to).

One `claude-opus-4-8` call per distinct combo — never per-record — with
schema-constrained JSON output, cross-field validation, and a retry-once-
then-flag-for-review policy (no silent guessing). Facets land in
`combo_facets` and fan out to all 766K records via the `record_facets` view.
One-time cost ≈ $5.30, well under the $20 budget.

```bash
# needs ANTHROPIC_API_KEY in .env
docker compose build enrichment
docker compose run --rm enrichment
```

Resumable: reruns only process combos not yet classified. Result: 65,699
records (~8.6%) carry a ≥2-agency signal, concentrated in the drainage
domain (23 of its 30 combos are multi-agency).

## Query understanding (C5)

Natural language (EN/中文) → `{topic, geo, time_range, aggregation}` via a
**pure-rules parser** ([api/parser.py](api/parser.py)). Why rules and not an
LLM: ~0.05ms parse latency against a 500ms budget, zero recurring cost
(§5 DoD), and the serving path stays fully local — `docker compose up` on a
fresh machine needs no API key. Parsing is subtractive: recognized
geo/time/intent phrases are consumed and the residue becomes `topic`, which
goes to BM25 + query embedding (the fuzzy side tolerates phrasing, so rules
only need to be precise about structure). Unparseable queries degrade to
plain hybrid retrieval — never an error.

Query embeddings use the same bge model as documents **plus**
`BGE_QUERY_PREFIX` (documents are embedded without it) — the contract lives
in [shared/semantic_contract.py](shared/semantic_contract.py) and
`api/smoke_prefix.py` proves the prefix is live by printing top-10s with
and without it.

```bash
docker compose up -d api            # http://localhost:8000
curl "localhost:8000/parse?q=Top+10+districts+for+catch-basin+clogging+citywide"
curl "localhost:8000/search?q=stormwater+drainage+problems&explain=true"
docker compose run --rm api pytest test_parser.py -v   # 5 canonical + 20 paraphrases + latency gates
docker compose run --rm api python smoke_prefix.py     # prefix A/B
```

Gates: 5/5 canonical questions, 20/20 paraphrases (≥80% required),
0.054ms/parse (≤500ms required). `?explain=true` attaches the parsed query
object and fired rules to search responses for live demo narration.

## Hybrid retrieval & aggregation (C6)

**RRF fusion at the pattern level** ([api/retrieval.py](api/retrieval.py)):
BM25 and kNN rankings are collapsed to the ~337 distinct text patterns
*before* fusing (`score = Σ 1/(60+rank)`), because thousands of records
share each pattern — record-level fusion degrades to whichever leg's
duplicates flood the depth. Fusion happens at the granularity where the
two signals are comparable; one representative record per fused pattern
also deduplicates the result list for free. A rank-weighted
**domain-consensus rerank** (the C2 ontology feeding back into retrieval)
demotes patterns that disagree with the fused head's majority domain —
this is what keeps "No Water" out of the results for "stormwater not
draining", the negation case embeddings get wrong (regression-tested).

geo/time from the parsed query are pushed down as pre-filters on both legs
and every aggregation. Aggregations run over the full matching record set,
not a top-k list: the query semantically selects relevant patterns (337
in-memory vectors), and the selection is pushed down as an exact
`full_text.raw` terms filter. Five shapes, one per chief-DS question:
district distribution (Q1), agency facets routed-vs-involved (Q2),
half-over-half trend by district (Q3), top-N (Q4), and spatial-temporal
co-occurrence with citywide lift (Q5, district×month cells).

**Q1's drill-down is two-stage by contract**: stage 1 returns buckets with
`expand` hints only; the client re-requests with `expand_group=<district>`
to get the records behind one bucket. Never flattened.

```bash
curl "localhost:8000/search?q=Where+in+Brooklyn+is+stormwater+not+draining&explain=true"
curl "localhost:8000/search?q=Where+in+Brooklyn+is+stormwater+not+draining&expand_group=03+BROOKLYN"
docker compose run --rm api pytest test_c6.py -v -s   # 5 questions + regressions + p95
```

Gates: all five questions return their shapes; exact-term and semantic
paths verified; No Water regression; p95 latency 181ms (≤2s required).

## Evaluation (C7)

FAISS (`IndexFlatIP`, exact) lives only in the eval image — never in
serving. Evaluation is at the **pattern level**: thousands of records share
identical vectors, so record-level top-10 comparison is tie-breaking noise.

**The eval harness earned its keep immediately**: the first recall run
measured 0.70 mean / 0.10 min and exposed that record-level kNN is
structurally broken in this corpus — head patterns (up to 67,903 identical
vectors) crowd out everything else within any reachable k. Fix: the kNN
leg now searches a 337-doc **pattern index** (`nyc311_patterns`, float
HNSW ≈ exact) and representative records are materialized afterwards WITH
geo/time filters. Post-fix: **recall@10 mean 0.9567 (gate 0.95: PASS)**,
residual misses are 1-2 near-tied patterns at the rank-10 boundary.

precision@10 runs against a human-labeled golden set (30 queries incl.
adversarial; facets are never used as a relevance proxy). Candidates are
**pooled** — BM25 top-15 + kNN top-15 + serving top-10 + 5 random, sources
hidden from the judge (protocol: [eval/LABELING.md](eval/LABELING.md)).

```bash
docker compose run --rm eval python pool.py   # labeling sheet -> Data/
docker compose run --rm eval                  # report -> eval/reports/ (versioned)
```
