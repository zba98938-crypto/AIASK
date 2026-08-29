"""Shared storage contracts for Strategy Factory persistence.

Ownership split (extraction plan Phase 2)
-----------------------------------------
``StrategyFactoryRepository`` historically mixed two domains in one protocol.
That blocked the factory-table ownership move: adopting the protocol wholesale
would have made ``strategy-factory`` the owner of raw market-data reads too.

The two domains are now declared separately:

* :data:`MARKET_DATA_REPOSITORY_METHODS` — raw market and external data reads. These stay
  owned by the data provider. ``strategy-factory`` *consumes* them (see
  ``application/collect.py``, ``application/event_engine.py``) but must never
  own the tables behind them.
* :data:`FACTORY_DOMAIN_REPOSITORY_METHODS` — strategy/factory lifecycle
  persistence. These move with the factory domain.

``REQUIRED_REPOSITORY_METHODS`` remains the union so existing runtime-boundary
checks keep working unchanged.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable


#: Raw market-data reads. Owned by the data provider; never move these.
MARKET_DATA_REPOSITORY_METHODS: tuple[str, ...] = (
    "get_klines",
    "get_limit_up_stats",
    "get_factor_ic_history",
    "list_stock_universe",
)


REQUIRED_REPOSITORY_METHODS: tuple[str, ...] = (
    "get_klines",
    "get_limit_up_stats",
    "get_factor_ic_history",
    "count_strategies_by_type",
    "save_daily_snapshot",
    "list_strategies",
    "get_strategy",
    "get_strategy_metrics",
    "get_signal_stats",
    "save_strategy",
    "save_strategy_quality_report",
    "update_strategy_status",
    "save_strategy_lineage",
    "save_strategy_metrics",
    "save_elimination_log",
    "get_strategy_generation_experiment",
    "save_strategy_generation_experiment",
    "save_factory_task_evidence",
    "save_strategy_candidate_evidence",
    "save_strategy_signal_evidence",
    "save_strategy_task_run",
    "update_strategy_task_run",
    "list_stock_universe",
    "list_factory_event_clusters",
    "save_factory_theme_definition",
    "save_strategy_factory_run",
    "list_strategy_factory_runs",
    "get_strategy_factory_run",
    "get_latest_strategy_factory_run",
    "save_strategy_factory_run_artifact",
    "list_strategy_factory_run_artifacts",
    "save_scheduler_state",
    "load_scheduler_state",
    "create_strategy_factory_dispatch",
    "update_strategy_factory_dispatch",
    "get_strategy_factory_dispatch",
)


#: Strategy/factory lifecycle persistence. Derived so the two lists cannot drift
#: apart: anything not classified as market data belongs to the factory domain.
FACTORY_DOMAIN_REPOSITORY_METHODS: tuple[str, ...] = tuple(
    name for name in REQUIRED_REPOSITORY_METHODS
    if name not in MARKET_DATA_REPOSITORY_METHODS
)


@runtime_checkable
class MarketDataRepository(Protocol):
    """Raw market-data reads. Stays with the data provider.

    ``strategy-factory`` depends on this as a *consumer*. Keeping it separate is
    what lets the factory tables move without dragging market tables along.
    """

    async def get_klines(self, code: str, limit: int = 500) -> list[Mapping[str, Any]]: ...

    async def get_limit_up_stats(self) -> Mapping[str, Any]: ...

    async def get_factor_ic_history(
        self, factor_name: str, horizon: str, limit: int
    ) -> list[Mapping[str, Any]]: ...

    async def list_stock_universe(
        self, limit: int = 200, offset: int = 0
    ) -> list[Mapping[str, Any]]: ...


@runtime_checkable
class StrategyFactoryRepository(Protocol):
    """Repository contract observed across Strategy Factory storage hosts.

    Union of :class:`MarketDataRepository` and the factory-domain methods. Kept
    whole for backwards compatibility; new code that only persists factory state
    should depend on the narrower domain list rather than this union.
    """

    async def get_klines(self, code: str, limit: int = 500) -> list[Mapping[str, Any]]: ...

    async def get_limit_up_stats(self) -> Mapping[str, Any]: ...

    async def get_factor_ic_history(self, factor_name: str, horizon: str, limit: int) -> list[Mapping[str, Any]]: ...

    async def count_strategies_by_type(self, status: str) -> Mapping[str, int]: ...

    async def save_daily_snapshot(self, snapshot_date: Any, snapshot: Mapping[str, Any]) -> Any: ...

    async def list_strategies(self, status: str, limit: int = 500) -> list[Mapping[str, Any]]: ...

    async def get_strategy(self, strategy_id: str) -> Optional[Mapping[str, Any]]: ...

    async def get_strategy_metrics(self, strategy_id: str) -> list[Mapping[str, Any]]: ...

    async def get_signal_stats(
        self,
        strategy_id: str,
        lookback_days: int | None = None,
        eps: float | None = None,
    ) -> Mapping[str, Any]: ...

    async def save_strategy(self, data: Mapping[str, Any]) -> Any: ...

    async def save_strategy_quality_report(self, strategy_id: str, report_type: str, report: Mapping[str, Any]) -> Any: ...

    async def update_strategy_status(self, strategy_id: str, status: str, **kwargs: Any) -> Any: ...

    async def save_strategy_lineage(
        self,
        strategy_id: str,
        parent_strategy_id: Optional[str],
        reason: str,
        snapshot: Mapping[str, Any],
    ) -> Any: ...

    async def save_strategy_metrics(self, strategy_id: str, period: str, payload: Mapping[str, Any]) -> Any: ...

    async def save_elimination_log(self, strategy_id: str, log_date: Any, red_flags: list[str], reason: str) -> Any: ...

    async def get_strategy_generation_experiment(self, experiment_id: str) -> Optional[Mapping[str, Any]]: ...

    async def save_strategy_generation_experiment(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_factory_task_evidence(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_candidate_evidence(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_signal_evidence(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_task_run(self, payload: Mapping[str, Any]) -> Any: ...

    async def update_strategy_task_run(self, task_run_id: Any, **kwargs: Any) -> Any: ...

    async def list_stock_universe(self, limit: int = 200, offset: int = 0) -> list[Mapping[str, Any]]: ...

    async def list_factory_event_clusters(self, status: Optional[str] = None, limit: int = 200) -> list[Mapping[str, Any]]: ...

    async def save_factory_theme_definition(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_factory_run(self, results: Mapping[str, Any]) -> Any: ...

    async def list_strategy_factory_runs(self, limit: int = 20) -> list[Mapping[str, Any]]: ...

    async def get_strategy_factory_run(self, run_id: str) -> Optional[Mapping[str, Any]]: ...

    async def get_latest_strategy_factory_run(self) -> Optional[Mapping[str, Any]]: ...

    async def save_strategy_factory_run_artifact(self, payload: Mapping[str, Any]) -> Any: ...

    async def list_strategy_factory_run_artifacts(self, run_id: str) -> list[Mapping[str, Any]]: ...

    async def save_scheduler_state(self, payload: Mapping[str, Any]) -> Any: ...

    async def load_scheduler_state(self, state_key: str = "strategy_factory_scheduler") -> Mapping[str, Any]: ...

    async def create_strategy_factory_dispatch(self, payload: Mapping[str, Any]) -> Any: ...

    async def update_strategy_factory_dispatch(self, dispatch_id: str, **kwargs: Any) -> Any: ...

    async def get_strategy_factory_dispatch(self, dispatch_id: str) -> Optional[Mapping[str, Any]]: ...

    async def list_strategy_factory_dispatches(
        self,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[Mapping[str, Any]]: ...


__all__ = [
    "FACTORY_DOMAIN_REPOSITORY_METHODS",
    "MARKET_DATA_REPOSITORY_METHODS",
    "MarketDataRepository",
    "REQUIRED_REPOSITORY_METHODS",
    "StrategyFactoryRepository",
]
