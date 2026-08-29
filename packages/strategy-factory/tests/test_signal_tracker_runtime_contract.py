from __future__ import annotations

import pytest

from strategy_factory.infrastructure.runtime_services import (
    clear_runtime_services,
    configure_runtime_services,
)
from strategy_factory.runtime.signal_tracker import (
    SignalTrackerRuntime,
    build_signal_tracker_runtime,
    get_signal_tracker_runtime,
)


@pytest.fixture(autouse=True)
def _reset_runtime_services():
    clear_runtime_services()
    yield
    clear_runtime_services()


class _AsyncIncubation:
    async def process_strategies(self, _db, _strategies, signal_date=None):
        del signal_date
        return {
            "orders_created": 0,
            "orders_filled": 0,
            "nav_snapshots": 0,
            "metrics_recorded": 0,
        }


class _AsyncPipeline:
    async def run_batch(self, _db, **_kwargs):
        return {"count": 0, "auto_promoted": 0}

    async def run_strategy(self, _db, _strategy, **_kwargs):
        return {"ok": True}


class _AsyncRisk:
    async def scan(self, _db, strategies, enforce_actions=True):
        del enforce_actions
        return {"event_count": len(strategies), "action_count": 0}


class _AsyncVector:
    async def reconcile_registry(self, _db, **_kwargs):
        return {"registry_updated": 0}


class _AsyncProjection:
    async def rebuild_batch(self, _db, **_kwargs):
        return {"count": 0}


def test_signal_tracker_runtime_support_factory_accepts_legacy_zero_arg_callable() -> None:
    class _Tracker:
        def status(self):
            return {"provider": "legacy-zero-arg"}

    configure_runtime_services(
        signal_tracker_runtime_factory=lambda: _Tracker(),
        signal_tracker_runtime_support_factory=lambda: _Tracker(),
    )

    runtime = build_signal_tracker_runtime()

    assert runtime.preflight()["runtime_type"] == "_Tracker"
    assert runtime.status()["provider"] == "legacy-zero-arg"


@pytest.mark.asyncio
async def test_signal_tracker_runtime_missing_support_has_stable_error_shape() -> None:
    runtime = SignalTrackerRuntime(None)
    result = await runtime.run_once()

    assert result["errors"] == ["signal_tracker_support_missing"]
    assert result["phase_results"] == {}
    assert result["runtime_universe"]["strategies"] == 0


@pytest.mark.asyncio
async def test_signal_tracker_runtime_status_contract_with_stub_support() -> None:
    class _Tracker:
        def status(self):
            return {"running": False, "provider": "stub"}

        def _phase_timeout_seconds(self, _phase_name: str) -> float:
            return 1.0

        def _get_default_universe(self):
            return []

        async def _load_executable_strategies_with_fallback(self, db, *, limit=500, use_contract=True):
            del db, limit, use_contract
            return []

        async def _load_runtime_submitted_strategies(self, db, *, limit=200):
            del db, limit
            return []

        async def _load_runtime_observation_strategies(self, db, *, limit=200):
            del db, limit
            return []

        @staticmethod
        def _merge_unique_strategies(*groups):
            merged = []
            for group in groups:
                merged.extend(list(group or []))
            return merged

        @staticmethod
        def _get_runtime_control_service():
            class _Control:
                @staticmethod
                def is_blocking_mode(_mode):
                    return False

            return _Control()

        @staticmethod
        async def backfill_forward_returns(_db, *, forward_days_list=None, batch_limit=0, max_rounds=0):
            del forward_days_list, batch_limit, max_rounds
            return {"computed": 0, "windows": {}}

        @staticmethod
        async def _run_lifecycle_scan(_db):
            return {"transitions": []}

        @staticmethod
        async def shutdown(grace_sec: float = 5.0):
            return {"grace_sec": grace_sec}

    class _Db:
        async def initialize(self):
            return None

        async def list_strategies(self, _status, limit=200):
            del limit
            return []

        async def save_strategy_task_run(self, payload):
            return {"id": 1, "trace_id": payload.get("trace_id")}

        async def update_strategy_task_run(self, task_run_id, **kwargs):
            return {"task_run_id": task_run_id, **kwargs}

        async def save_strategy_domain_event(self, payload):
            return payload

    configure_runtime_services(
        db_provider=lambda: _Db(),
        signal_tracker_runtime_factory=lambda run_time=None: _Tracker(),
        signal_tracker_runtime_support_factory=lambda run_time=None: _Tracker(),
        strategy_incubation_service_factory=_AsyncIncubation,
        strategy_incubation_pipeline_service_factory=_AsyncPipeline,
        strategy_runtime_risk_service_factory=_AsyncRisk,
        strategy_vector_governance_service_factory=_AsyncVector,
        strategy_domain_projection_service_factory=_AsyncProjection,
        strategy_lifecycle_scan_runner=_Tracker._run_lifecycle_scan,
        strategy_registry=object(),
    )

    runtime = get_signal_tracker_runtime()
    assert runtime.status()["provider"] == "stub"
    result = await runtime.run_once()
    assert result["phase_results"]["A"]["status"] == "completed"
    assert result["phase_results"]["B"]["status"] == "completed"
    assert result["phase_results"]["F"]["status"] == "completed"
    assert result["phase_error_count"] == 0
    assert result["runtime_universe"]["strategies"] == 0


