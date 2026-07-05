"""C6 hybrid retrieval: BM25 + kNN fused with RRF, filters pushed down.

## Why client-side RRF (interview narrative)

Reciprocal Rank Fusion: fused(d) = Σ_legs 1/(K + rank_leg(d)), K=60.
Rank-based fusion is the standard answer to "BM25 scores and cosine scores
live on incomparable scales" — no score normalization to tune, and a doc
ranked well by BOTH legs beats a doc ranked brilliantly by one. K=60 is the
constant from the original Cormack/Clarke/Buettcher paper; it damps the
head so rank 1 vs rank 3 isn't a blowout. We fuse in Python rather than
using ES's native rrf retriever because (a) native RRF needs a paid
license and this stack must run on the free tier anywhere, and (b) the
fusion logic stays inspectable — ?explain=true can show each leg's ranks.

## Why pattern-level semantic selection for aggregations

Aggregation questions (Q1-Q5) need the FULL matching record set, not a
top-k list — but kNN can't tag 100K+ records as "relevant". The corpus
structure solves this: all 766K records collapse onto ~337 distinct text
patterns (C3's dedup insight). So we score the query against 337 pattern
vectors in memory (~microseconds), select the relevant patterns, and push
them down to ES as an exact `full_text.raw` terms filter. Semantic
selection at the pattern level, exact counting at the record level.

## Filter push-down

geo/time from the parsed query become ES `filter` clauses applied to BOTH
legs (bool.filter for BM25, knn.filter for kNN) and to every aggregation —
filtering happens before scoring, not after.
"""

import os

import numpy as np
import psycopg

RRF_K = 60
LEG_DEPTH = 100          # BM25 leg depth (collapsed to distinct patterns by ES)
# The kNN leg dedupes to patterns in Python, and head patterns own thousands
# of records each — k must be deep or the leg surfaces only 2-3 distinct
# patterns and fusion starves. 1000 records ~ top 15-30 patterns.
KNN_DEPTH = 1000

# Pattern selection: keep patterns within REL_WINDOW of the best cosine,
# subject to an absolute floor and a cap. Calibration points from C3:
# related drainage patterns ~0.79-0.88, unrelated ~0.56. C7's golden-set
# evaluation is the instrument for tuning these.
PATTERN_CAP = 25
PATTERN_ABS_MIN = 0.70
PATTERN_REL_WINDOW = 0.12

_STOP = {"the", "a", "an", "in", "for", "of", "with", "and", "or", "not",
         "is", "are", "problems", "problem", "complaint", "complaints"}


class PatternStore:
    """The 337 distinct patterns + vectors + facets, loaded once at startup."""

    def __init__(self):
        conn = psycopg.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "semanticlens"),
            user=os.environ.get("POSTGRES_USER", "semanticlens"),
            password=os.environ.get("POSTGRES_PASSWORD", "changeme"),
        )
        rows = conn.execute("""
            SELECT c.embed_text, c.embedding, f.problem_domain, f.agencies_involved
            FROM embedding_text_cache c
            LEFT JOIN combo_facets f ON f.embed_text = c.embed_text
        """).fetchall()
        conn.close()
        self.texts = [r[0] for r in rows]
        self.matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        self.domain = {r[0]: r[2] for r in rows}
        self.agencies = {r[0]: r[3] or [] for r in rows}

    def select(self, query_vec, query_text):
        """Patterns relevant to the topic, with the reason each was kept."""
        sims = self.matrix @ np.asarray(query_vec, dtype=np.float32)
        best = float(sims.max())
        floor = max(PATTERN_ABS_MIN, best - PATTERN_REL_WINDOW)
        order = np.argsort(-sims)

        # Lexical guarantee: exact-term queries must select their pattern
        # even if cosine is off — every significant query token present.
        tokens = [t for t in query_text.lower().split() if len(t) > 2 and t not in _STOP]

        selected = []
        for i in order:
            sim = float(sims[i])
            text_l = self.texts[i].lower()
            lexical = bool(tokens) and all(t in text_l for t in tokens)
            if sim >= floor or lexical:
                selected.append({"pattern": self.texts[i], "sim": round(sim, 4),
                                 "via": "lexical" if (lexical and sim < floor) else "vector"})
            if len(selected) >= PATTERN_CAP:
                break
        return selected


def filters_from(parsed):
    """geo/time -> ES filter clauses (pushed down, pre-scoring)."""
    clauses = []
    if parsed.geo.borough:
        clauses.append({"term": {"borough": parsed.geo.borough}})
    if parsed.time_range and (parsed.time_range.gte or parsed.time_range.lte):
        rng = {}
        if parsed.time_range.gte:
            rng["gte"] = parsed.time_range.gte
        if parsed.time_range.lte:
            rng["lte"] = parsed.time_range.lte
        clauses.append({"range": {"created_date": rng}})
    return clauses


