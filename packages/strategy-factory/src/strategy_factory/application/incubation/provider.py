"""Provider abstraction for incubation runtime support services."""

from __future__ import annotations

from typing import Any, Protocol


class IncubationRuntimeSupport(Protocol):
    """Protocol defining the support services required by incubation orchestrator.

    This protocol abstracts the concrete implementation details (intake, signal
    generation, forward verification, metrics recording, etc.) as provider
    contracts, allowing Strategy Factory to own the orchestration logic while
    delegating the actual implementation to host-provided services.
    """

    async def get_db(self) -> Any:
        """Return an initialized database connection.

        Public counterpart to the legacy ``_get_db``. Declaring it here removes
        the last private reach-through from the incubation orchestration path.
        """
        ...

    async def scan_and_accept_strategies(self, db: Any) -> dict[str, Any]:
        """Scan for new strategies and accept them into incubation.

        Returns:
            dict with keys: accepted_count, rejected_count, intake_summary
        """
        ...

    async def list_incubating_strategies(self, db: Any, limit: int = 100) -> list[dict[str, Any]]:
        """List all strategies currently in incubation stage."""
        ...

    async def list_paper_observation_strategies(self, db: Any, limit: int = 100) -> list[dict[str, Any]]:
        """List all strategies in paper (warmup) observation stage."""
        ...

    async def list_diagnostic_observation_strategies(self, db: Any, limit: int = 50) -> list[dict[str, Any]]:
        """List all strategies in diagnostic observation stage."""
        ...

    async def generate_signals(self, db: Any, strategy: dict[str, Any]) -> dict[str, Any]:
        """Generate trading signals for a strategy.

        Returns:
            dict with keys: signals_generated, signal_ids
        """
        ...

    async def verify_forward_returns(
        self, db: Any, strategy: dict[str, Any]
    ) -> dict[str, Any]:
        """Verify forward returns for a strategy's historical signals.

        Returns:
            dict with keys: verified_count, hit_rate, avg_return
        """
        ...

    async def record_metrics(
        self,
        db: Any,
        strategy: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        """Record performance metrics for a strategy.

        Returns:
            dict with keys: metrics_recorded, timestamp
        """
        ...

    async def generate_hit_rate_report(
        self, db: Any, strategy: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate hit rate report for a strategy.

        Returns:
            dict with keys: hit_rate, sample_size, confidence
        """
        ...

    async def write_feedback(
        self,
        db: Any,
        strategy: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        """Write feedback for Strategy Factory to consume.

        Returns:
            dict with keys: feedback_written, feedback_id
        """
        ...

    async def settle_strategy_orders(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Settle pending orders for a strategy (paper trading).

        Returns:
            dict with keys: filled_count, rejected_count, settlement_details
        """
        ...

    async def run_recompile_remediation(self, db: Any) -> dict[str, Any]:
        """Run recompile remediation for observe-pool strategies.

        Returns:
            dict with keys: recompiled_count, promoted_count
        """
        ...

    async def verify_trade_predictions(self, db: Any) -> dict[str, Any]:
        """Run daily trade prediction verification.

        Returns:
            dict with keys: verified_count, accuracy
        """
        ...

    async def write_daily_predictions(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist predictions for signals already generated in this cycle."""
        ...

    async def score_horizon_predictions(self, db: Any) -> dict[str, Any]:
        """Score pending predictions using the net-of-cost horizon contract."""
        ...

    async def run_calibration(self, db: Any) -> dict[str, Any]:
        """Evaluate calibration start gates and fit only an eligible model."""
        ...

    async def evaluate_selective_gate(
        self,
        db: Any,
        *,
        signal_date: Any,
    ) -> dict[str, Any]:
        """Persist selective decisions for a target session."""
        ...

    async def run_accelerator(self, db: Any) -> dict[str, Any]:
        """Run incubation accelerator to fast-track high-quality strategies.

        Returns:
            dict with keys: accelerated_count, promoted_count
        """
        ...

    async def monitor_alerts(self, db: Any) -> dict[str, Any]:
        """Monitor and process incubation alerts.

        Returns:
            dict with keys: alerts_processed, critical_count
        """
        ...


__all__ = ["IncubationRuntimeSupport"]
