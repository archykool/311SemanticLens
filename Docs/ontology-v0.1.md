# Rechannel Ontology v0.1.1

Status: **locked for W0** (owner: Archy; changes require Archy's sign-off, not Claude Code's discretion)
Applies to: the 337 distinct (complaint_type × descriptor × additional_details) combos in `embedding_text_cache`
Consumed by: C2 enrichment (LLM batch mapping), C4 index fields, C5/C6 retrieval & aggregation

---

## Design principles (do not re-litigate during implementation)

1. **Enrichment operates on the 337 distinct combos, never per-record.** Facet labels fan out to all 765,728 rows via SQL join, exactly like C3's embedding cache.
2. **This ontology is not a replica of the 311 taxonomy — it is a re-aggregation layered on top of it.** The 951 sub-categories are the *input*; these three facets are the *output*. If a facet ever degenerates into a 1:1 copy of complaint_type, it has lost its purpose.
3. **No asset_type facet** (decided). Asset information stays in the raw `complaint_type` field; do not invent a fourth facet.
4. **Controlled vocabulary only.** The LLM must choose from the value tables below. No free-form values, no synonyms ("storm_water", "drainage_issue" etc. are invalid).

---

## Facet 1 · problem_domain (single-valued, required for all 337 combos)

| value | definition | inclusion anchors (raw combos) |
|---|---|---|
| `drainage` | Water that should leave the street/system but cannot: sewers, catch basins, street flooding, culverts, manholes. **Includes** DPR's Root/Sewer/Sidewalk (tree roots breaking sewer laterals). | Sewer Backup (SA), Catch Basin Clogged/Flooding (SC), Street Flooding (SJ), Catch Basin Sunken/Damaged (SC1), Catch Basin Search (SC2), Manhole Overflow (SA1), Manhole Cover Missing (SA3), Sewer Odor (SA2), Culvert Blocked (SE), Rain Garden Debris (GIRGD), Root/Sewer/Sidewalk Condition (DPR) |
| `water_supply` | Water delivery infrastructure: hydrants, water mains, water quality, water pressure, meters. **Deliberately separate from drainage** — supply-side, not removal-side. | Hydrant Running (WC3/WA4/FHE/WC1/WC2/WC), Possible Water Main Break (WA1), Dirty Water (WE), No Water (WNW), Low Water Pressure (WLWP), Water Meter combos, Illegal Use of Hydrant (CIN) |
| `sanitation` | Solid waste and street cleanliness. | Dirty Condition (all descriptors), Illegal Dumping, Missed Collection (Trash/Compost/Recycling), Derelict Vehicles, Litter Basket combos |
| `street_infrastructure` | Roadway and sidewalk physical condition. | Street Condition/Pothole, sidewalk defects, street sign/marking combos (DOT) |
| `other` | Everything else. **Explicitly includes HPD indoor WATER LEAK (wall/ceiling), UNSANITARY CONDITION (mold/pests), DOHMH rodents, NYPD abandoned vehicles, parks maintenance.** | — |

### Hard boundary rules (Q1 precision depends on these)

- **HPD `WATER LEAK` (at wall/ceiling) → `other`, never `drainage`.** It is indoor plumbing, not street drainage. Lexical overlap on "water" is exactly the trap this ontology exists to avoid.
- **Hydrant/water-main combos → `water_supply`, never `drainage`.** Supply ≠ removal.
- **DPR `Root/Sewer/Sidewalk Condition` → `drainage`** despite being agency-tagged DPR. This is a deliberate cross-agency call and a talking point for the demo. *(v0.1.1: applies to the sewer-affecting combos only — "Roots Affecting Sewer" → `drainage`; "Roots Affecting Foundation" → `other` (building issue, not street drainage); "Trees and Sidewalks Program / Free Repair" → `street_infrastructure` (sidewalk repair program).)*

## Facet 2 · failure_mode (single-valued; ONLY for combos where problem_domain ∈ {drainage, sanitation}; NULL for all other domains — decided, do not backfill)

