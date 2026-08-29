"""Factor Mining provider interface - defines what host must provide."""

from __future__ import annotations

from typing import Any, Protocol


class FactorMiningProvider(Protocol):
    """Provider interface for factor mining runtime capabilities.

    Host process (e.g., akshare-mcp) must implement this interface to provide
    concrete engine scheduling, evolution, validation, and persistence.
    """

    async def get_db(self) -> Any:
        """Return initialized database connection."""
        ...

    async def ensure_persistent_pool(self, db: Any) -> None:
        """Ensure factor pool tables exist and load active pool from DB."""
        ...

    async def build_mining_context(
        self,
        db: Any,
        *,
        codes: list[str] | None = None,
    ) -> Any:
        """Build mining context with validation universe and metadata."""
        ...

    async def search_candidates(
        self,
        context: Any,
        *,
        engines: list[str] | None = None,
        candidate_count: int = 30,
    ) -> list[Any]:
        """Search for raw factor candidates using configured engines."""
        ...

    async def evolve_candidates(
        self,
        candidates: list[Any],
        context: Any,
        *,
        generations: int = 5,
        ic_evaluator: Any = None,
    ) -> list[Any]:
        """Evolve candidates through evolutionary optimization."""
        ...

    async def quick_filter_candidates(
        self,
        candidates: list[Any],
        context: Any,
    ) -> list[Any]:
        """Apply quick evidence filters to candidates."""
        ...

    async def validate_batch(
        self,
        db: Any,
        candidates: list[Any],
        context: Any,
    ) -> list[Any]:
        """Run full validation on candidate batch."""
        ...

    async def admit_batch(
        self,
        validated: list[Any],
    ) -> list[Any]:
        """Admit validated factors to active pool."""
        ...

    async def persist_admitted_factors(
        self,
        db: Any,
        admitted: list[Any],
    ) -> None:
        """Persist admitted factors to database."""
        ...

    async def persist_mining_run(
        self,
        db: Any,
        report: dict[str, Any],
    ) -> None:
        """Persist mining run report to database."""
        ...

    async def reappraise_quarantine_factors(
        self,
        db: Any,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Reappraise factors in quarantine for potential promotion."""
        ...

    async def record_feedback(
        self,
        run_id: str,
        raw_candidates: list[Any],
        evolved: list[Any],
        validated: list[Any],
        admitted: list[Any],
    ) -> None:
        """Record quality feedback for engine tuning."""
        ...

    def get_active_pool_size(self) -> int:
        """Get current active factor pool size."""
        ...

    def get_last_engines_used(self) -> list[str]:
        """Get list of engines used in last search."""
        ...

    def install_quick_evidence_evaluators(
        self,
        db: Any,
        context: Any,
    ) -> Any:
        """Install quick IC evaluators for evolution."""
        ...

    # --- maintenance ----------------------------------------------------------
    # These closed the one orchestration path that had zero protocol coverage:
    # ``FactorMiningRuntime.run_maintenance`` previously reached through seven
    # private attributes on the support object (``_decay_monitor``,
    # ``_persist_decay_report``, ``_persist_decay_updates``,
    # ``_promote_quarantine_factors``, ``_run_qc_pipeline``, ``_active_pool``),
    # which meant the maintenance cycle could not move without the host.

    async def run_decay_check(self, db: Any) -> dict[str, Any]:
        """Run the daily factor-decay check and return its report."""
        ...

    async def persist_decay_report(self, db: Any, report: dict[str, Any]) -> None:
        """Persist the decay report."""
        ...

    async def persist_decay_updates(self, db: Any, report: dict[str, Any]) -> None:
        """Apply the decay report's per-factor updates."""
        ...

    async def promote_quarantine_factors(self, db: Any) -> dict[str, Any]:
        """Promote quarantined factors that now satisfy the strict gate."""
        ...

    async def run_qc_pipeline(self, db: Any) -> dict[str, Any]:
        """Run the QC pipeline and return shelf decisions."""
        ...

    # --- QC / validation seams ------------------------------------------------
    # Admission and shelf decisions are Strategy Factory-owned, but the
    # statistics behind them are host compute. These keep that split explicit
    # instead of letting the decision code import host tools directly.

    async def run_factor_oos_validation(
        self,
        db: Any,
        factor: dict[str, Any],
        codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Out-of-sample validation for one factor."""
        ...

    async def run_factor_group_backtest(
        self,
        db: Any,
        factor: dict[str, Any],
        codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Grouped backtest for one factor."""
        ...

    async def run_factor_robustness_check(
        self,
        db: Any,
        factor: dict[str, Any],
        codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Robustness check for one factor."""
        ...

    async def validate_factor_candidate(
        self,
        db: Any,
        candidate: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run the strict validation pipeline for a single candidate."""
        ...

    def compile_factor_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Compile a candidate expression; admission is gated on success."""
        ...

    # --- pool / gateway seams -------------------------------------------------

    async def load_factor_pool(
        self,
        db: Any,
        *,
        statuses: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Load persisted factor-pool rows."""
        ...

    def optimize_factor_weights(
        self,
        metrics: list[dict[str, Any]],
        *,
        method: str = "max_sharpe",
    ) -> dict[str, Any]:
        """Compute portfolio weights over factor metrics."""
        ...

    async def write_factor_performance(
        self,
        db: Any,
        payload: dict[str, Any],
    ) -> None:
        """Record realized factor performance."""
        ...

    def get_factor_metrics(self, factor_ids: list[str]) -> dict[str, Any]:
        """Metrics for the given factors.

        Replaces reaching into ``factory._active_pool._factors`` — a two-level
        private access that made the gateway untestable without the host.
        """
        ...

    def get_factor_llm_provider(self) -> Any:
        """Return the LLM provider used for factor generation, if configured."""
        ...
