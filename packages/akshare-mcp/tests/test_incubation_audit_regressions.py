"""Behavior regressions for the canonical incubation / sidecar audit findings."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from aiask_quant_core.storage.sqlite import SQLiteAdapter
from strategy_factory.infrastructure.factory_schema import register_factory_schema
from strategy_factory.runtime.incubation import build_incubation_runtime
from strategy_factory.application.signal_tracker.orchestrator import SignalTrackerOrchestrator
from akshare_mcp.integration.strategy_factory_support import IncubationFactoryRunnerAdapter
from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner
from akshare_mcp.services.incubation_factory.forward_verifier import ForwardVerifier
from akshare_mcp.services.incubation_factory.hit_rate_reporter import HitRateReporter
from akshare_mcp.services.incubation_factory.feedback_writer import FeedbackWriter
from akshare_mcp.services.incubation_factory.metrics_recorder import MetricsRecorder
from akshare_mcp.adapters.signal_tracker.provider import AKShareSignalTrackerProvider
from akshare_mcp.services.signal_tracker import SignalTracker


@pytest_asyncio.fixture
async def store(tmp_path):
    register_factory_schema()
    db = SQLiteAdapter(path=tmp_path / "incubation.sqlite3")
    await db.initialize()
    await db.save_strategy({"id": "s1", "name": "audit", "strategy_type": "momentum", "status": "incubating", "params": {}})
    for i in range(60):
        day = (date(2026, 7, 1) + timedelta(days=i)).isoformat()
        ret = .02 if i < 40 else -.02
        await db.save_strategy_signal_evidence({
            "signal_id": str(i), "evidence_id": "e", "strategy_id": "s1",
            "signal_date": day, "direction": "long", "forward_returns": {"5": ret, "10": ret},
        })
    yield db
    await db.close()


def runner_for(db, monkeypatch):
    monkeypatch.setattr("akshare_mcp.storage.get_db", lambda: db)
    for name in ("STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED", "STRATEGY_FACTORY_HORIZON_SCORING_ENABLED", "STRATEGY_FACTORY_CALIBRATION_ENABLED", "STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED"):
        monkeypatch.setenv(name, "0")
    runner = SimpleNamespace(dry_run=False, run_time=None)
    runner._get_db = AsyncMock(return_value=db)
    runner._get_read_only_db = IncubationFactoryRunner._get_read_only_db.__get__(runner)
    runner._intake = SimpleNamespace(scan_and_accept=AsyncMock(return_value={"accepted": 0}))
    runner._signal_generator = SimpleNamespace(generate=AsyncMock(return_value={"signals_generated": 1, "signal_date": "2026-08-29"}))
    runner._forward_verifier = ForwardVerifier()
    runner._metrics_recorder = SimpleNamespace(record=AsyncMock(return_value={"metrics_recorded": 1}))
    runner._trade_prediction_verifier = SimpleNamespace(verify_pending=AsyncMock(return_value={"verified": 1}))
    runner._reporter = HitRateReporter()
    runner._feedback_writer = FeedbackWriter()
    runner._accelerator = SimpleNamespace(evaluate_batch=AsyncMock(return_value={"accelerated_count": 1}))
    runner._alert_monitor = SimpleNamespace(check=AsyncMock(return_value={}))
    runner._list_incubating = AsyncMock(side_effect=lambda conn: conn.list_strategies("incubating", limit=200))
    # AsyncMock does not await a coroutine returned by a synchronous side_effect.
    async def incubating(conn):
        return await conn.list_strategies("incubating", limit=200)
    runner._list_incubating.side_effect = incubating
    runner._list_paper_observation = AsyncMock(return_value=[])
    runner._list_diagnostic_observation = AsyncMock(return_value=[])
    for name in ("_run_recompile_remediation", "_run_rejected_strategy_position_cleanup", "_run_stale_paper_position_closure", "_run_execution_audit_acceptance", "_start_paper_trading_daemons", "_stop_paper_trading_daemons"):
        setattr(runner, name, AsyncMock(return_value={}))
    runner._run_pipeline = AsyncMock(return_value={"count": 1, "auto_promoted": 0, "stage_counts": {"observe": 1}})
    runner._settle_strategy_orders = AsyncMock(return_value={"filled_count": 0})
    return runner


def table_counts(db):
    conn = db.connection
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}


@pytest.mark.asyncio
async def test_canonical_cycle_executes_pipeline_report_feedback_and_accelerator(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    runtime = build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner))
    result = await runtime.run_once()
    assert result["status"] == "completed", result["phase_failures"]
    assert result["pipeline"]["count"] == 1
    assert result["report"]["summary"]["total_with_signals"] == 1
    assert result["feedback"]["written_events"] > 0
    assert store.connection.execute("SELECT count(*) FROM strategy_domain_events WHERE source='incubation_factory'").fetchone()[0] > 0
    pipeline_strategies = runner._run_pipeline.await_args.kwargs["strategies"]
    assert [item["id"] for item in pipeline_strategies] == ["s1"]
    _, accelerated_strategies, verifications = runner._accelerator.evaluate_batch.await_args.args
    assert [item["id"] for item in accelerated_strategies] == ["s1"]
    assert verifications["s1"]["primary_effective_n"] == 60
    # An acceleration recommendation is not a completed promotion.
    assert result["strategies_promoted"] == 0
    phases = [phase["phase_name"] for phase in result["phases"]]
    assert phases.index("execution_audit_acceptance") < phases.index("pipeline") < phases.index("hit_rate_report") < phases.index("feedback_write")


@pytest.mark.asyncio
async def test_canonical_cycle_persists_real_pipeline_snapshot(store, monkeypatch):
    from akshare_mcp.services import governance_persistence

    runner = runner_for(store, monkeypatch)
    runner.auto_apply_review = False
    runner._run_pipeline = IncubationFactoryRunner._run_pipeline.__get__(runner)
    monkeypatch.setattr(governance_persistence, "get_db", lambda: store)
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "completed", result["phase_failures"]
    assert result["pipeline"]["count"] == 1
    snapshot = await store.get_latest_strategy_incubation_pipeline_snapshot("s1")
    assert snapshot is not None
    assert snapshot["source"] == "incubation_factory"
    assert result["pipeline"]["items"][0]["snapshot"]["id"] == snapshot["id"]
    assert result["feedback"]["written_events"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("construction_dry_run", [False, True])
async def test_dry_run_reads_evidence_without_any_business_write(store, monkeypatch, construction_dry_run):
    runner = runner_for(store, monkeypatch)
    reporter_write = AsyncMock(side_effect=AssertionError("report write attempted in dry-run"))
    monkeypatch.setattr(runner._reporter, "_persist_report", reporter_write)
    before = table_counts(store)
    runtime = build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner), dry_run=construction_dry_run)
    result = await runtime.run_once(**({} if construction_dry_run else {"dry_run": True}))
    assert result["status"] == "completed", result["phase_failures"]
    assert result["dry_run"] is True
    assert result["report"]["summary"]["total_with_signals"] == 1
    assert result["verification"]["metrics_recorded"] == 0
    assert table_counts(store) == before
    runner._get_db.assert_not_awaited()
    reporter_write.assert_not_awaited()
    for method in (runner._intake.scan_and_accept, runner._signal_generator.generate, runner._settle_strategy_orders, runner._metrics_recorder.record, runner._trade_prediction_verifier.verify_pending, runner._run_pipeline, runner._run_recompile_remediation, runner._run_rejected_strategy_position_cleanup, runner._run_stale_paper_position_closure, runner._run_execution_audit_acceptance, runner._accelerator.evaluate_batch, runner._alert_monitor.check):
        method.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategy_error_fails_cycle_and_is_not_submitted_to_pipeline(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    runner._signal_generator.generate.side_effect = RuntimeError("generation broke")
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "failed"
    assert any(item.get("strategy_id") == "s1" and "generation broke" in item["error"] for item in result["phase_failures"])
    assert runner._run_pipeline.await_args.kwargs["strategies"] == []


@pytest.mark.asyncio
async def test_settlement_error_is_not_hidden(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    runner._settle_strategy_orders.side_effect = RuntimeError("settlement broke")
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "failed"
    assert any(item["phase"] == "settlement" for item in result["phase_failures"])
    assert runner._run_pipeline.await_args.kwargs["strategies"] == []


@pytest.mark.asyncio
async def test_latest_losses_are_recent_from_real_descending_storage(store):
    rows = await store.list_strategy_signal_evidence(strategy_id="s1", limit=500)
    assert rows[0]["signal_date"] > rows[-1]["signal_date"]
    result = await ForwardVerifier().verify(store, {"id": "s1", "strategy_type": "momentum"})
    assert result["primary_effective_n"] == 60
    assert result["primary_hit_rate"] == pytest.approx(2 / 3, abs=.0001)
    assert result["recent_primary_hit_rate"] == 0
    assert result["recent_primary_skill_lcb"] < 0


@pytest.mark.asyncio
async def test_sidecar_honors_provider_horizons_and_counts_real_completions(monkeypatch):
    import akshare_mcp.services.signal_tracker as host
    monkeypatch.setenv("STRATEGY_FACTORY_FORWARD_DAYS", "1,5,10,20,40")
    monkeypatch.setattr(host, "FORWARD_DAYS", host._resolve_forward_days())
    windows = []
    class Db:
        async def get_pending_forward_returns(self, forward_days, **kwargs):
            windows.append(forward_days)
            return []
    tracker = SignalTracker()
    concrete = AKShareSignalTrackerProvider(tracker)
    provider = SimpleNamespace(get_db=AsyncMock(return_value=Db()), phase_timeout_seconds=lambda *_: 1, get_default_universe=lambda: [])
    for name in ("load_executable_strategies", "load_runtime_submitted_strategies", "load_runtime_observation_strategies"):
        setattr(provider, name, AsyncMock(return_value=[]))
    for name in ("run_runtime_risk_scan", "run_lifecycle_scan", "reconcile_vector_registry", "snapshot_domain_projections"):
        setattr(provider, name, AsyncMock(return_value={}))
    provider.backfill_forward_returns = concrete.backfill_forward_returns
    result = await SignalTrackerOrchestrator(provider).run_cycle(as_of=date(2026, 10, 30))
    assert windows == [1, 5, 10, 20, 40], result
    assert result["errors"] == []
    monkeypatch.setattr(tracker, "_backfill_forward_returns", AsyncMock(return_value={"computed": 42}))
    result = await SignalTrackerOrchestrator(provider).run_cycle(as_of=date(2026, 10, 30))
    assert result["forward_returns_computed"] == 42


@pytest.mark.asyncio
async def test_returned_strategy_failure_is_not_silently_completed(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    runner._metrics_recorder.record.return_value = {"status": "failed", "error": "metric write failed"}
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "failed"
    assert result["verification"]["metrics_recorded"] == 0
    assert "metric write failed" in result["phase_failures"][0]["error"]


@pytest.mark.asyncio
async def test_pipeline_returned_error_marks_cycle_partial_with_cause(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    runner._run_pipeline.return_value = {"status": "failed", "error": "snapshot write failed"}
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "partial"
    assert {"phase": "pipeline", "error": "snapshot write failed"} in result["phase_failures"]


@pytest.mark.asyncio
async def test_existing_runner_dry_run_is_preserved_by_runtime_builder(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    runner.dry_run = True
    before = table_counts(store)
    runtime = build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner))
    result = await runtime.run_once(dry_run=False)
    assert result["dry_run"] is True
    assert result["status"] == "completed", result["phase_failures"]
    assert table_counts(store) == before
    runner._get_db.assert_not_awaited()
    runner._signal_generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_fails_closed_when_provider_has_no_read_only_connection():
    from strategy_factory.application.incubation.orchestrator import IncubationOrchestrator
    support = SimpleNamespace(get_db=AsyncMock(side_effect=AssertionError("must not bootstrap")))
    with pytest.raises(RuntimeError, match="dry_run_requires_read_only_database_provider"):
        await IncubationOrchestrator(support).run_cycle(dry_run=True)
    support.get_db.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_provider", [False, True])
@pytest.mark.parametrize("returns_none", [False, True])
async def test_real_metric_write_failure_excludes_strategy_from_promotion(store, monkeypatch, legacy_provider, returns_none):
    runner = runner_for(store, monkeypatch)
    runner._metrics_recorder = MetricsRecorder()
    writer = AsyncMock(return_value=None) if returns_none else AsyncMock(side_effect=RuntimeError("disk write rejected"))
    monkeypatch.setattr(store, "save_strategy_incubation_metric", writer)
    support = runner if legacy_provider else IncubationFactoryRunnerAdapter(runner)
    result = await build_incubation_runtime(support=support).run_once()
    assert result["status"] == "failed", result
    assert result["verification"]["metrics_recorded"] == 0
    cause = "no saved record" if returns_none else "disk write rejected"
    assert any(cause in failure["error"] for failure in result["phase_failures"])
    assert runner._run_pipeline.await_args.kwargs["strategies"] == []
    assert runner._accelerator.evaluate_batch.await_args.args[1] == []
    assert store.connection.execute("SELECT count(*) FROM strategy_incubation_metrics").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_real_report_write_failure_is_partial_and_does_not_write_feedback(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    writer = AsyncMock(side_effect=RuntimeError("report disk rejected"))
    monkeypatch.setattr(store, "save_strategy_domain_event", writer)
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "partial"
    assert any(failure["phase"] == "hit_rate_report" and "report disk rejected" in failure["error"] for failure in result["phase_failures"])
    assert result["feedback"] == {}
    assert writer.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_both", [False, True])
async def test_real_feedback_write_failures_report_only_persisted_event_counts(store, monkeypatch, fail_both):
    runner = runner_for(store, monkeypatch)
    save_event = store.save_strategy_domain_event

    async def save(payload):
        kind = payload["event_type"]
        if kind == "incubation_factory.prediction_feedback_written" or (fail_both and kind == "incubation_factory.feedback_written"):
            raise RuntimeError("feedback disk rejected")
        return await save_event(payload)

    monkeypatch.setattr(store, "save_strategy_domain_event", save)
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "partial"
    assert any(failure["phase"] == "feedback_write" and "feedback disk rejected" in failure["error"] for failure in result["phase_failures"])
    feedback = result["feedback"]
    assert feedback["status"] == ("failed" if fail_both else "partial")
    assert feedback["written_events"] == (0 if fail_both else 1)
    actual = store.connection.execute("SELECT count(*) FROM strategy_domain_events WHERE event_type IN ('incubation_factory.feedback_written', 'incubation_factory.prediction_feedback_written')").fetchone()[0]
    assert actual == feedback["written_events"]


@pytest.mark.asyncio
async def test_real_feedback_snapshot_failure_is_not_hidden(store, monkeypatch):
    runner = runner_for(store, monkeypatch)
    monkeypatch.setattr(store, "save_strategy_closure_snapshot", AsyncMock(side_effect=RuntimeError("snapshot disk rejected")), raising=False)
    result = await build_incubation_runtime(support=IncubationFactoryRunnerAdapter(runner)).run_once()
    assert result["status"] == "partial"
    assert result["feedback"]["written_events"] == 2
    assert result["feedback"]["written_snapshots"] == 0
    assert result["feedback"]["snapshot_status"] == "failed"
    assert "snapshot disk rejected" in result["feedback"]["error"]


@pytest.mark.asyncio
async def test_real_feedback_control_failure_preserves_successful_event_count(store, monkeypatch):
    monkeypatch.setattr(store, "save_strategy_runtime_control", AsyncMock(side_effect=RuntimeError("control disk rejected")))
    result = await FeedbackWriter().write(store, {"feedback_actions": {"families_to_freeze": ["momentum"]}})
    assert result["status"] == "partial"
    assert result["written_events"] == 1
    assert result["written_controls"] == 0
    assert "control disk rejected" in result["error"]


@pytest.mark.asyncio
async def test_metric_batch_counts_real_storage_errors_as_failures(store, monkeypatch):
    monkeypatch.setattr(store, "save_strategy_incubation_metric", AsyncMock(side_effect=RuntimeError("batch disk rejected")))
    result = await MetricsRecorder().record_batch(store, [{"id": "s1"}], {})
    assert result["recorded"] == 0
    assert result["failed"] == 1
