from __future__ import annotations

from strategy_factory.runtime.factor_mining import FactorMiningRuntime
from strategy_factory.runtime.incubation import IncubationRuntime
from strategy_factory.runtime.market_event_ingest import MarketEventIngestRuntime
from strategy_factory.runtime.signal_tracker import SignalTrackerRuntime


def test_runtime_classes_expose_canonical_methods() -> None:
    for runtime_cls in (
        FactorMiningRuntime,
        IncubationRuntime,
        SignalTrackerRuntime,
        MarketEventIngestRuntime,
    ):
        assert hasattr(runtime_cls, "preflight")
        assert hasattr(runtime_cls, "status")
        assert hasattr(runtime_cls, "run_once")


def test_factor_mining_runtime_missing_support_shape() -> None:
    runtime = FactorMiningRuntime(None)
    result = runtime.preflight()
    assert result["available"] is False
    assert result["runtime_type"] is None


def test_market_event_ingest_runtime_missing_support_shape() -> None:
    runtime = MarketEventIngestRuntime(db_provider=lambda: object(), support=None)
    result = runtime.preflight()
    assert result["available"] is False
    assert result["db_provider_available"] is True
