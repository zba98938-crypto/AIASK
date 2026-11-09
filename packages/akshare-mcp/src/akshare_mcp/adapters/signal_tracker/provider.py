"""Signal Tracker provider implementation using AKShare MCP services."""

from __future__ import annotations

from datetime import date
from typing import Any

from ...services.signal_tracker import SignalTracker


class AKShareSignalTrackerProvider:
    """AKShare-based implementation of SignalTrackerProvider.

    This class wraps the existing SignalTracker implementation and exposes
    it through the provider interface defined by strategy-factory.
    """

    def __init__(self, signal_tracker: SignalTracker):
        self._tracker = signal_tracker

    async def get_db(self) -> Any:
        """Return initialized database connection."""
        from ...storage import get_db

        db = get_db()
        await db.initialize()
        return db

    async def resolve_session_date(self, db: Any) -> dict[str, Any]:
        """Newest synced ``kline_1d`` session; used when no ``as_of`` is given."""
        from ...services.incubation_factory.session_date import resolve_session_date

        return await resolve_session_date(db)

    def get_default_universe(self) -> list[dict[str, Any]]:
        """Return default strategy universe.

        Stock-code strings are not strategies; only dict strategy records are kept.
        """
        return [
            item
            for item in list(self._tracker._get_default_universe())
            if isinstance(item, dict)
        ]

    async def load_executable_strategies(
        self,
        db: Any,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Load executable strategies from execution universe."""
        return await self._tracker._load_executable_strategies_with_fallback(
            db,
            limit=limit,
            use_contract=True,
        )

    async def load_runtime_submitted_strategies(
        self,
        db: Any,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Load runtime submitted strategies."""
        return await self._tracker._load_runtime_submitted_strategies(db, limit=limit)

    async def load_runtime_observation_strategies(
        self,
        db: Any,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Load runtime observation strategies."""
        return await self._tracker._load_runtime_observation_strategies(db, limit=limit)

    async def get_klines(
        self,
        db: Any,
        code: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Get K-line data."""
        return await self._tracker._get_klines_with_fallback(db, code, limit=limit)

    async def generate_signals(
        self,
        db: Any,
        strategy: dict[str, Any],
        *,
        as_of: date,
    ) -> dict[str, Any]:
        """Generate trading signals for a strategy."""
        # Delegate to existing signal generation logic
        generate = getattr(self._tracker, "_generate_signals", None)
        if callable(generate):
            return await generate(db, strategy, as_of=as_of)
        return {"signals_generated": 0}

    async def create_signal_event_snapshot(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
        *,
        as_of: date,
    ) -> dict[str, Any]:
        """Create signal event snapshot."""
        create_snapshot = getattr(self._tracker, "_create_signal_event_snapshot", None)
        if callable(create_snapshot):
            return await create_snapshot(db, strategy, signal_result, as_of=as_of)
        return {"snapshot_created": False}

    async def backfill_forward_returns(
        self,
        db: Any,
        *,
        forward_days_list: list[int] | None = None,
        batch_limit: int = 2000,
        max_rounds: int = 100,
    ) -> dict[str, Any]:
        """Backfill forward returns."""
        backfill = getattr(self._tracker, "_backfill_forward_returns", None)
        if callable(backfill):
            result = await backfill(
                db,
                forward_days_list=forward_days_list,
                batch_limit=batch_limit,
                max_rounds=max_rounds,
            )
            payload = dict(result or {})
            payload.setdefault("computed_count", int(payload.get("computed") or 0))
            return payload
        return {"computed_count": 0}

    async def sync_incubation_orders(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        *,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Sync incubation orders.

        ``as_of`` aligns the date with the orchestrator request; the legacy
        SignalTracker service does not take one, so it is accepted for
        interface parity and intentionally not forwarded.
        """
        del as_of
        sync = getattr(self._tracker, "_sync_incubation_orders", None)
        if callable(sync):
            return await sync(db, strategies)
        return {"orders_synced": 0, "filled_count": 0}

    async def sync_incubation_nav_snapshots(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        *,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Sync incubation NAV snapshots (``as_of`` accepted for interface parity)."""
        del as_of
        sync = getattr(self._tracker, "_sync_incubation_nav_snapshots", None)
        if callable(sync):
            return await sync(db, strategies)
        return {"snapshots_created": 0}

    async def sync_incubation_metrics(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        *,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Sync incubation metrics (``as_of`` accepted for interface parity)."""
        del as_of
        sync = getattr(self._tracker, "_sync_incubation_metrics", None)
        if callable(sync):
            return await sync(db, strategies)
        return {"metrics_recorded": 0}

    async def run_incubation_pipeline(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run incubation pipeline."""
        run_pipeline = getattr(self._tracker, "_run_incubation_pipeline", None)
        if callable(run_pipeline):
            return await run_pipeline(db, strategies)
        return {"snapshots_created": 0, "auto_promoted": 0}

    async def run_submitted_runtime_pipeline(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run submitted runtime pipeline."""
        run_pipeline = getattr(self._tracker, "_run_submitted_runtime_pipeline", None)
        if callable(run_pipeline):
            return await run_pipeline(db, strategies)
        return {"snapshots_created": 0}

    async def run_runtime_risk_scan(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run runtime risk scan."""
        risk_scan = getattr(self._tracker, "_run_runtime_risk_scan", None)
        if callable(risk_scan):
            return await risk_scan(db, strategies)
        return {"events_detected": 0, "actions_taken": 0}

    async def run_lifecycle_scan(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run lifecycle scan."""
        lifecycle = getattr(self._tracker, "_run_lifecycle_scan", None)
        if callable(lifecycle):
            return await lifecycle(db, strategies)
        return {"transitions": 0}

    async def reconcile_vector_registry(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Reconcile vector registry."""
        reconcile = getattr(self._tracker, "_reconcile_vector_registry", None)
        if callable(reconcile):
            return await reconcile(db, strategies)
        return {"updates": 0}

    async def snapshot_domain_projections(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Snapshot domain projections."""
        snapshot = getattr(self._tracker, "_snapshot_domain_projections", None)
        if callable(snapshot):
            return await snapshot(db, strategies)
        return {"snapshots_created": 0}

    def phase_timeout_seconds(self, phase_name: str) -> float:
        """Get phase timeout."""
        return self._tracker._phase_timeout_seconds(phase_name)
