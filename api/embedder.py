"""Query-side embedding for C5/C6.

Same model as C3's document embedding (shared/semantic_contract.py), with
the bge query prefix prepended — documents were encoded WITHOUT it, queries
MUST be encoded WITH it. Run api/smoke_prefix.py to see the retrieval
difference live.
"""

from shared.semantic_contract import BGE_QUERY_PREFIX, MODEL_NAME

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def embed_query(text: str, with_prefix: bool = True) -> list[float]:
    """with_prefix=False exists ONLY for the smoke test — never serve with it."""
    payload = (BGE_QUERY_PREFIX + text) if with_prefix else text
    # Normalized to unit length: the index uses dot_product similarity,
    # which equals cosine only on unit vectors (same invariant as C3).
    return _get_model().encode(payload, normalize_embeddings=True).tolist()
