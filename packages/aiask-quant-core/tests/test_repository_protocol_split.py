"""The repository protocol must keep market data and factory domain separate.

``StrategyFactoryRepository`` mixed both domains in one protocol. Adopting it
wholesale during the factory-table ownership move would have made
``strategy-factory`` the owner of raw market-data reads too — which contradicts
the declared DAG, where market data stays with the data provider.

These tests pin the split and, importantly, pin that the two lists cannot drift
apart or silently reclassify a method.
"""

from __future__ import annotations

import pytest

from aiask_quant_core.storage.contracts import (
    FACTORY_DOMAIN_REPOSITORY_METHODS,
    MARKET_DATA_REPOSITORY_METHODS,
    MarketDataRepository,
    REQUIRED_REPOSITORY_METHODS,
    StrategyFactoryRepository,
)


# --- the partition itself ------------------------------------------------------


def test_the_two_domains_partition_the_union_exactly() -> None:
    """No method may be lost, duplicated, or invented by the split."""
    assert set(MARKET_DATA_REPOSITORY_METHODS) | set(FACTORY_DOMAIN_REPOSITORY_METHODS) == set(
        REQUIRED_REPOSITORY_METHODS
    )
    assert len(MARKET_DATA_REPOSITORY_METHODS) + len(FACTORY_DOMAIN_REPOSITORY_METHODS) == len(
        REQUIRED_REPOSITORY_METHODS
    )


def test_the_domains_do_not_overlap() -> None:
    assert not set(MARKET_DATA_REPOSITORY_METHODS) & set(FACTORY_DOMAIN_REPOSITORY_METHODS)


def test_market_data_domain_is_exactly_the_four_known_reads() -> None:
    """Pinned explicitly: silently widening this would move market tables."""
    assert set(MARKET_DATA_REPOSITORY_METHODS) == {
        "get_klines",
        "get_limit_up_stats",
        "get_factor_ic_history",
        "list_stock_universe",
    }


def test_factory_domain_carries_the_lifecycle_persistence() -> None:
    factory = set(FACTORY_DOMAIN_REPOSITORY_METHODS)
    for expected in (
        "save_strategy",
        "save_strategy_factory_run",
        "update_strategy_status",
        "save_factory_task_evidence",
        "save_scheduler_state",
        "create_strategy_factory_dispatch",
    ):
        assert expected in factory, f"{expected} belongs to the factory domain"


@pytest.mark.parametrize("method", sorted(MARKET_DATA_REPOSITORY_METHODS))
def test_no_market_read_leaks_into_the_factory_domain(method: str) -> None:
    assert method not in FACTORY_DOMAIN_REPOSITORY_METHODS, (
        f"{method} reads raw market data and must stay with the data provider"
    )


def test_factory_domain_is_derived_not_hand_maintained() -> None:
    """Derivation is what prevents the two lists from drifting apart."""
    expected = tuple(
        name
        for name in REQUIRED_REPOSITORY_METHODS
        if name not in MARKET_DATA_REPOSITORY_METHODS
    )
    assert FACTORY_DOMAIN_REPOSITORY_METHODS == expected


# --- protocol shape -----------------------------------------------------------


def test_market_data_protocol_declares_exactly_its_domain() -> None:
    declared = {
        name
        for name in dir(MarketDataRepository)
        if not name.startswith("_")
    }
    assert declared == set(MARKET_DATA_REPOSITORY_METHODS)


def test_union_protocol_still_declares_every_required_method() -> None:
    """Backwards compatibility: existing callers depend on the union."""
    declared = {name for name in dir(StrategyFactoryRepository) if not name.startswith("_")}
    missing = set(REQUIRED_REPOSITORY_METHODS) - declared
    assert missing == set(), f"union protocol lost: {sorted(missing)}"


def test_both_protocols_are_runtime_checkable() -> None:
    class _MarketOnly:
        async def get_klines(self, code, limit=500):
            return []

        async def get_limit_up_stats(self):
            return {}

        async def get_factor_ic_history(self, factor_name, horizon, limit):
            return []

        async def list_stock_universe(self, limit=200, offset=0):
            return []

    instance = _MarketOnly()
    assert isinstance(instance, MarketDataRepository)
    # A market-only implementation must NOT satisfy the full union.
    assert not isinstance(instance, StrategyFactoryRepository)


def test_a_factory_only_implementation_is_not_a_market_repository() -> None:
    class _FactoryOnly:
        async def save_strategy(self, data):
            return None

    assert not isinstance(_FactoryOnly(), MarketDataRepository)
