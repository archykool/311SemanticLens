# NYC 311 Semantic Governance Infrastructure · Project Spec

**Codename**: Rechannel (working title, pending confirmation)
**Version**: v0.1 draft
**Date**: 2026-07-02
**Author**: Archy

---

## 1. Overview

A semantic retrieval and signal-analysis prototype over NYC 311 complaint data. Core thesis: the existing 311 taxonomy (191 top categories / 951 sub categories) is single-label and agency-centric, while real urban problems are multi-facet and cross-agency (e.g., a trash-clogged catch basin simultaneously involves DSNY, DEP, and DOT). The system uses batch LLM semantic enrichment to remap complaints onto a multi-dimensional ontology, and serves natural-language hybrid retrieval (BM25 + vector) with aggregation analytics.

### 1.1 Dual goals (equal priority)

| Goal | Audience | Success looks like |
|---|---|---|
| Job-search portfolio | Recruiters / interviewers | Demonstrates high-frequency skills (Elasticsearch, Docker, Postgres, evaluation methodology); README tells a clear engineering-decision narrative |
| Government-facing demo | City Council chief data scientist | Demonstrates cross-agency signal discovery; answers government-style natural-language questions |

### 1.2 Design principles (reasoned back from the Instacart case)

- This workload is **read-heavy and append-only** (~8–10k new records/day, existing records rarely change) — the opposite of Instacart's write catastrophe — so Elasticsearch is a legitimate choice here.
- Architectural discipline: **Postgres is the single system of record; ES is derived data, fully rebuildable at any time**. Sync is one-directional batch; no dual-write consistency problem exists.
- **Zero training**: embeddings come from an off-the-shelf pretrained model; LLM tagging goes through an API. All investment is design and engineering time.

---

## 2. System overview

```
[Offline batch]   Socrata API → Postgres (system of record) → LLM enrichment + embedding → build ES index
[Online serving]  Browser → FastAPI (query understanding + query embedding) → ES hybrid retrieval (BM25 + kNN, RRF fusion) → aggregation → JSON
[Offline eval]    FAISS IndexFlatIP brute-force exact top-k as ground truth; measure ES approximate recall@k
```

Stack: Elasticsearch 8.x (serving), PostgreSQL 16 (system of record), FAISS (evaluation only), FastAPI (service layer), bge-small-en-v1.5 or e5-base (384-dim embeddings), Docker Compose (single-command orchestration).

---

## 3. Component specs

Each component is defined by {Input / Output / Boundary / Acceptance criteria}. Boundaries are solid lines: anything outside them is explicitly out of scope for that component.

### C1 · Ingestion

- **Input**: NYC Open Data 311 dataset (Socrata API). Phase 1 scope: catch-basin-related slice (~766K rows / 460MB). Phase 2: full year 2025 (~3.4M rows).
- **Output**: raw records table in Postgres (unique key, created/closed timestamps, complaint_type, descriptor, resolution_description, geo fields, agency, status), with an incremental-pull watermark.
- **Boundary**: no real-time streaming; no non-public fields (call recordings, PII); no semantic processing beyond basic cleaning at this stage; full history (21M rows) is out of prototype scope, roadmap only.
- **Acceptance**: (1) a single command pulls the full Phase 1 slice from scratch and is resumable; (2) reruns are idempotent, row count within 0.1% of the Socrata-side count; (3) in incremental mode, a daily delta syncs in under 10 minutes.

### C2 · Semantic enrichment (Ontology + LLM)

- **Input**: unique (top category × sub category × descriptor) combinations from Postgres (a few thousand unique patterns — not per-row); a hand-designed multi-dimensional ontology (draft facet dimensions: asset type, failure mode, agencies involved, spatial scale — amendable).
- **Output**: a mapping table in Postgres: each unique combination → multi-label facet set + cross-agency attribution set; a versioned ontology definition file (managed like code).
- **Boundary**: no per-record LLM calls over 766K/21M rows; no model training or fine-tuning; the number of ontology dimensions is derived from the question set, not from a pursuit of a "complete taxonomy"; per-record semantic analysis of resolution_description is an optional extension, not Phase 1.
- **Acceptance**: (1) one-time enrichment API cost < $20; (2) on a manually reviewed sample of 100 combinations, multi-label mapping accuracy ≥ 90%; (3) trash-clogged-catch-basin-type complaints correctly map to ≥ 2 agency facets; (4) the ontology file carries a version number and changelog.

### C3 · Embedding pipeline

