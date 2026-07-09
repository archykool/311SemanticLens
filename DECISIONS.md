# DECISIONS.md — every non-obvious choice, with receipts

Factual record for the author. Each entry cites where the value lives in
code, what it controls, and whether it is **principled** (derived from data,
a paper, or a hard constraint) or **eyeballed** (chosen by inspection,
pending golden-set calibration). Line numbers current as of commit `e44ff64`.

---

## Pattern-selection absolute floor (0.70)

- **What**: the minimum cosine similarity a complaint pattern needs against the query before aggregations will count its records.
- **Value / choice**: `PATTERN_ABS_MIN = 0.70` — [api/retrieval.py:53](api/retrieval.py#L53), applied at line 86 as `floor = max(PATTERN_ABS_MIN, best - PATTERN_REL_WINDOW)`.
- **Why this and not the alternative**: bge similarity scores compress into a high, narrow band — measured anchors from C3: related drainage patterns score ~0.79–0.88 against each other, clearly unrelated pairs ~0.56 (catch-basin vs rat-sighting). 0.70 sits in the gap. A fixed top-N with no floor was rejected because out-of-corpus queries ("noise at night") must select *nothing* rather than the 25 least-bad patterns.
- **What happens if I change it**: raise it → out-of-corpus queries go empty sooner (good) but paraphrases with weak wording lose legitimate patterns (recall drops on aggregations). Lower it → junk patterns leak into aggregation counts and Q1/Q4 numbers inflate.
- **Principled or eyeballed**: **eyeballed** — anchored to two measured similarity pairs, not a tuned curve. Calibrated by inspection, not derived. Golden set is the instrument to fix it.

## Pattern-selection relative window (0.12)

- **What**: patterns within 0.12 of the best-scoring pattern are kept (subject to the floor); anything further behind is dropped.
- **Value / choice**: `PATTERN_REL_WINDOW = 0.12` — [api/retrieval.py:54](api/retrieval.py#L54).
- **Why this and not the alternative**: because bge scores compress, an absolute floor alone can't separate "the query's topic cluster" from "adjacent but different topics" when the whole result set sits above 0.70. The window makes selection relative to the query's own best match. 0.12 ≈ the observed gap between "same topic, different failure mode" (~0.08 apart) and "different domain" (~0.15+ apart) on the drainage anchors.
- **What happens if I change it**: widen → more adjacent-domain patterns enter aggregations (Q2's agency distribution picks up noise). Narrow → multi-pattern topics (drainage has 30 patterns) get truncated and counts undershoot.
- **Principled or eyeballed**: **eyeballed** — the 0.08/0.15 gaps were read off a handful of anchor pairs, not a distribution.

## Domain-consensus demotion factor (0.75)

- **What**: after fusing, a pattern whose C2 `problem_domain` disagrees with the rank-weighted majority of the top-10 fused patterns gets its score multiplied by 0.75 (nudged down, never removed).
- **Value / choice**: literal `0.75` — [api/retrieval.py:203](api/retrieval.py#L203); consensus vote is 1/rank-weighted over the fused top 10 (lines 190–200).
- **Why this and not the alternative**: exists to fix one measured failure: embeddings are blind to negation, so "stormwater not draining" scored "No Water" at 0.719 — *above* real drainage patterns. Hard-filtering non-consensus domains was rejected because legitimately cross-domain queries (Q27 "trash blocking storm drain") must keep both domains. 0.75 was picked as "one RRF rank-step worth of penalty, roughly" and verified to pass the No-Water regression test without breaking the other eight.
- **What happens if I change it**: lower (harsher) → consensus domain monopolizes results; cross-domain queries degrade. Raise toward 1.0 → the No-Water regression returns.
- **Principled or eyeballed**: **eyeballed** — verified against exactly one regression case. The most fragile constant in the system.

## RRF fusion constant (k=60)

- **What**: in Reciprocal Rank Fusion, each leg contributes `1/(60 + rank)` per pattern; the 60 damps how much rank-1 dominates rank-5.
- **Value / choice**: `RRF_K = 60` — [api/retrieval.py:37](api/retrieval.py#L37), used in `rrf_fuse` at line 121.
- **Why this and not the alternative**: 60 is the constant from the original RRF paper (Cormack, Clarke & Buettcher 2009) and the default ES itself uses. No score normalization to tune is the whole point of RRF — BM25 scores and cosine scores live on incomparable scales.
- **What happens if I change it**: lower → head-heavy (rank 1 in one leg can outvote ranks 3+4 in both legs); higher → flatter, more consensus-driven, slower to prefer either leg's top pick.
- **Principled or eyeballed**: **principled** (literature default, adopted deliberately, not tuned further).

## Fusion at PATTERN level, not record level

- **What**: BM25 and kNN rankings are collapsed to distinct text patterns *before* RRF; one representative record per fused pattern is returned.
- **Value / choice**: architecture of `hybrid_search` — [api/retrieval.py:139](api/retrieval.py#L139) onward (BM25 leg uses ES `collapse`, line 163; kNN leg is pattern-native, line 170).
- **Why this and not the alternative**: the corpus is 766K records built from only 337 distinct texts (311 complaints are dropdown selections, not free text). Records sharing a pattern share an identical vector and identical BM25 score, so the two legs almost never agree on *record IDs* even when they fully agree on *patterns* — record-level RRF degrades to whichever leg's duplicates flood the depth. Fusion must happen at the granularity where the two signals are actually comparable.
- **What happens if I change it**: revert to record-level → fusion quality collapses silently (this was the shipped C6 v1; it passed its tests and was still wrong — only the C7 eval exposed it).
- **Principled or eyeballed**: **principled** — forced by measured corpus structure (337 distinct texts, head pattern = 67,903 records).

## kNN leg searches the 337-pattern index, not the record index

- **What**: the semantic leg runs against `nyc311_patterns` (337 docs, one per pattern) instead of the 766K record index; records are fetched afterwards *with* geo/time filters applied.
- **Value / choice**: [api/retrieval.py:168-171](api/retrieval.py#L168) (leg), materialization loop right after; index built in [indexer/build_index.py](indexer/build_index.py) (`PATTERNS_MAPPING`).
- **Why this and not the alternative**: C7's first recall run measured **0.70 mean / 0.10 min** vs FAISS exact. Root cause: the head pattern's 67,903 identical vectors fill any reachable k (ES caps k at 10,000) before a second pattern appears. ES `collapse` on the record index doesn't help — collapse dedupes *after* the k-candidate truncation. Searching patterns directly removes the problem class entirely: post-fix recall@10 = **0.9567**. Cost: pattern-level kNN can't take record filters, so filters move to the materialization step (a pattern with zero records under the filters drops out and the next fused pattern takes its slot).
- **What happens if I change it**: revert → recall@10 back to ~0.70; queries near high-volume topics see 1–3 distinct patterns total.
- **Principled or eyeballed**: **principled** — measured before/after with the FAISS baseline.

## Embedding model (bge-small-en-v1.5) and the query prefix rule

- **What**: one pretrained model embeds everything; queries get a magic prefix string prepended, documents don't.
- **Value / choice**: `MODEL_NAME = "BAAI/bge-small-en-v1.5"`, `BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "` — [shared/semantic_contract.py](shared/semantic_contract.py). Query side: [api/embedder.py](api/embedder.py). Document side (no prefix): [embedding/embed.py](embedding/embed.py).
- **Why this and not the alternative**: model fixed by SPEC §2 (384-dim, CPU-friendly, no training allowed). The asymmetric prefix is how bge was *trained* for retrieval: short queries and longer passages get mapped into compatible regions. Same model without the same prefix convention = subtly wrong rankings that fail silently. That's why the contract lives in one shared module both sides import, and why `api/smoke_prefix.py` prints with/without top-10s (measured: 6/10 overlap, shifted scores).
- **What happens if I change it**: prefix documents too, or drop the query prefix → no error anywhere, rankings just quietly worsen. Swap models on one side only → kNN compares vectors from different spaces; results are noise.
- **Principled or eyeballed**: **principled** (model card requirement + measured difference). Known limitation, recorded honestly: the model is English-only, so 中文 queries parse structurally but embed poorly.

## int8 quantization on the record vector index

- **What**: ES stores the HNSW graph's vectors as 8-bit integers instead of 32-bit floats.
- **Value / choice**: `"type": "int8_hnsw"` — [indexer/build_index.py:119](indexer/build_index.py#L119); `m: 16`, `ef_construction: 100` (lines 120–121) are ES defaults, deliberately untouched.
- **Why this and not the alternative**: 766K × 384 float32 ≈ 1.2 GB of hot vector memory; int8 cuts that to ~300 MB, which is what keeps the whole stack inside the 16 GB single-machine budget (SPEC C4 acceptance #3). The accuracy cost is measured, not assumed: recall@10 = 0.9567 vs exact *includes* whatever error int8 adds, and still clears the 0.95 gate. Note the pattern index (337 docs) is float32 — quantizing 337 vectors saves nothing and adds error for free.
- **What happens if I change it**: float32 → ~4× vector memory, marginal recall gain (the residual misses are near-ties, some may flip). int4 → more memory savings, recall risk unmeasured.
- **Principled or eyeballed**: **principled** (memory arithmetic + measured recall downstream).

## Pure-rules query parser, no LLM at serving time

- **What**: natural-language → `{topic, geo, time_range, aggregation}` via regex/lexicons ([api/parser.py](api/parser.py)); the leftover text after stripping recognized phrases becomes the semantic `topic`.
- **Why this and not the alternative**: four stacked reasons. (1) Latency: measured 0.054 ms vs a 500 ms budget; an LLM call is 300–900 ms p95. (2) Cost: §5 DoD requires zero recurring cost. (3) Boundary: SPEC global boundary #1 — the serving path must run on `docker compose up` with no cloud dependency and no API key. (4) The failure mode is already specified: unparsed queries degrade to plain hybrid retrieval (`aggregation=None`), never an error — so the rules only need precision on five closed-world intents, and the fuzzy side (embedding+BM25) absorbs phrasing. Decision made by Archy (Option A) with these arguments on 2026-07-05.
- **What happens if I change it**: add an LLM fallback → paraphrase robustness rises, but the fresh-machine demo breaks without a key and p95 blows the budget on exactly the queries that fall through.
- **Principled or eyeballed**: **principled** (spec constraints + measured gates: 5/5 canonical, 20/20 paraphrases, 0.054 ms).

## Ontology: three facets, no asset_type, hard boundary rules

- **What**: every complaint pattern is remapped to `problem_domain` (single), `failure_mode` (single, only for drainage/sanitation), `agencies_involved` (multi) — [Docs/ontology-v0.1.md](Docs/ontology-v0.1.md), enforced by CHECK constraints in [sql/003_facets.sql](sql/003_facets.sql).
- **Why this and not the alternative**: facet count is derived backwards from the five demo questions, not from taxonomy completeness (ontology design principle #2). `asset_type` was explicitly rejected (principle #3) because asset information already lives in raw `complaint_type` — a fourth facet would duplicate it. The hard boundary rules (HPD indoor WATER LEAK → `other` never `drainage`; hydrants → `water_supply` never `drainage`) exist because lexical overlap on "water" is precisely the trap semantic search falls into — they are the precision guarantees for Q1. Decisions are Archy's (owner), including the v0.1.1 changelog overrides.
- **What happens if I change it**: add facets → more LLM enrichment scope with no question consuming the output. Soften boundary rules → Q1 pollutes with indoor plumbing.
- **Principled or eyeballed**: **principled** as design (question-driven derivation); the individual pattern *mappings* are LLM output pending Archy's 50-sample spot-check — accuracy is asserted (pilot 15/15) but not yet fully audited.

## Postgres = source of truth; ES = derived; alias-swap rebuilds

- **What**: everything in ES can be deleted and rebuilt from Postgres at any time; rebuilds load a timestamped index (`nyc311_<epoch>`) and an atomic alias flip happens only after the doc count matches Postgres exactly.
- **Value / choice**: [indexer/build_index.py](indexer/build_index.py) — count check aborts before `swap_alias`; old indices deleted after the flip.
- **Why this and not the alternative**: one-directional batch sync means no dual-write consistency problem exists at all (SPEC §1.2 — the anti-Instacart argument: this workload is read-heavy append-only, so ES is safe *because* it's disposable). The alias swap means a failed rebuild leaves the live index untouched, and a successful one is atomic — no window where searches see a half-built index.
- **What happens if I change it**: write to ES directly → data exists only in ES, rebuildability lost, the whole architectural defense collapses.
- **Principled or eyeballed**: **principled** (architecture requirement from spec).

## The long tail of smaller constants

| Constant | Where | What it does | Status |
|---|---|---|---|
| `PATTERN_CAP = 25` | [api/retrieval.py:52](api/retrieval.py#L52) | max patterns entering aggregations; bounds the ES terms filter | **eyeballed** — drainage domain has 30 patterns, so 25 truncates the broadest topic slightly; UNKNOWN whether that matters — needs review with golden set |
| `LEG_DEPTH = 100` | [api/retrieval.py:38](api/retrieval.py#L38) | BM25 leg depth before fusion | eyeballed; > 337 total patterns would be pointless, 100 ≈ "deep enough to not truncate any real topic" |
| `KNN_PATTERNS = 50`, `num_candidates: 337` | [api/retrieval.py:45,170](api/retrieval.py#L45) | kNN leg depth; candidates = entire pattern universe, i.e. exhaustive search — this is what makes the leg effectively exact | principled (337 is the whole index) |
| lexical-guarantee tokenizer: tokens `len > 2`, stopword set `_STOP` | [api/retrieval.py:56,91](api/retrieval.py#L56) | forces pattern inclusion when ALL significant query tokens appear in it (exact-term safety net) | eyeballed; the hand-rolled stopword list includes "problems/complaint" — domain words, not linguistics |
| `size: int = 10`, `expand_size: int = 20` | [api/main.py:57,61](api/main.py#L57) | default result count and drill-down page size | eyeballed UI defaults, consequence-free |
| trend: `size: 60, min_doc_count: 30` | [api/aggregations.py:127](api/aggregations.py#L127) | districts considered for growth ranking; volume floor to suppress noise | eyeballed — 59 community boards exist so 60 = "all of them"; 30 records/year ≈ "enough to call a trend", not derived |
| trend: skip series `< 4` months; growth = 2nd-half vs 1st-half | [api/aggregations.py:136-140](api/aggregations.py#L136) | growth metric | eyeballed metric choice; alternatives (linear slope, YoY) rejected for explainability, not accuracy — UNKNOWN which is more robust, needs review |
| co-occurrence: cell `doc_count >= 5`, top `100` cells, patterns `size 15`, baseline `size 300` | [api/aggregations.py:197-199,220,226](api/aggregations.py#L197) | which district×month cells count as "topic concentration"; how many co-occurring patterns to score; citywide baseline coverage | all **eyeballed**; 300 covers 337 patterns so the baseline is ~complete; the `>=5` floor and 100-cell cap are unvalidated |
| `track_total_hits: True` on every total consumed | [api/aggregations.py](api/aggregations.py) (7 sites) | ES silently caps `hits.total` at 10,000 otherwise; Q5 lifts were ~10× wrong before this | principled — fixed a measured bug found in browser verification |
| embedding `--batch-size 512` | [embedding/embed.py:139](embedding/embed.py#L139) | encode batch; also the checkpoint commit interval | eyeballed, low-stakes (337 texts = one batch) |
| indexer `BATCH = 1000` | [indexer/build_index.py:41](indexer/build_index.py#L41) | bulk-load chunk and server-side cursor page | eyeballed; measured throughput 1,280 docs/s was acceptable, never tuned |
| `ignore_above: 512` on keyword subfields | [indexer/build_index.py:53](indexer/build_index.py#L53) | don't index absurdly long strings as keywords | ES convention; longest real pattern ≈ 90 chars, so inert |
| enrichment `max_workers=4`, `max_tokens=16000`, retry ×1 | [enrichment/enrich.py:219,131](enrichment/enrich.py#L219) | API concurrency; output headroom (thinking tokens); one correction attempt before needs_review | eyeballed; retry-once is ontology note 4 (Archy's rule), the numbers around it are not |
| eval `K = 10`, gates `0.95` / `0.8` | [eval/evaluate.py:46-48](eval/evaluate.py#L46) | metric depth and pass thresholds | principled in provenance — set by SPEC §5, not by me; whether 0.95 is the *right* bar is the spec's judgment |
| pool: `BM25_DEPTH=15`, `KNN_DEPTH_PATTERNS=15`, `RANDOM_N=5`, `SEED=311` | [eval/pool.py:31-34](eval/pool.py#L31) | pooled-labeling coverage per system + calibration rows; seed for reproducibility | 15 > the serving depth of 10 so every servable result gets judged (principled); 5 random is eyeballed; seed value is a joke (311) |
| ES JVM heap `-Xms1g -Xmx1g` | [.env.example](.env.example) / compose | ES memory ceiling | eyeballed; held 1.8 GiB container RSS in practice, never stress-tested |

## Open calibration items

Everything below is currently **indefensible on principle** — the golden set
(30 labeled queries, pending Archy) is the instrument for all of them:

1. `PATTERN_ABS_MIN = 0.70` — sweep against labeled relevance; pick the knee of the precision/recall curve.
2. `PATTERN_REL_WINDOW = 0.12` — same sweep, jointly with the floor (they interact).
3. Consensus demotion `0.75` — validated by ONE regression case; needs adversarial queries Q19–Q28 labeled to know whether it helps or hurts on average.
4. `PATTERN_CAP = 25` vs the 30-pattern drainage domain — check whether Q1/Q4 counts change at cap 35.
5. Trend metric (half-vs-half) and `min_doc_count: 30` — no ground truth exists even in the golden set; would need a "known trend" synthetic check. UNKNOWN how to validate cheaply — needs review.
6. Co-occurrence cell floor `>= 5` and 100-cell cap — sensitivity unmeasured.
7. The `_STOP` list in the lexical guarantee — currently hand-rolled; misses will show up as exact-term queries failing to select their pattern (Q23's misspelling variant partially probes this).
8. Chinese-query quality (bge-small-en is English-only) — Q24/Q25 labels will quantify the damage; fix is a model swap (v1.1 discussion, spec-locked for now).
