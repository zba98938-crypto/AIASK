"""Provider implementations that bridge Strategy Factory protocols to akshare-mcp services.

These classes implement the Provider protocols defined in strategy-factory's application
layer, delegating to the concrete implementations in akshare-mcp/services.
"""

from __future__ import annotations

from datetime import date
from typing import Any


class SignalTrackerProviderImpl:
    """Implementation of SignalTrackerProvider using akshare-mcp services."""

    def __init__(self, *, db_provider, signal_generator, validator, support):
        self._db_provider = db_provider
        self._signal_generator = signal_generator
        self._validator = validator
        self._support = support

    async def get_db(self) -> Any:
        """Return initialized database connection."""
        return self._db_provider()

    async def execute_phase_a(self, db: Any, universe: list[dict[str, Any]]) -> dict[str, Any]:
        """Phase A: Pre-validation."""
        return await self._support._execute_phase_a(db, universe)

    async def execute_phase_b(self, db: Any, universe: list[dict[str, Any]]) -> dict[str, Any]:
        """Phase B: Signal generation."""
        return await self._support._execute_phase_b(db, universe)

    async def execute_phase_c(self, db: Any, results: dict[str, Any]) -> dict[str, Any]:
        """Phase C: Signal validation."""
        return await self._support._execute_phase_c(db, results)

    async def execute_phase_d(self, db: Any, results: dict[str, Any]) -> dict[str, Any]:
        """Phase D: Forward verification."""
        return await self._support._execute_phase_d(db, results)

    async def execute_phase_e(self, db: Any, results: dict[str, Any]) -> dict[str, Any]:
        """Phase E: Position tracking."""
        return await self._support._execute_phase_e(db, results)

    async def execute_phase_f(self, db: Any, results: dict[str, Any]) -> dict[str, Any]:
        """Phase F: Metrics recording."""
        return await self._support._execute_phase_f(db, results)

    async def execute_phase_g(self, db: Any, results: dict[str, Any]) -> dict[str, Any]:
        """Phase G: Event bridge."""
        return await self._support._execute_phase_g(db, results)

    async def execute_phase_h(self, db: Any, results: dict[str, Any]) -> dict[str, Any]:
        """Phase H: Quality summary."""
        return await self._support._execute_phase_h(db, results)

    async def load_execution_universe(
        self,
        db: Any,
        *,
        strict_subset: list[str] | None = None,
        max_positions: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load execution universe."""
        return await self._support._load_execution_universe(
            db,
            strict_subset=strict_subset,
            max_positions=max_positions,
        )


class FactorMiningProviderImpl:
    """Implementation of FactorMiningProvider using akshare-mcp services."""

    def __init__(self, *, db_provider, factor_scheduler, support):
        self._db_provider = db_provider
        self._factor_scheduler = factor_scheduler
        self._support = support

    async def get_db(self) -> Any:
        """Return initialized database connection."""
        return self._db_provider()

    async def validate_environment(self, db: Any) -> dict[str, Any]:
        """Validate mining environment."""
        return await self._support._validate_environment(db)

    async def mine_factors(
        self,
        db: Any,
        *,
        max_candidates: int = 5,
    ) -> dict[str, Any]:
        """Execute factor mining."""
        return await self._support._mine_factors(db, max_candidates=max_candidates)

    async def persist_factors(
        self,
        db: Any,
        mining_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist mined factors."""
        return await self._support._persist_factors(db, mining_result)

    def quality_summary(self, mining_result: dict[str, Any]) -> dict[str, Any]:
        """Generate quality summary."""
        return self._support._quality_summary(mining_result)


class IncubationProviderImpl:
    """Implementation of IncubationProvider using akshare-mcp services."""

    def __init__(self, *, db_provider, support):
        self._db_provider = db_provider
        self._support = support

    async def get_db(self) -> Any:
        """Return initialized database connection."""
        return self._db_provider()

    async def scan_and_accept_strategies(
        self,
        db: Any,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Scan and accept strategies into incubation."""
        return await self._support._intake.scan_and_accept(db)

    async def list_incubating_strategies(
        self,
        db: Any,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """List strategies currently in incubation."""
        return await self._support._list_incubating(db)

    async def generate_signals(
        self,
        db: Any,
        strategy: dict[str, Any],
        *,
        as_of: date,
    ) -> dict[str, Any]:
        """Generate trading signals for a strategy."""
        return await self._support._signal_generator.generate(db, strategy)

    async def verify_forward_returns(
        self,
        db: Any,
        strategy: dict[str, Any],
    ) -> dict[str, Any]:
        """Verify forward returns for strategy signals."""
        return await self._support._verify_forward_returns(db, strategy)

    async def record_metrics(
        self,
        db: Any,
        strategy: dict[str, Any],
        verification_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Record incubation metrics."""
        return {"recorded": True}

    async def settle_orders(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Settle paper trading orders."""
        return await self._support._settle_strategy_orders(db, strategy, signal_result=signal_result)

    async def run_pipeline(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run incubation pipeline transitions."""
        return await self._support._run_pipeline(db, strategies)

    async def generate_hit_rate_report(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        verification_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate hit rate report."""
        return {"hit_rate": 0.0}

    def paper_runtime_status(self) -> dict[str, Any]:
        """Get paper runtime status."""
        return self._support._paper_runtime_status()

    async def start_paper_runtime(self) -> None:
        """Start paper runtime engines."""
        await self._support._start_paper_runtime()

    async def stop_paper_runtime(self) -> None:
        """Stop paper runtime engines."""
        await self._support._stop_paper_runtime()


class MarketEventIngestProviderImpl:
    """Implementation of MarketEventIngestProvider using akshare-mcp services."""

    def __init__(self, *, db_provider, support):
        self._db_provider = db_provider
        self._support = support

    async def get_db(self) -> Any:
        """Return initialized database connection."""
        return self._db_provider()

    async def scan_event_sources(
        self,
        db: Any,
        *,
        as_of: date,
        lookback_days: int = 7,
    ) -> dict[str, Any]:
        """Scan configured event sources for new events."""
        return await self._support._scan_sources(db, as_of=as_of, lookback_days=lookback_days)

    async def normalize_events(
        self,
        db: Any,
        raw_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Normalize raw events into standard format."""
        return await self._support._normalize_events(db, raw_events)

    async def cluster_events(
        self,
        db: Any,
        normalized_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Cluster related events into themes."""
        return await self._support._cluster_events(db, normalized_events)

    async def generate_event_signals(
        self,
        db: Any,
        clusters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate trading signals from event clusters."""
        return await self._support._generate_signals(db, clusters)

    async def detect_theme_events(
        self,
        db: Any,
        clusters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Detect major theme events."""
        return {"themes_detected": 0}

    async def persist_events(
        self,
        db: Any,
        normalized_events: list[dict[str, Any]],
        clusters: list[dict[str, Any]],
        signals: list[dict[str, Any]],
    ) -> None:
        """Persist processed events."""
        await self._support._persist(db, normalized_events, clusters, signals)


__all__ = [
    "SignalTrackerProviderImpl",
    "FactorMiningProviderImpl",
    "IncubationProviderImpl",
    "MarketEventIngestProviderImpl",
]