- **Input**: concatenated text per record (category + sub category + descriptor, ~30–60 tokens); pretrained bge-small-en-v1.5 (384-dim).
- **Output**: one 384-dim vector per record, written to Postgres first (with progress checkpoints), consumed by C4.
- **Boundary**: document embeddings and query embeddings MUST use the same model (same vector space); no fine-tuning; no multi-model ensembles; Phase 1 vector volume ≈ 1.2GB (766K), Phase 2 ≈ 5GB (3.4M); the 21M full corpus is out of scope.
- **Acceptance**: (1) batches of 256–1024 texts, job is resumable and idempotent; (2) the 766K slice encodes in ≤ 2 hours on a single-machine CPU (or ≤ 30 min on GPU); (3) vector row count exactly matches record row count.

### C4 · ES index build

- **Input**: records + facet mappings + vectors from Postgres.
- **Output**: a single ES index whose mapping includes: full-text fields (BM25), dense_vector (HNSW, int8 quantization available), structured filter fields (borough, community district, time, agency, facets, status).
- **Boundary**: no data may exist only in ES — the index must be fully rebuildable from Postgres at any time; no multi-index sharding strategy (a single index covers Phase 2 scale); no ES cluster — high availability is out of prototype scope.
- **Acceptance**: (1) rebuilding the Phase 1 index from an empty ES takes ≤ 30 minutes; (2) post-rebuild document count exactly matches Postgres; (3) memory footprint fits a 16GB single machine (enable int8 quantization if needed).

### C5 · Query understanding (inside FastAPI)

- **Input**: user natural-language query (Chinese/English); a predefined chief-DS question set (finalized, see below).
- **Output**: a structured query object `{topic (semantic part), geo, time_range, aggregation}` — the four dimensions are confirmed sufficient by back-reasoning from the question set; `time_range` is an **optional/defaultable** attribute; `aggregation` must support **drill-down** (group-then-expand), not a flat enum.

**Chief-DS question set (D1 output, locked):**

| # | Question | Capability | topic | geo | time | aggregation |
|---|---|---|---|---|---|---|
| Q1 lead | "Where in Brooklyn is stormwater not draining?" | Semantic understanding | ✓ (no category words) | Brooklyn, group by community district | default | category distribution → drill down to records |
| Q2 | Which agencies does a single catch-basin complaint actually involve? | Cross-agency signal (killer feature) | ✓ | default | default | agency-facet distribution |
| Q3 | Which district's drainage complaints grew fastest last year? | Time trend | ✓ | by district | ✓ past year | trend |
| Q4 | Top 10 districts for catch-basin clogging citywide? | Spatial ranking | ✓ | citywide, by district | default | topN |
| Q5 | What problems tend to co-occur with catch-basin clogging? | Co-occurrence / association | ✓ | default | default | co-occurrence |

Coverage check: topic exercised 5×; geo 3× (Q1/Q3/Q4); time 1× (Q3); aggregation spans distribution / drill-down / trend / topN / co-occurrence; the agency facet is the lead in Q2. Every schema dimension is question-driven — no speculative dimensions.
- **Boundary**: no multi-turn clarification dialogs; no query rewriting beyond spell correction; on parse failure, degrade gracefully to plain hybrid retrieval (no aggregation) rather than erroring out.
- **Acceptance**: (1) all 5 questions in the question set parse correctly into structured objects; (2) ≥ 80% parse accuracy on 20 paraphrased variants; (3) single-parse latency ≤ 500ms.

### C6 · Hybrid retrieval & aggregation

- **Input**: the structured query object from C5.
- **Output**: ES retrieval results (BM25 + kNN, RRF fusion, structured conditions pushed down as pre-filters) + FastAPI-layer aggregation (group-by-district, time trends, rankings, co-occurrence), returned as JSON.
- **Boundary**: no trained rerankers (e.g., learning-to-rank); no personalization; aggregation types are limited to the shapes the question set covers (grouped counts, trends, topN, co-occurrence) — open-ended analysis is left to the user.
- **Acceptance**: (1) "healthy foods"-type queries (e.g., "stormwater drainage problems", lexically matching no category name) retrieve Sewer / Catch Basin / Street Flooding records; (2) exact-term queries (e.g., "catch basin clogged") recall normally via the BM25 path; (3) end-to-end p95 latency ≤ 2 seconds (local single machine).

### C7 · Evaluation harness

- **Input**: a golden set (~100 test queries with human relevance labels); FAISS IndexFlatIP brute-force exact retrieval over the same vectors.
- **Output**: an evaluation report: recall@10 (ES approximate vs FAISS exact), precision (against the golden set), plus macro-F1 if the "query → category mapping" framing is used.
- **Boundary**: FAISS is used in evaluation only and never appears in the online serving path; evaluation does not cover UI usability; no A/B testing (no real traffic).
- **Acceptance**: (1) recall@10 ≥ 0.95 relative to the FAISS exact ground truth; (2) precision@10 ≥ 0.8 on the golden set; (3) the evaluation script reproduces with a single command and writes a versioned report.

### C8 · Demo frontend

