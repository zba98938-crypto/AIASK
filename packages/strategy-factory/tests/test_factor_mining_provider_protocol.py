"""The factor-mining orchestration paths must use the public provider protocol.

``run_maintenance`` used to reach through seven private attributes on the host
support object (``_decay_monitor``, ``_persist_decay_report``,
``_persist_decay_updates``, ``_promote_quarantine_factors``,
``_run_qc_pipeline``, ``_active_pool``, ``_get_db``). That was the only
orchestration path with zero protocol coverage, which is exactly what blocked it
from moving out of the host. These tests pin that it now goes through the
protocol, and that the legacy adapter still bridges older hosts.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from strategy_factory.application.factor_mining.provider import FactorMiningProvider
from strategy_factory.runtime.factor_mining import FactorMiningRuntime
from strategy_factory.runtime.provider_adapters import LegacyFactorMiningProvider


#: Methods added to close the maintenance and QC seams.
MAINTENANCE_METHODS = (
    "run_decay_check",
    "persist_decay_report",
    "persist_decay_updates",
    "promote_quarantine_factors",
    "run_qc_pipeline",
)

QC_METHODS = (
    "run_factor_oos_validation",
    "run_factor_group_backtest",
    "run_factor_robustness_check",
    "validate_factor_candidate",
    "compile_factor_candidate",
)

GATEWAY_METHODS = (
    "load_factor_pool",
    "optimize_factor_weights",
    "write_factor_performance",
    "get_factor_metrics",
    "get_factor_llm_provider",
)


class _ModernProvider:
    """Implements the public protocol only — no private attributes at all."""

    def __init__(self):
        self.calls: list[str] = []

    def _record(self, name: str):
        self.calls.append(name)

    async def get_db(self):
        self._record("get_db")
        return object()

    async def ensure_persistent_pool(self, db):
        self._record("ensure_persistent_pool")

    async def run_decay_check(self, db):
        self._record("run_decay_check")
        return {"decayed": 2}

    async def persist_decay_report(self, db, report):
        self._record("persist_decay_report")

    async def persist_decay_updates(self, db, report):
        self._record("persist_decay_updates")

    async def promote_quarantine_factors(self, db):
        self._record("promote_quarantine_factors")
        return {"promoted": 1}

    async def run_qc_pipeline(self, db):
        self._record("run_qc_pipeline")
        return {"retired": 3}

    def get_active_pool_size(self) -> int:
        self._record("get_active_pool_size")
        return 42


# --- protocol completeness -----------------------------------------------------


@pytest.mark.parametrize(
    "method", MAINTENANCE_METHODS + QC_METHODS + GATEWAY_METHODS
)
def test_protocol_declares_the_new_seam(method: str) -> None:
    assert hasattr(FactorMiningProvider, method), (
        f"FactorMiningProvider must declare {method}; without it the "
        "orchestration path cannot move out of the host"
    )


@pytest.mark.parametrize(
    "method", MAINTENANCE_METHODS + QC_METHODS + GATEWAY_METHODS
)
def test_legacy_adapter_implements_the_new_seam(method: str) -> None:
    """Older hosts keep working: the adapter bridges private to public."""
    assert callable(getattr(LegacyFactorMiningProvider, method, None)), (
        f"LegacyFactorMiningProvider must bridge {method}"
    )


# --- run_maintenance uses the protocol ----------------------------------------


def test_run_maintenance_uses_only_public_protocol_methods() -> None:
    provider = _ModernProvider()
    runtime = FactorMiningRuntime(provider)

    result = asyncio.run(runtime.run_maintenance())

    assert result["decay_report"] == {"decayed": 2}
    assert result["promotion_report"] == {"promoted": 1}
    assert result["qc_pipeline_report"] == {"retired": 3}
    assert result["pool_size"] == 42

    # The full maintenance sequence ran through public methods.
    for name in ("get_db", "ensure_persistent_pool", *MAINTENANCE_METHODS):
        assert name in provider.calls, f"{name} was not called"
    assert "get_active_pool_size" in provider.calls


def test_run_maintenance_source_has_no_private_reach_through() -> None:
    """Guard the property, not just the behaviour.

    A future edit could re-introduce ``support._decay_monitor``; this catches it
    without needing a host to be present.
    """
    source = inspect.getsource(FactorMiningRuntime.run_maintenance)

    # Match the reach-through pattern (``support._x`` / ``provider._x``), not the
    # bare name — ``get_active_pool_size`` legitimately contains "_active_pool".
    for private in (
        "_decay_monitor",
        "_persist_decay_report",
        "_persist_decay_updates",
        "_promote_quarantine_factors",
        "_run_qc_pipeline",
        "_active_pool",
        "_get_db",
        "_ensure_persistent_pool",
    ):
        for holder in ("support.", "provider.", "self._support."):
            assert f"{holder}{private}" not in source, (
                f"run_maintenance must not reach through {holder}{private}; "
                "add a provider protocol method instead"
            )


def test_run_maintenance_reports_missing_support_structurally() -> None:
    runtime = FactorMiningRuntime(None)
    result = asyncio.run(runtime.run_maintenance())
    assert result == {"error": "factor_mining_support_missing"}


# --- legacy bridging behaviour -------------------------------------------------


def test_legacy_adapter_degrades_when_a_host_capability_is_absent() -> None:
    """A host predating a capability must degrade, not crash."""

    class _BareSupport:
        async def _get_db(self):
            return object()

        async def _ensure_persistent_pool(self, db):
            return None

    provider = LegacyFactorMiningProvider(_BareSupport())

    decay = asyncio.run(provider.run_decay_check(object()))
    promotion = asyncio.run(provider.promote_quarantine_factors(object()))
    qc = asyncio.run(provider.run_qc_pipeline(object()))

    assert decay["status"] == "skipped"
    assert "decay_monitor_unavailable" in decay["reason"]
    assert promotion["status"] == "skipped"
    assert qc["status"] == "skipped"


def test_legacy_adapter_bridges_a_real_decay_monitor() -> None:
    class _Monitor:
        async def daily_check(self, pool, *, db=None):
            return {"checked": True, "pool": pool}

    class _Support:
        def __init__(self):
            self._decay_monitor = _Monitor()
            self._active_pool = "POOL"

    provider = LegacyFactorMiningProvider(_Support())
    report = asyncio.run(provider.run_decay_check(object()))

    assert report["checked"] is True
    assert report["pool"] == "POOL"


def test_get_factor_metrics_prefers_the_public_host_method() -> None:
    class _Support:
        def _get_factor_metrics(self, factor_ids):
            return {"public": list(factor_ids)}

        _active_pool = None

    provider = LegacyFactorMiningProvider(_Support())
    assert provider.get_factor_metrics(["a"]) == {"public": ["a"]}


def test_get_factor_metrics_falls_back_to_the_legacy_private_pool() -> None:
    """The two-level private access is contained in the adapter, not the caller."""

    class _Pool:
        _factors = {"f1": {"ic": 0.1}, "f2": {"ic": 0.2}}

    class _Support:
        _active_pool = _Pool()

    provider = LegacyFactorMiningProvider(_Support())

    assert provider.get_factor_metrics(["f1"]) == {"f1": {"ic": 0.1}}
    # Unknown ids are omitted rather than raising.
    assert provider.get_factor_metrics(["nope"]) == {}


def test_get_factor_metrics_tolerates_a_missing_pool() -> None:
    class _Support:
        pass

    assert LegacyFactorMiningProvider(_Support()).get_factor_metrics(["x"]) == {}


def test_compile_factor_candidate_reports_absence_rather_than_raising() -> None:
    class _Support:
        pass

    outcome = LegacyFactorMiningProvider(_Support()).compile_factor_candidate({"expr": "x"})
    assert outcome["ok"] is False
    assert outcome["reason"] == "compiler_unavailable"
