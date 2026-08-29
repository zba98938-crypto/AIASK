# Factor Engines, Validation, QC, And Pool

## Engines And Candidate Generation

Engine-related code lives under `services/factor_mining_factory/engines/` and adjacent orchestration modules. Preserve engine identity, timeout/degradation behavior, and result metadata when changing search.

Do not let LLM-generated factors bypass validation or persist directly to the active pool.

## Validation And QC

Current validation concerns include:

- Candidate schema and expression safety.
- IC calculation and optional neutralization.
- Cross-section degrees-of-freedom guards.
- Forward-horizon evidence.
- Regime/profile coverage.
- Catalog registration and provenance.
- Degraded but explainable behavior when style/regime inputs are missing.

The QC pipeline should produce actionable rejection/degradation reasons rather than silent discard.

## Active Pool Governance

The active pool is persistent and should maintain:

- Factor identity and family.
- Regime/profile applicability.
- Validation and freshness metadata.
- Decay/maintenance state.
- Admission/rejection reasons.

When pool outputs feed Strategy Factory summaries, preserve compactness and avoid raw payload bloat.