- **Input**: JSON output from C6.
- **Output**: a single-page demo: query box + result list + at least one aggregation visualization (district map or trend line).
- **Boundary**: no user accounts, permissions, or multi-tenancy; visual polish is secondary to functional demonstration; always-on hosting is optional (small VPS, $5–15/month) — the default deliverable is a screen recording + screenshots + local one-command reproduction via `docker compose up`.
- **Acceptance**: (1) all 5 chief-DS questions are demonstrable on the page; (2) on a fresh machine, cloning the repo and running `docker compose up` brings up the full working stack; (3) the README carries the dual narrative (engineering decisions for recruiters + signal discovery for the government audience).

---

## 4. Global boundaries (non-goals, solid lines)

1. **No cloud**: the prototype runs entirely on local Docker Compose; the cloud migration path (Cloud SQL + Cloud Run / managed ES) lives on a single roadmap page only.
2. **No model training**: no fine-tuning, no learning-to-rank, no custom embeddings.
3. **No real-time streaming**: daily batch increments suffice.
4. **No 21M full corpus**: Phase 1 = 766K slice → Phase 2 = 2025 full year (3.4M); the full corpus is only argued feasible (~13GB of vectors, well under the 50–100M-vectors-per-index comfort ceiling), not built.
5. **No dual retrieval engines in serving**: FAISS never enters the online path; pgvector is not enabled (Postgres is the system of record only).
6. **No non-public data**: call recordings and personally identifiable information are never touched.

## 5. Global acceptance criteria (project-level Definition of Done)

- [ ] `docker compose up` brings up the full stack (ES + Postgres + FastAPI + demo page) with one command.
- [ ] Phase 1 (766K catch-basin slice) runs end to end: ingest → enrich → embed → index → retrieve → evaluate.
- [ ] recall@10 ≥ 0.95 (vs FAISS exact); precision@10 ≥ 0.8 (vs golden set).
- [ ] All 5 chief-DS questions demonstrable, at least one showing a cross-agency signal (a single complaint mapped to ≥ 2 agencies).
- [ ] One-time cost ≤ $20 (LLM enrichment); zero recurring cost (excluding the optional VPS).
- [ ] README dual narrative complete; evaluation report reproducible.
- [ ] Phase 2 (2025 full year, 3.4M) ingested and indexed as the "the system scales" chapter.

## 6. Milestones (dual-track: W0 MVP sprint + v1.1 hardening)

### Track A · W0 one-week MVP (downgraded acceptance, tagged v0)

Premise: use Fable 5 Cowork/Code to compress engineering time; judgment tasks done by Archy directly; wall-clock tasks (ingest / encode / enrichment) run in the background in parallel. Dataset remains the 766K catch-basin slice (the cost bottleneck is not row count).

| Day | Archy (judgment, non-outsourceable) | Cowork/Code (engineering) | Background (wall-clock) |
|---|---|---|---|
| D1 | **Define 5-question set** + draft facet dimensions (hard blocker) | compose skeleton + ingestion script | Socrata pull starts |
| D2 | Review enrichment prompt, spot-check first mappings | embedding pipeline + enrichment batch script | enrichment runs |
| D3 | Re-check mappings (50 samples, v0 downgrade) | ES mapping + index + FastAPI skeleton | encode 766K vectors |
| D4 | Label golden set (30 items, v0 downgrade) | query understanding + hybrid retrieval | — |
| D5 | Read core code (RRF / HNSW / fusion logic) | eval script + FAISS baseline | eval runs |
| D6 | Accept demo behavior on the 5 questions | demo page + visualization | — |
| D7 | Finalize README dual narrative | **buffer** (integration debug will consume it) | — |

v0 downgraded acceptance (distinct from §5 full standard): golden set 30 items (not 100); mapping spot-check 50 (not 100); no hard recall/precision gates yet (run through and record a baseline).

### Track B · v1.1 hardening (after W0, as needed)

- Meet all §5 acceptance numbers (recall@10 ≥ 0.95, precision@10 ≥ 0.8, mapping accuracy ≥ 90%).
- Expand golden set to 100; mapping spot-check to 100.
- Ingest and index Phase 2 (2025 full year, 3.4M).
- Deepen the README engineering narrative (prepare for interview probes: RRF's k, HNSW's ef_search, etc.).

## 7. Open items (blocking first)

1. ~~**[Blocks C5] Chief-DS question set**~~ → **Resolved (D1)**: 5 questions locked (see C5); four dimensions confirmed sufficient, with two added constraints: `time_range` is defaultable, `aggregation` must support drill-down.
2. **Project name**: Rechannel is the working candidate, pending confirmation.
3. Whether resolution_description joins the Phase 1 text concatenation (current: no; listed as an extension).
4. Demo hosting form: recorded delivery (default) vs always-on VPS (optional).
