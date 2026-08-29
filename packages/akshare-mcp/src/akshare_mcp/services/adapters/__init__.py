"""External component adapter interfaces.

Provides adapter pattern for optional external component integration:

- mapie_adapter: Conformal prediction (MAPIE)
- experiment_tracker_adapter: Experiment tracking (MLflow)
- data_validation_adapter: Data validation (Great Expectations)

All adapters follow the same pattern:
1. Define an abstract interface
2. Provide a pure-Python builtin implementation
3. Optionally load the external library via try/except
"""
