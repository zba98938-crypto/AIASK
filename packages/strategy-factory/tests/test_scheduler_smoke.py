"""Smoke tests for StrategyFactoryScheduler instantiation and basic state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from types import MethodType


def test_scheduler_instantiation():
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    sched = StrategyFactoryScheduler()
    assert sched.schedule_mode in ("continuous", "daily")
    assert sched._running is False
    assert sched._consecutive_failures == 0
    assert sched._circuit_open_until is None


def test_scheduler_start_stop():
    """Verify start/stop lifecycle without an event loop (no actual scheduling)."""
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    sched = StrategyFactoryScheduler()
    # stop without start should be safe
    sched.stop()
    assert sched._running is False


def test_event_theme_maintenance_runs_default_steps_off_hours(monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.research import theme_exposure_builder
    from strategy_factory.application.research import theme_response_regression
    from strategy_factory.application.research import theme_seed

    scheduler = StrategyFactoryScheduler()
    fixed_now = datetime(2026, 5, 24, 20, 0, tzinfo=timezone.utc)
    db = object()
    calls: list[str] = []

    monkeypatch.setattr(scheduler, "_now", lambda: fixed_now)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda now: False)
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_SEED_AUTO_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_EXPOSURE_AUTO_REFRESH_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_EVENT_OUTBOX_AUTO_DRAIN_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_REGRESSION_AUTO_RUN_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_EXPOSURE_BATCH_SIZE", "123")
    monkeypatch.setenv("STRATEGY_FACTORY_EVENT_OUTBOX_DRAIN_LIMIT", "7")

    async def fake_seed(resolved_db, *, overwrite=False, updated_by="bootstrap"):
        calls.append("theme_seed")
        assert resolved_db is db
        assert overwrite is False
        assert updated_by == "scheduler"
        return {"status": "seeded"}

    class FakeExposureBuilder:
        def __init__(self, *, batch_size):
            calls.append(f"exposure_init:{batch_size}")
            self.batch_size = batch_size

        async def build(self, resolved_db, *, batch_size=None):
            calls.append("theme_exposure_refresh")
            assert resolved_db is db
            assert self.batch_size == 123
            assert batch_size == 123
            return {"rows_written": 3}

    class FakeRegression:
        async def run_full_update(self, resolved_db, *, apply_updates=False):
            calls.append("theme_regression_run")
            assert resolved_db is db
            assert apply_updates is False
            return {"mode": "report_only", "updated_count": 0}

    async def fake_outbox(self, resolved_db, *, limit):
        calls.append("outbox_drain")
        assert resolved_db is db
        assert limit == 7
        return {"processed": 1}

    monkeypatch.setattr(theme_seed, "seed_default_theme_graph", fake_seed)
    monkeypatch.setattr(theme_exposure_builder, "ThemeExposureBuilder", FakeExposureBuilder)
    monkeypatch.setattr(theme_response_regression, "ThemeResponseRegression", FakeRegression)
    monkeypatch.setattr(
        scheduler,
        "_drain_event_outbox_maintenance",
        MethodType(fake_outbox, scheduler),
    )

    result = asyncio.run(scheduler._run_event_theme_maintenance_if_due(db))

    assert result["status"] == "completed"
    assert result["ran"] == [
        "theme_seed",
        "theme_exposure_refresh",
        "outbox_drain",
        "theme_regression_run",
    ]
    assert calls == [
        "theme_seed",
        "exposure_init:123",
        "theme_exposure_refresh",
        "outbox_drain",
        "theme_regression_run",
    ]
    assert scheduler._last_event_theme_maintenance_result is result


def test_event_theme_maintenance_skips_market_hours(monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    scheduler = StrategyFactoryScheduler()
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda now: True)

    result = asyncio.run(scheduler._run_event_theme_maintenance_if_due(object()))

    assert result == {"status": "skipped", "reason": "market_hours"}


def test_event_theme_maintenance_respects_disable_switches(monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    scheduler = StrategyFactoryScheduler()
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 5, 24, 20, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda now: False)
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_SEED_AUTO_ENABLED", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_EXPOSURE_AUTO_REFRESH_ENABLED", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_EVENT_OUTBOX_AUTO_DRAIN_ENABLED", "0")
    monkeypatch.setenv("STRATEGY_FACTORY_THEME_REGRESSION_AUTO_RUN_ENABLED", "0")

    result = asyncio.run(scheduler._run_event_theme_maintenance_if_due(object()))

    assert result == {"status": "disabled"}


def test_scheduler_execution_mode_default():
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.factory_execution import FactoryExecutionMode

    sched = StrategyFactoryScheduler()
    assert sched.execution_mode == FactoryExecutionMode.STOCK_FIRST_OBSERVE_PRIMARY


def test_scheduler_engine_version():
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.factory_execution import resolve_factory_engine_version

    sched = StrategyFactoryScheduler()
    assert sched.engine_version == resolve_factory_engine_version(sched.execution_mode)


def test_facade_scheduler_applies_explicit_runtime_providers(monkeypatch):
    import strategy_factory.application.runtime as runtime_module
    from strategy_factory import get_strategy_factory_scheduler

    monkeypatch.setattr(runtime_module, "_local_factory_scheduler", None)
    db = object()
    db_2 = object()

    def db_provider():
        return db

    def db_provider_2():
        return db_2

    adapters = SimpleNamespace(
        repository=SimpleNamespace(raw=db),
        vector_search=object(),
        validation=object(),
        risk=object(),
        incubation=object(),
        autonomy=object(),
        factor_research=object(),
    )
    adapters_2 = SimpleNamespace(
        repository=SimpleNamespace(raw=db_2),
        vector_search=object(),
        validation=object(),
        risk=object(),
        incubation=object(),
        autonomy=object(),
        factor_research=object(),
    )

    sched = get_strategy_factory_scheduler(db_provider=db_provider, runtime_adapters=adapters)

    assert sched._db_provider is db_provider
    assert sched._runtime_adapters is adapters
    assert sched._vector_gateway is adapters.vector_search
    assert sched._autonomy_gateway is adapters.autonomy

    cached = get_strategy_factory_scheduler(db_provider=db_provider_2, runtime_adapters=adapters_2)

    assert cached is sched
    assert cached._db_provider is db_provider_2
    assert cached._runtime_adapters is adapters_2
    assert cached._vector_gateway is adapters_2.vector_search
    assert cached._autonomy_gateway is adapters_2.autonomy


def test_factor_research_refresh_remains_explicit_when_auto_refresh_disabled(monkeypatch):
    from strategy_factory.application.research.runner import ResearchPlaneRunner

    monkeypatch.setenv("STRATEGY_FACTORY_FACTOR_AUTO_REFRESH", "0")

    class FakeScheduler:
        def _adapt_gateway_repository(self, db):
            return db

        def _summarize_refresh_result(self, result):
            return dict(result or {})

        def _inject_factor_refresh_meta(self, artifact, refresh_meta):
            return {**dict(artifact or {}), "freshness_repair": dict(refresh_meta)}

    class FakeFactorGateway:
        def __init__(self):
            self.refresh_count = 0

        async def build_artifact(self, db, snapshot):
            return {
                "summary": {
                    "factor_source_mode": "seed_fallback",
                    "scheduler_last_run": "2026-05-19T09:30:00+08:00",
                    "active_candidate_count": 0,
                    "governed_source_candidate_count": 0,
                }
            }

        async def refresh(self):
            self.refresh_count += 1
            return {"ok": True}

    gateway = FakeFactorGateway()
    runner = ResearchPlaneRunner(FakeScheduler(), SimpleNamespace())

    artifact = asyncio.run(
        runner.build_factor_research_artifact(
            gateway,
            object(),
            {"_factor_refresh_self_heal": False},
        )
    )

    assert gateway.refresh_count == 0
    assert artifact["freshness_repair"]["refresh_status"] == "disabled"


def test_cycle_runner_keeps_factor_self_heal_env_gated_by_default():
    root = Path(__file__).resolve().parents[3]
    module_path = root / "packages/strategy-factory/src/strategy_factory/application/cycle_runner.py"
    text = module_path.read_text(encoding="utf-8", errors="ignore")

    assert 'os.getenv("STRATEGY_FACTORY_FACTOR_REFRESH_SELF_HEAL", "0")' in text
    assert '"_factor_refresh_self_heal": factor_refresh_self_heal' in text


def test_scheduler_run_once_heartbeats_task_board_during_long_cycle(tmp_path, monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.factory_task_board import FactoryTaskBoard

    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    scheduler = StrategyFactoryScheduler(db_provider=lambda: object())
    scheduler._task_board = board
    heartbeat_calls = {"count": 0}
    original_heartbeat = board.heartbeat

    def _heartbeat(*args, **kwargs):
        heartbeat_calls["count"] += 1
        return original_heartbeat(*args, **kwargs)

    board.heartbeat = _heartbeat
    monkeypatch.setenv("STRATEGY_FACTORY_TASK_BOARD_HEARTBEAT_MIN_SEC", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_TASK_BOARD_HEARTBEAT_SEC", "1")

    async def _execute_factory_run_once_mode(self, resolved_db, **kwargs):
        await asyncio.sleep(1.2)
        return (
            {
                "run_id": "factory_run_heartbeat",
                "status": "success",
                "summary": {},
                "artifact_refs": [],
            },
            [],
        )

    async def _persist_run_result(self, db, results, *, persistence_failures):
        return None

    scheduler._execute_factory_run_once_mode = MethodType(_execute_factory_run_once_mode, scheduler)
    scheduler._persist_run_result = MethodType(_persist_run_result, scheduler)

    result = asyncio.run(scheduler.run_once())

    assert result["status"] == "success"
    assert heartbeat_calls["count"] >= 2
    active = board.reclaim_stale()
    assert active == []
    completed = [
        row
        for row in (
            board.get_task(result["task_board"]["task_id"]),
        )
        if row is not None
    ]
    assert completed[0]["status"] == "completed"


def test_scheduler_run_once_timeout_blocks_task_board(tmp_path, monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.factory_task_board import FactoryTaskBoard

    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    scheduler = StrategyFactoryScheduler(db_provider=lambda: object())
    scheduler._task_board = board
    monkeypatch.setenv("STRATEGY_FACTORY_TASK_BOARD_HEARTBEAT_MIN_SEC", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_TASK_BOARD_HEARTBEAT_SEC", "1")
    scheduler._resolve_run_once_timeout_sec = MethodType(lambda self: 0.01, scheduler)

    async def _execute_factory_run_once_mode(self, resolved_db, **kwargs):
        await asyncio.sleep(1.0)
        return ({"run_id": "factory_run_timeout", "status": "success"}, [])

    scheduler._execute_factory_run_once_mode = MethodType(_execute_factory_run_once_mode, scheduler)

    try:
        asyncio.run(scheduler.run_once())
    except TimeoutError as exc:
        assert "StrategyFactory run_once exceeded" in str(exc)
    else:
        raise AssertionError("run_once should have timed out")

    with board._connect() as conn:
        rows = conn.execute("SELECT * FROM factory_tasks").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "blocked"
    assert "StrategyFactory run_once exceeded" in rows[0]["blocked_reason"]


def test_scheduler_cycle_cancel_persists_interrupted_run_marker(monkeypatch):
    from strategy_factory.application import factory_scheduler as scheduler_module
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    class _DB:
        def __init__(self):
            self.runs = []

        async def save_strategy_factory_run(self, payload):
            self.runs.append(dict(payload))

    class _CancelledRunner:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            raise asyncio.CancelledError()

    db = _DB()
    scheduler = StrategyFactoryScheduler(db_provider=lambda: db)
    monkeypatch.setattr(scheduler_module, "FactoryCycleRunner", _CancelledRunner)

    async def _run():
        try:
            await scheduler._execute_factory_cycle_once(
                db,
                previous_result=None,
                execution_mode="stock_first_observe_primary",
                run_id="factory_run_cancel_marker",
                trace_id="trace-cancel",
            )
        except asyncio.CancelledError:
            return
        raise AssertionError("cycle should have been cancelled")

    asyncio.run(_run())

    statuses = [row["status"] for row in db.runs]
    assert statuses == ["running", "interrupted"]
    assert {row["run_id"] for row in db.runs} == {"factory_run_cancel_marker"}
    assert db.runs[-1]["completed_at"]
    assert "cancelled" in db.runs[-1]["summary"]["reason"]


def test_scheduler_run_once_blocks_orphaned_predecessor_task(tmp_path):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler
    from strategy_factory.application.factory_task_board import FactoryTaskBoard

    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    old_task = board.create_task(
        task_type="research",
        title="Strategy factory run_once",
        payload={"execution_mode": "legacy_primary"},
    )
    old_claim = board.claim_task(old_task["task_id"], worker_id="old-worker", ttl_seconds=1800)
    assert old_claim is not None
    scheduler = StrategyFactoryScheduler(db_provider=lambda: object())
    scheduler._task_board = board

    async def _execute_factory_run_once_mode(self, resolved_db, **kwargs):
        return (
            {
                "run_id": "factory_run_new",
                "status": "success",
                "summary": {},
                "artifact_refs": [],
            },
            [],
        )

    async def _persist_run_result(self, db, results, *, persistence_failures):
        return None

    scheduler._execute_factory_run_once_mode = MethodType(_execute_factory_run_once_mode, scheduler)
    scheduler._persist_run_result = MethodType(_persist_run_result, scheduler)

    result = asyncio.run(scheduler.run_once())

    assert result["status"] == "success"
    old_after = board.get_task(old_task["task_id"])
    assert old_after is not None
    assert old_after["status"] == "blocked"
    assert "superseded" in old_after["blocked_reason"]
    new_after = board.get_task(result["task_board"]["task_id"])
    assert new_after is not None
    assert new_after["status"] == "completed"
    assert isinstance(new_after["payload"].get("owner_pid"), int)


def test_full_market_topn_record_only_skips_strategy_entity(monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    monkeypatch.setenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", "1")

    class _DB:
        def __init__(self):
            self.saved_strategies = []
            self.snapshots = []

        async def save_strategy(self, payload):
            self.saved_strategies.append(dict(payload))
            return payload

        async def save_strategy_factory_topn_snapshot(self, payload):
            row = {"snapshot_id": "snap-1", **dict(payload)}
            self.snapshots.append(row)
            return row

    scheduler = StrategyFactoryScheduler()
    db = _DB()
    failures = []
    results = {
        "run_id": "factory_run_topn_record_only",
        "trace_id": "trace-topn",
        "summary": {},
        "full_market_topn": {
            "available": True,
            "as_of_date": "2026-06-08",
            "topn_n": 1,
            "universe_count": 1,
            "eligible_count": 1,
            "constituents": [
                {"code": "600000", "name": "浦发银行", "score": 91.0},
            ],
        },
    }

    asyncio.run(
        scheduler._persist_full_market_topn(
            db,
            results,
            persistence_failures=failures,
        )
    )

    assert db.saved_strategies == []
    assert db.snapshots
    assert db.snapshots[0]["portfolio_candidate_id"] == "factory_topn_factory_run_topn_record_only"
    assert results["full_market_topn"]["snapshot_id"] == "snap-1"


def test_full_market_topn_can_persist_strategy_when_record_only_disabled(monkeypatch):
    from strategy_factory.application.factory_scheduler import StrategyFactoryScheduler

    monkeypatch.setenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", "0")

    class _DB:
        def __init__(self):
            self.saved_strategies = []

        async def save_strategy(self, payload):
            self.saved_strategies.append(dict(payload))
            return payload

    scheduler = StrategyFactoryScheduler()
    db = _DB()
    results = {
        "run_id": "factory_run_topn_live_entity",
        "full_market_topn": {
            "available": True,
            "as_of_date": "2026-06-08",
            "topn_n": 1,
            "constituents": [{"code": "600000", "score": 91.0}],
        },
    }

    asyncio.run(
        scheduler._persist_full_market_topn(
            db,
            results,
            persistence_failures=[],
        )
    )

    assert db.saved_strategies[0]["id"] == "factory_topn_factory_run_topn_live_entity"
