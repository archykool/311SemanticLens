"""The document/query text contract — single source of truth.

C3 (document embedding) and C5 (query embedding) MUST agree on all of this
or kNN compares vectors from different spaces and silently returns garbage
(SPEC C3 boundary: same model, same vector space). Import from here; never
inline copies.
"""

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384

# bge-small-en-v1.5 asymmetric-retrieval convention: queries get this prefix,
# documents do NOT. C5 prepends it before encoding user queries; C3 encodes
# raw document text. See api/smoke_prefix.py for the live proof.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Canonical document-text construction, used by C3 (what gets embedded),
# C2 (facet join key), and C4 (the full_text field + facet fan-out).
EMBED_TEXT_SQL = """
concat_ws('. ',
    nullif(trim(complaint_type), ''),
    nullif(trim(descriptor), ''),
    nullif(trim(additional_details), '')
)
""".strip()