| value | definition | anchors |
|---|---|---|
| `blockage` | Flow path physically obstructed | Catch Basin Clogged, Culvert Blocked/Needs Cleaning |
| `overflow` | Contents escaping the system | Sewer Backup, Manhole Overflow, Street Flooding |
| `structural_damage` | Asset broken/sunken/missing | Catch Basin Sunken/Damaged/Raised, Manhole Cover Missing, Catch Basin Search (v0.1.1: basin buried/paved-over/unlocatable) |
| `odor` | Smell without confirmed physical failure | Sewer Odor |
| `debris` | Waste accumulation in/around public space | Illegal Dumping, Dirty Condition (Trash), Litter Basket overflow |
| `service_gap` | Expected service not delivered | Missed Collection (Trash/Compost/Recycling) |

## Facet 3 · agencies_involved (multi-valued, required for all 337 combos) — the killer feature

This is a **judgment output**, not a lookup of the raw `agencies` column. For each combo, answer: *which agencies does this physical problem actually implicate?* The raw single-agency assignment is the baseline being improved upon.

- Vocabulary: `DSNY, DEP, DOT, HPD, NYPD, DOHMH, DPR` (Phase 1's seven agencies only).
- The raw assigned agency is normally *included* in the set, then extended.
- Canonical example (spot-check anchor): **`Sewer. Catch Basin Clogged/Flooding` → `[DEP, DSNY, DOT]`** — DEP owns the basin, DSNY owns the street litter that clogs it, DOT owns the flooded roadway surface.
- Counter-example: `Rodent. Rat Sighting` → `[DOHMH]` alone is fine. **Do not inflate sets to look impressive; only add an agency when the physical causal chain touches its jurisdiction.** Over-tagging destroys Q2's credibility as surely as under-tagging.

---

## Enrichment execution notes (for Claude Code)

1. Input: `SELECT embed_text, complaint_type, descriptor, additional_details, record_count, agencies FROM embedding_text_cache` (337 rows).
2. One LLM call per combo (or small batches), constrained to the vocabularies above; output written to a `combo_facets` table keyed by embed_text, then fanned out to records via join. Store the model name + prompt version alongside (reproducibility).
3. Cost guardrail: 337 calls is well under the $20 budget; do not switch to per-record calls under any circumstance.
4. Parse/validation: reject any output value not in the vocabulary; retry once; surviving failures go to a `needs_review` list for Archy instead of being silently guessed.

## Changelog

- **v0.1.1** (2026-07-04, Archy):
  1. Accepted the model's three-way split of the DPR Root/Sewer/Sidewalk family
     (sewer→`drainage`, foundation→`other`, sidewalks-program→`street_infrastructure`);
     hard boundary rule 3 amended accordingly.
  2. `Sewer. Catch Basin Search (SC2)` overridden: `failure_mode` blockage →
     `structural_damage` (a search request means the basin is buried/paved-over,
     not clogged), `agencies_involved` [DEP,DSNY,DOT] → **[DEP, DOT]** (no litter
     causal chain). Applied as a data override in `sql/004_ontology_v0.1.1_overrides.sql`
     — must be re-applied after any enrichment rebuild.
- **v0.1** (2026-07-02, Archy): initial locked version for W0.

## Spot-check protocol (Archy, D3 — non-outsourceable)

- Sample 50 of 337 (~15% coverage). **Oversample the drainage domain and every catch-basin combo** (Q2's killer feature lives or dies here).
- Must-pass anchors: Catch Basin Clogged → domain=drainage, mode=blockage, agencies ⊇ {DEP, DSNY}; HPD WATER LEAK → domain=other; Hydrant Running → domain=water_supply.
- Acceptance (v0): ≥ 90% of sampled combos correct on problem_domain; 100% correct on the hard boundary rules; agencies_involved judged reasonable (no inflation) on all drainage samples.
