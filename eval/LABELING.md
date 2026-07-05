# Golden-set labeling protocol (v0, 30 queries)

## What you're judging

Each row pairs one query with one **complaint pattern** (a distinct
complaint_type / descriptor / details combination — not an individual
record). Judge: *if a chief data scientist searched this query, is a
record of this pattern something they'd want back?*

Fill the `label` column:

| label | meaning |
|---|---|
| `1` | relevant — this pattern answers the query |
| `0` | not relevant |

Binary only. **When in doubt, 0** — a strict golden set keeps precision@10
honest. Blank labels are treated as unjudged and excluded (not assumed 0).

## Ground rules (agreed C7 constraints)

- **Judge the text, not the facets.** The ontology's problem_domain /
  agencies are system OUTPUT under evaluation — using them as the
  relevance signal would be circular. They are deliberately absent from
  the sheet.
- **Candidate source is hidden.** Rows are pooled from BM25 top-15, kNN
  top-15, the serving hybrid top-10, and 5 random patterns, shuffled per
  query. The random rows calibrate your baseline (they should usually be
  0); which system suggested a row must not influence the judgment.
- Adversarial queries deserve extra care — e.g. for Q20 ("water leak from
  ceiling") the HPD indoor-leak patterns are the RELEVANT ones and
  drainage patterns are 0; for Q29 (noise) likely everything is 0. Judge
  intent, not word overlap.

## Mechanics

1. Open `Data/golden_labeling_sheet_v0.csv` (UTF-8) in Excel/Sheets.
2. Fill `label` for every row (~650 rows; the pattern texts repeat across
   queries, so it goes faster than it looks).
3. Save as `eval/golden_v0.csv` (same columns) and tell Claude Code —
   the labeled file is committed to the repo; the report script picks it
   up automatically.

Edit `eval/queries_v0.csv` first if you want to add/drop queries (your
call per the C7 agreement), then ask for the sheet to be regenerated.
