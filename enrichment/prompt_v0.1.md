<!--
Prompt version: v0.1 — tracks Docs/ontology-v0.1.md (locked for W0).
Changes here require an ontology sign-off from Archy and a version bump;
the version string is stored on every combo_facets row for reproducibility.
-->

You are classifying NYC 311 complaint-type patterns onto a fixed multi-dimensional ontology. Each input is one distinct (complaint_type, descriptor, additional_details) combination from the NYC 311 taxonomy, plus the agency the raw 311 system assigned it to. Your output re-aggregates the 311 taxonomy — it must NOT simply mirror it.

## Facet 1 · problem_domain (exactly one, required)

- `drainage` — water that should leave the street/system but cannot: sewers, catch basins, street/highway flooding, culverts, manholes, rain gardens. INCLUDES DPR's Root/Sewer/Sidewalk combos where tree roots affect sewers (a deliberate cross-agency call).
- `water_supply` — water delivery infrastructure: hydrants, water mains, water quality, water pressure, meters. Supply-side, never removal-side.
- `sanitation` — solid waste and street cleanliness: dirty conditions, illegal dumping, missed collections, derelict vehicles, litter baskets.
- `street_infrastructure` — roadway and sidewalk physical condition: potholes, sidewalk defects, street signs/markings, curbs.
- `other` — everything else. EXPLICITLY includes: HPD indoor WATER LEAK (wall/ceiling), UNSANITARY CONDITION (mold/pests), DOHMH rodents, NYPD abandoned vehicles, parks maintenance.

HARD BOUNDARY RULES (violating these is the worst possible error):
1. HPD `WATER LEAK` (at wall/ceiling) → `other`, NEVER `drainage`. It is indoor plumbing. Do not be baited by the word "water".
2. Hydrant / water-main combos → `water_supply`, NEVER `drainage`. Supply ≠ removal.
3. DPR `Root/Sewer/Sidewalk Condition` combos that affect sewers → `drainage`, despite the DPR agency tag. (But "Trees and Sidewalks Program / Free Repair" is sidewalk repair, not drainage.)

## Facet 2 · failure_mode (exactly one IF problem_domain is drainage or sanitation; null otherwise — even if a mode seems to fit)

- `blockage` — flow path physically obstructed (catch basin clogged, culvert blocked)
- `overflow` — contents escaping the system (sewer backup, manhole overflow, street flooding)
- `structural_damage` — asset broken/sunken/missing (basin sunken, manhole cover missing)
- `odor` — smell without confirmed physical failure
- `debris` — waste accumulation in/around public space (dumping, litter, dirty conditions)
- `service_gap` — expected service not delivered (missed collection)

## Facet 3 · agencies_involved (one or more, required)

Answer this question: which agencies does this PHYSICAL PROBLEM actually implicate — not just who the 311 system routed it to? Vocabulary: DSNY, DEP, DOT, HPD, NYPD, DOHMH, DPR.

- Normally include the raw assigned agency, then extend along the physical causal chain.
- Canonical example: catch basin clogged/flooding → [DEP, DSNY, DOT]. DEP owns the basin, DSNY owns the street litter that clogs it, DOT owns the flooded roadway surface.
- Counter-example: rat sighting → [DOHMH] alone is correct. DO NOT inflate the set to look thorough. Add an agency only when the physical causal chain genuinely touches its jurisdiction. Over-tagging is as wrong as under-tagging.

## Output

Return JSON with: problem_domain, failure_mode (null unless drainage/sanitation), agencies_involved (array), rationale (ONE short sentence justifying the agency set — mention the causal chain only if multi-agency).
