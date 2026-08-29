"""Strategy Factory owns the factory-domain persistence contract.

Phase 2: rather than physically relocating the repository mixins (which are built
on host connection-pool internals and reached by duck typing from ~121 call
sites), Strategy Factory owns a facade that declares the domain surface and
forwards to an injected connection.

These tests pin the two properties that make the facade worth having:

* it refuses market-data reads, so it cannot become a backdoor to tables the
  data provider owns;
* a missing host capability is reported structurally, not as an AttributeError
  from inside an orchestration path.
"""

from __future__ import annotations

import asyncio

import pytest

from strategy_factory.infrastructure.factory_repository import (
    FactoryRepository,
    FactoryRepositoryError,
    MissingCapability,
    assert_factory_domain_ready,
    build_factory_repository,
    factory_domain_methods,
)


MARKET_READS = ("get_klines", "get_limit_up_stats", "get_factor_ic_history", "list_stock_universe")


class _FullConnection:
    """Implements every declared factory-domain method."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        for name in factory_domain_methods():
            setattr(self, name, self._make(name))

    def _make(self, name: str):
        async def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"method": name}

        return _call

    # Market reads exist on the real adapter too; the facade must still refuse them.
    async def get_klines(self, code, limit=500):
        return []

    async def list_stock_universe(self, limit=200, offset=0):
        return []


class _BareConnection:
    """Implements nothing."""


# --- construction --------------------------------------------------------------


def test_declared_surface_is_non_trivial() -> None:
    methods = factory_domain_methods()
    assert len(methods) >= 30, f"expected the factory-domain surface, got {len(methods)}"


def test_repository_requires_a_connection() -> None:
    with pytest.raises(FactoryRepositoryError, match="requires a connection"):
        FactoryRepository(None)


def test_builder_returns_a_repository() -> None:
    assert isinstance(build_factory_repository(_FullConnection()), FactoryRepository)


# --- capability reporting ------------------------------------------------------


def test_full_connection_reports_ready() -> None:
    report = FactoryRepository(_FullConnection()).capability_report()

    assert report["ready"] is True
    assert report["missing"] == []
    assert report["available"] == report["declared"]


def test_bare_connection_reports_every_gap() -> None:
    report = FactoryRepository(_BareConnection()).capability_report()

    assert report["ready"] is False
    assert report["available"] == 0
    assert set(report["missing"]) == set(factory_domain_methods())


def test_partial_connection_names_only_what_is_missing() -> None:
    class _Partial:
        async def save_strategy_factory_run(self, run):
            return None

    missing = FactoryRepository(_Partial()).missing_capabilities()

    assert "save_strategy_factory_run" not in missing
    assert "save_scheduler_state" in missing


# --- market-data boundary ------------------------------------------------------


@pytest.mark.parametrize("method", MARKET_READS)
def test_market_reads_are_refused_even_when_the_connection_offers_them(method: str) -> None:
    """The facade must not become a backdoor to provider-owned tables."""
    repository = FactoryRepository(_FullConnection())

    with pytest.raises(FactoryRepositoryError, match="market data"):
        repository._resolve(method)


@pytest.mark.parametrize("method", MARKET_READS)
def test_market_reads_are_not_declared_domain_methods(method: str) -> None:
    assert method not in factory_domain_methods()


def test_undeclared_method_is_refused() -> None:
    repository = FactoryRepository(_FullConnection())

    with pytest.raises(FactoryRepositoryError, match="not a declared"):
        repository._resolve("drop_everything")


# --- forwarding ----------------------------------------------------------------


def test_call_forwards_to_the_connection() -> None:
    connection = _FullConnection()
    repository = FactoryRepository(connection)

    result = asyncio.run(repository.call("save_strategy_factory_run", {"run_id": "r1"}))

    assert result == {"method": "save_strategy_factory_run"}
    assert connection.calls[0][0] == "save_strategy_factory_run"
    assert connection.calls[0][1] == ({"run_id": "r1"},)


def test_attribute_access_forwards_too() -> None:
    """Keeps duck-typed call sites working when a repository replaces a connection."""
    connection = _FullConnection()
    repository = FactoryRepository(connection)

    result = asyncio.run(repository.save_scheduler_state({"state_key": "k"}))

    assert result == {"method": "save_scheduler_state"}


def test_private_attribute_access_still_raises_attribute_error() -> None:
    repository = FactoryRepository(_FullConnection())
    with pytest.raises(AttributeError):
        repository._not_a_real_thing


def test_missing_capability_is_reported_with_its_name() -> None:
    repository = FactoryRepository(_BareConnection())

    with pytest.raises(MissingCapability) as excinfo:
        asyncio.run(repository.call("save_strategy_factory_run"))

    assert excinfo.value.method == "save_strategy_factory_run"
    assert "save_strategy_factory_run" in str(excinfo.value)


def test_try_call_degrades_for_optional_capabilities() -> None:
    repository = FactoryRepository(_BareConnection())

    result = asyncio.run(
        repository.try_call("list_strategy_factory_runs", default=[])
    )

    assert result == []


def test_try_call_still_forwards_when_available() -> None:
    repository = FactoryRepository(_FullConnection())

    result = asyncio.run(repository.try_call("list_strategy_factory_runs", default=[]))

    assert result == {"method": "list_strategy_factory_runs"}


def test_try_call_does_not_mask_a_market_read_refusal() -> None:
    """Degrading is for absent capabilities, not for boundary violations."""
    repository = FactoryRepository(_FullConnection())

    with pytest.raises(FactoryRepositoryError, match="market data"):
        asyncio.run(repository.try_call("get_klines", default=[]))


# --- preflight assertion -------------------------------------------------------


def test_assert_ready_passes_for_a_full_connection() -> None:
    assert_factory_domain_ready(_FullConnection())


def test_assert_ready_names_the_gaps() -> None:
    with pytest.raises(FactoryRepositoryError) as excinfo:
        assert_factory_domain_ready(_BareConnection())

    message = str(excinfo.value)
    assert "save_strategy_factory_run" in message


def test_assert_ready_accepts_a_narrowed_requirement() -> None:
    class _Partial:
        async def save_strategy_factory_run(self, run):
            return None

    # Only the named subset is required.
    assert_factory_domain_ready(_Partial(), required=["save_strategy_factory_run"])

    with pytest.raises(FactoryRepositoryError):
        assert_factory_domain_ready(_Partial(), required=["save_scheduler_state"])


# --- against the real adapter --------------------------------------------------


def test_the_real_sqlite_adapter_satisfies_the_whole_domain() -> None:
    """The facade must be adoptable without changing the host adapter."""
    sqlite_module = pytest.importorskip(
        "aiask_quant_core.storage.sqlite", reason="quant-core not installed"
    )
    adapter = sqlite_module.SQLiteAdapter(":memory:")

    report = FactoryRepository(adapter).capability_report()

    assert report["ready"] is True, (
        f"the host adapter is missing factory-domain methods: {report['missing']}"
    )
