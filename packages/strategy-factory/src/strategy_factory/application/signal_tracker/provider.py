"""Signal Tracker provider interface - defines what host must provide."""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol


class SignalTrackerProvider(Protocol):
    """Provider interface for signal tracker runtime capabilities.

    Host process (e.g., akshare-mcp) must implement this interface to provide
    concrete data access, execution, and storage capabilities.
    """

    async def get_db(self) -> Any:
        """Return initialized database connection."""
        ...

    def get_default_universe(self) -> list[dict[str, Any]]:
        """Return default strategy universe for signal tracking."""
        ...

    async def load_executable_strategies(
        self,
        db: Any,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Load executable strategies from the execution universe."""
        ...

    async def load_runtime_submitted_strategies(
        self,
        db: Any,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Load runtime submitted strategies eligible for tracking."""
        ...

    async def load_runtime_observation_strategies(
        self,
        db: Any,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Load runtime observation strategies (paper trading)."""
        ...

    async def get_klines(
        self,
        db: Any,
        code: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Get K-line data for a stock code."""
        ...

    async def generate_signals(
        self,
        db: Any,
        strategy: dict[str, Any],
        *,
        as_of: date,
    ) -> dict[str, Any]:
        """Generate trading signals for a strategy."""
        ...

    async def create_signal_event_snapshot(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
        *,
        as_of: date,
    ) -> dict[str, Any]:
        """Create a signal event snapshot."""
        ...

    async def backfill_forward_returns(
        self,
        db: Any,
        *,
        forward_days_list: list[int],
        batch_limit: int = 2000,
        max_rounds: int = 100,
    ) -> dict[str, Any]:
        """Backfill forward returns for signal events."""
        ...

    async def sync_incubation_orders(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Sync incubation orders and fill status."""
        ...

    async def sync_incubation_nav_snapshots(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Sync incubation NAV snapshots."""
        ...

    async def sync_incubation_metrics(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Sync incubation performance metrics."""
        ...

    async def run_incubation_pipeline(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run incubation pipeline stage transitions."""
        ...

    async def run_submitted_runtime_pipeline(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run pipeline for submitted runtime strategies."""
        ...

    async def run_runtime_risk_scan(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run runtime risk scanning and actions."""
        ...

    async def run_lifecycle_scan(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run lifecycle state transitions."""
        ...

    async def reconcile_vector_registry(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Reconcile vector search registry."""
        ...

    async def snapshot_domain_projections(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Snapshot domain projections for strategies."""
        ...

    def phase_timeout_seconds(self, phase_name: str) -> float:
        """Get timeout in seconds for a phase."""
        ...
