"""Query embedding for the eval harness — same contract as serving (C5)."""

from shared.semantic_contract import BGE_QUERY_PREFIX, MODEL_NAME

_model = None


def embed_query(text: str) -> list[float]:
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model.encode(BGE_QUERY_PREFIX + text, normalize_embeddings=True).tolist()