@pytest.mark.asyncio
async def test_signal_tracker_runtime_phase_b_does_not_require_private_forward_attrs(monkeypatch) -> None:
    monkeypatch.delenv("STRATEGY_FACTORY_FORWARD_DAYS", raising=False)
    captured: dict[str, object] = {}

    class _Tracker:
        def status(self):
            return {"running": False, "provider": "no-private-forward-attrs"}

        def _phase_timeout_seconds(self, _phase_name: str) -> float:
            return 1.0

        def _get_default_universe(self):
            return []

        async def _load_executable_strategies_with_fallback(self, db, *, limit=500, use_contract=True):
            del db, limit, use_contract
            return []

        async def _load_runtime_submitted_strategies(self, db, *, limit=200):
            del db, limit
            return []

        async def _load_runtime_observation_strategies(self, db, *, limit=200):
            del db, limit
            return []

        @staticmethod
        def _merge_unique_strategies(*groups):
            return [item for group in groups for item in list(group or [])]

        @staticmethod
        def _get_runtime_control_service():
            class _Control:
                @staticmethod
                def is_blocking_mode(_mode):
                    return False

            return _Control()

        @staticmethod
        async def backfill_forward_returns(_db, *, forward_days_list=None, batch_limit=0, max_rounds=0):
            captured["forward_days_list"] = list(forward_days_list or [])
            captured["batch_limit"] = batch_limit
            captured["max_rounds"] = max_rounds
            return {"computed": 0, "windows": {}}

    class _Db:
        async def initialize(self):
            return None

        async def list_strategies(self, _status, limit=200):
            del limit
            return []

        async def save_strategy_task_run(self, payload):
            return {"id": 1, "trace_id": payload.get("trace_id")}

        async def update_strategy_task_run(self, task_run_id, **kwargs):
            return {"task_run_id": task_run_id, **kwargs}

        async def save_strategy_domain_event(self, payload):
            return payload

    async def _scan(_db):
        return {"transitions": []}

    configure_runtime_services(
        db_provider=lambda: _Db(),
        signal_tracker_runtime_factory=lambda run_time=None: _Tracker(),
        signal_tracker_runtime_support_factory=lambda run_time=None: _Tracker(),
        strategy_incubation_service_factory=_AsyncIncubation,
        strategy_incubation_pipeline_service_factory=_AsyncPipeline,
        strategy_runtime_risk_service_factory=_AsyncRisk,
        strategy_vector_governance_service_factory=_AsyncVector,
        strategy_domain_projection_service_factory=_AsyncProjection,
        strategy_lifecycle_scan_runner=_scan,
        strategy_registry=object(),
    )

    result = await get_signal_tracker_runtime().run_once()

    assert result["phase_results"]["B"]["status"] == "completed"
    assert result["phase_error_count"] == 0
    assert captured == {
        "forward_days_list": [1, 5, 10, 20],
        "batch_limit": 2000,
        "max_rounds": 100,
    }
