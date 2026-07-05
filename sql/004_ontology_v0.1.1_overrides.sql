-- Ontology v0.1.1 manual overrides (see Docs/ontology-v0.1.md changelog).
-- combo_facets is derived data: a full rebuild re-runs enrichment and MUST
-- re-apply this file afterwards. Idempotent.
--
-- Decision 2 (Archy, 2026-07-04): "Catch Basin Search" is a request to
-- locate a buried/paved-over basin — the asset is effectively damaged, not
-- clogged, and no DSNY litter chain is involved.
UPDATE combo_facets
SET failure_mode = 'structural_damage',
    agencies_involved = ARRAY['DEP', 'DOT'],
    rationale = '[manual override, ontology v0.1.1] Basin buried/paved-over: '
                || 'DEP owns the asset, DOT owns the roadway surface concealing it.',
    prompt_version = 'v0.1.1'
WHERE embed_text = 'Sewer. Catch Basin Search (SC2). SC2';