def rrf_fuse(*rankings, k=RRF_K):
    """rankings: lists of doc ids in rank order -> [(id, fused_score)] desc."""
    scores = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


SOURCE_FIELDS = ["unique_key", "created_date", "complaint_type", "descriptor",
                 "additional_details", "borough", "community_board", "agency",
                 "problem_domain", "failure_mode", "agencies_involved", "status"]


def hybrid_search(es, index, topic, query_vec, filters, size=10, explain=False,
                  pattern_store=None):
    """BM25 + kNN fused with RRF **at the pattern level**.

    Why pattern-level, not record-level: thousands of records share each of
    the ~337 text patterns, so the two legs almost never agree on record
    IDs even when they fully agree on patterns — record-level RRF degrades
    to whichever leg's duplicates flood the depth. Both legs are therefore
    collapsed to distinct patterns first (BM25 via ES collapse, kNN via
    first-seen dedup), fused where the rankings are actually comparable,
    and one representative record per fused pattern is returned — which
    also de-duplicates the result list for free.

    Optional domain-consensus rerank (needs pattern_store): embeddings are
    weak on negation ("not draining" ~ "No Water"), so patterns whose C2
    problem_domain disagrees with the rank-weighted majority of the fused
    top get nudged down. The ontology feeding back into retrieval quality.
    """
    bm25_body = {
        "query": {"bool": {
            "must": [{"multi_match": {
                "query": topic,
                "fields": ["full_text^2", "complaint_type", "descriptor",
                           "additional_details"],
            }}],
            "filter": filters,
        }},
        "collapse": {"field": "full_text.raw"},
        "size": LEG_DEPTH, "_source": False,
        "fields": ["full_text.raw"],
    }
    knn_body = {
        "knn": {"field": "embedding", "query_vector": query_vec,
                "k": KNN_DEPTH, "num_candidates": KNN_DEPTH * 2,
                "filter": filters},
        "size": KNN_DEPTH, "_source": False,
        "fields": ["full_text.raw"],
    }
    resp = es.msearch(searches=[
        {"index": index}, bm25_body,
        {"index": index}, knn_body,
    ])

    def pattern_ranking(hits):
        """(ordered distinct patterns, pattern -> representative record id)"""
        order, rep = [], {}
        for h in hits:
            pattern = h["fields"]["full_text.raw"][0]
            if pattern not in rep:
                rep[pattern] = h["_id"]
                order.append(pattern)
        return order, rep

    bm25_patterns, bm25_rep = pattern_ranking(resp["responses"][0]["hits"]["hits"])
    knn_patterns, knn_rep = pattern_ranking(resp["responses"][1]["hits"]["hits"])

    fused = rrf_fuse(bm25_patterns, knn_patterns)
    if not fused:
        return [], None

    consensus_domain = None
    if pattern_store is not None and len(fused) >= 3:
        # Rank-weighted domain vote over the fused head; nudge dissenters
        # down without removing them (a prior, not a filter).
        votes = {}
        for rank, (pattern, _) in enumerate(fused[:10], start=1):
            domain = pattern_store.domain.get(pattern)
            if domain:
                votes[domain] = votes.get(domain, 0.0) + 1.0 / rank
        if votes:
            consensus_domain = max(votes, key=votes.get)
            fused = sorted(
                ((p, s * (1.0 if pattern_store.domain.get(p) == consensus_domain
                          else 0.75))
                 for p, s in fused),
                key=lambda kv: -kv[1],
            )

    fused = fused[:size]
    # Representative record per pattern: BM25's collapse top hit, else kNN's.
    ids = [bm25_rep.get(p) or knn_rep[p] for p, _ in fused]
    docs = es.mget(index=index, ids=ids, source_includes=SOURCE_FIELDS)
    by_id = {d["_id"]: d["_source"] for d in docs["docs"] if d.get("found")}
    hits = [
        {"rrf_score": round(score, 5), **by_id[doc_id]}
        for (_, score), doc_id in zip(fused, ids) if doc_id in by_id
    ]

    debug = None
    if explain:
        bm25_rank = {p: r for r, p in enumerate(bm25_patterns, 1)}
        knn_rank = {p: r for r, p in enumerate(knn_patterns, 1)}
        debug = {
            "rrf_k": RRF_K, "leg_depth": LEG_DEPTH,
            "fusion_granularity": "pattern",
            "consensus_domain": consensus_domain,
            "per_hit_leg_ranks": [
                {"pattern": p, "bm25_rank": bm25_rank.get(p),
                 "knn_rank": knn_rank.get(p),
                 "domain": pattern_store.domain.get(p) if pattern_store else None}
                for p, _ in fused
            ],
        }
    return hits, debug
