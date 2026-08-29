from __future__ import annotations

from strategy_factory.application.factory_execution import (
    FactoryExecutionMode,
    is_shadow_readonly,
    normalize_factory_execution_mode,
    resolve_factory_engine_version,
    resolve_runtime_mode_flags,
)


def test_stock_first_observe_primary_mode_is_recognized():
    mode = normalize_factory_execution_mode("stock_first_observe_primary")
    assert mode == FactoryExecutionMode.STOCK_FIRST_OBSERVE_PRIMARY
    assert resolve_factory_engine_version(mode) == "strategy_factory.stock_first_observe.primary"


def test_stock_first_observe_shadow_is_readonly_shadow():
    mode = normalize_factory_execution_mode("stock_first_observe_shadow")
    assert mode == FactoryExecutionMode.STOCK_FIRST_OBSERVE_SHADOW
    assert is_shadow_readonly(mode) is True
    assert resolve_factory_engine_version(mode) == "strategy_factory.stock_first_observe.shadow"


def test_stock_first_observe_mode_enables_runtime_flags():
    flags = resolve_runtime_mode_flags("stock_first_observe_primary")
    assert flags["stock_first_observe_mode"] is True
    assert flags["router_enabled"] is True
    assert flags["router_strict"] is True
    assert flags["observe_first_enabled"] is True
