# MWI Progress Data Center

This folder is the project progress record and decision log for the MEDA weather-data work.

## Rules

1. Every completed stage gets a dated report with scope, source evidence, validation results, decisions, open questions, and next actions.
2. A documented decision must exist before a transformation, filter, exclusion, aggregation, or canonical-variable choice is applied to the dataset.
3. Keep source observations, calibrated products, derived products, and project-normalized outputs distinguishable.
4. Missing values, blank fields, sentinels, and failed quality conditions are separate states. Never silently coerce one into another.
5. A source-defined flag meaning does not automatically imply a project filtering rule. Record its semantics first; approve downstream handling separately.
6. Reports must state the exact dataset scope and input-file hashes so results can be reproduced.

## Current reports

- `MEDA_SCIENTIFIC_VALIDATION_QUALITY_FLAG_MAPPING_V1.md` — first source-backed scientific validation checkpoint and decisions.
- `meda_quality_flag_mapping_v1.json` — machine-readable flag dictionary; semantics are source-backed, application policies remain pending.
- `meda_source_flag_counts_v1.json` — counts re-computed from the uploaded source CSV products for Sols 230–239.

## Current status

**In progress — source definitions and quality-flag semantics mapped; no quality-based filtering or normalized-data rewrite has been applied yet.**
