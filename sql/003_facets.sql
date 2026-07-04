-- C2 semantic enrichment storage. One row per distinct combo (same key as
-- embedding_text_cache.embed_text), fanned out to records via the
-- record_facets view — never per-record LLM calls (ontology rule 1).
--
-- The CHECK constraints duplicate the controlled vocabularies from
-- Docs/ontology-v0.1.md as defense-in-depth: even if the enrichment
-- script's validation regresses, an out-of-vocabulary value cannot land
-- in the system of record. Vocabulary changes require an ontology version
-- bump, so these constraints changing in lockstep is a feature, not a
-- maintenance burden.

CREATE TABLE IF NOT EXISTS combo_facets (
    embed_text          TEXT PRIMARY KEY,
    -- NULL only allowed on needs_review rows (parse/validation failed twice;
    -- awaiting Archy's manual call rather than a silent guess).
    problem_domain      TEXT,
    failure_mode        TEXT,
    agencies_involved   TEXT[],
    -- Model's one-sentence justification; kept to speed up the D3 spot-check.
    rationale           TEXT,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    needs_review        BOOLEAN NOT NULL DEFAULT false,
    review_reason       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT domain_vocab CHECK (
        problem_domain IS NULL OR problem_domain IN
        ('drainage', 'water_supply', 'sanitation', 'street_infrastructure', 'other')
    ),
    CONSTRAINT failure_mode_vocab CHECK (
        failure_mode IS NULL OR failure_mode IN
        ('blockage', 'overflow', 'structural_damage', 'odor', 'debris', 'service_gap')
    ),
    -- failure_mode exists ONLY for drainage/sanitation (ontology facet 2).
    CONSTRAINT failure_mode_domain CHECK (
        failure_mode IS NULL OR problem_domain IN ('drainage', 'sanitation')
    ),
    CONSTRAINT agencies_vocab CHECK (
        agencies_involved IS NULL OR agencies_involved <@
        ARRAY['DSNY', 'DEP', 'DOT', 'HPD', 'NYPD', 'DOHMH', 'DPR']
    ),
    CONSTRAINT reviewed_rows_complete CHECK (
        needs_review OR (problem_domain IS NOT NULL AND agencies_involved IS NOT NULL
                         AND array_length(agencies_involved, 1) >= 1)
    )
);

-- Fan-out to records, computed at query time (C4 reads this when building
-- the ES index). Uses the same canonical text construction as C3's
-- embedding pipeline so all three derived layers key off identical strings.
CREATE OR REPLACE VIEW record_facets AS
SELECT
    r.unique_key,
    f.problem_domain,
    f.failure_mode,
    f.agencies_involved,
    f.needs_review
FROM raw_311_requests r
JOIN combo_facets f
  ON f.embed_text = concat_ws('. ',
        nullif(trim(r.complaint_type), ''),
        nullif(trim(r.descriptor), ''),
        nullif(trim(r.additional_details), '')
     );
