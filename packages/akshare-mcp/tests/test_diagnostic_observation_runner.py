from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from akshare_mcp.services.incubation_factory.accelerator import IncubationAccelerator
from akshare_mcp.services.incubation_factory.metrics_recorder import MetricsRecorder
from akshare_mcp.services.incubation_factory.runner import IncubationFactoryRunner
from akshare_mcp.services.incubation_factory.signal_generator import SignalGenerator
from akshare_mcp.services.incubation_pipeline import StrategyIncubationPipelineService


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("INCUBATION_FACTORY_DIAGNOSTIC_INTAKE_ENABLED", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_DIAGNOSTIC_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_ENABLED", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_ENABLED", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_BACKFILL_ENABLED", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_GRACE_DAYS", raising=False)
    yield


@pytest.mark.asyncio
async def test_runner_diagnostic_intake_disabled_by_default():
    runner = IncubationFactoryRunner(dry_run=True)
    db = MagicMock()
    db.list_diagnostic_observation_strategies = AsyncMock(return_value=[{"id": "s1"}])

    result = await runner._list_diagnostic_observation(db)

    assert result == []
    db.list_diagnostic_observation_strategies.assert_not_called()


@pytest.mark.asyncio
async def test_runner_diagnostic_intake_enabled_uses_batch_limit(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_DIAGNOSTIC_INTAKE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_DIAGNOSTIC_BATCH_LIMIT", "4")
    runner = IncubationFactoryRunner(dry_run=True)
    db = MagicMock()
    db.list_diagnostic_observation_strategies = AsyncMock(return_value=[{"id": "s1"}])

    result = await runner._list_diagnostic_observation(db)

    assert result == [{"id": "s1"}]
    db.list_diagnostic_observation_strategies.assert_called_once_with(limit=4)


@pytest.mark.asyncio
async def test_runner_diagnostic_intake_db_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_DIAGNOSTIC_INTAKE_ENABLED", "1")
    runner = IncubationFactoryRunner(dry_run=True)
    db = MagicMock()
    db.list_diagnostic_observation_strategies = AsyncMock(side_effect=RuntimeError("boom"))

    result = await runner._list_diagnostic_observation(db)

    assert result == []


@pytest.mark.asyncio
async def test_runner_recompile_remediation_enabled_by_default(monkeypatch):
    calls = []

    async def fake_backfill(db, **kwargs):
        calls.append({"db": db, "kwargs": dict(kwargs)})
        return {
            "scanned": 2,
            "recompiled": 1,
            "promoted_to_formal": 1,
            "revision_required": 0,
            "updated": 1,
            "dry_run": bool(kwargs.get("dry_run")),
        }

    monkeypatch.setenv("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_BATCH_LIMIT", "7")
    monkeypatch.setattr(
        "akshare_mcp.services.strategy_recompile_backfill.backfill_historical_trend_strategies",
        fake_backfill,
    )
    runner = IncubationFactoryRunner(dry_run=True)
    db = MagicMock()

    result = await runner._run_recompile_remediation(db)

    assert result["status"] == "ok"
    assert result["promoted_to_formal"] == 1
    assert calls == [
        {
            "db": db,
            "kwargs": {
                "statuses": ["submitted"],
                "limit": 7,
                "dry_run": True,
                "measure_profile": True,
                "promote_ready": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_runner_records_diagnostic_processed_event():
    runner = IncubationFactoryRunner(dry_run=True)
    db = MagicMock()
    db.save_strategy_domain_event = AsyncMock()

    await runner._record_diagnostic_processed_event(
        db,
        {"id": "s1", "name": "diag", "strategy_type": "momentum"},
        {"primary_hit_rate": 0.35, "primary_skill_lcb": -0.01, "coverage_ratio": 0.7},
        {"signals_generated": 2},
    )

    db.save_strategy_domain_event.assert_called_once()
    event = db.save_strategy_domain_event.call_args[0][0]
    assert event["event_type"] == "incubation_factory.diagnostic_observation_processed"
    assert event["source"] == "incubation_factory_diagnostic"
    assert event["payload"]["stage"] == "diagnostic"
    assert event["payload"]["diagnostic_observation"] is True
    assert event["payload"]["signals_generated"] == 2


def test_metrics_recorder_uses_diagnostic_intake_stage():
    metric = MetricsRecorder()._build_metric(
        strategy={"id": "s1", "status": "submitted", "_intake_stage": "diagnostic"},
        verification={
            "primary_hit_rate": 0.35,
            "primary_skill_lcb": -0.01,
            "coverage_ratio": 0.7,
            "primary_effective_n": 3,
        },
        nav_info={},
        account_id="acct-1",
        metric_date=date(2026, 5, 29),
    )

    assert metric["stage"] == "diagnostic"
    assert metric["metadata"]["intake_stage"] == "diagnostic"
    assert metric["metadata"]["diagnostic_observation"] is True


def test_metrics_recorder_sanitizes_non_finite_values():
    metric = MetricsRecorder()._build_metric(
        strategy={"id": "s1", "status": "submitted", "_intake_stage": "diagnostic"},
        verification={
            "primary_hit_rate": "nan",
            "primary_skill_lcb": float("inf"),
            "recent_primary_skill_lcb": "-inf",
            "secondary_hit_rate": float("nan"),
            "secondary_skill_lcb": "inf",
            "stability_gap": "nan",
            "coverage_ratio": "-inf",
            "forward_sharpe": float("inf"),
            "forward_ic": float("nan"),
            "primary_effective_n": float("inf"),
            "secondary_effective_n": "nan",
            "total_signals": "-inf",
            "profile": float("nan"),
            "min_days_remaining": float("inf"),
        },
        nav_info={
            "total_value": "inf",
            "cash": float("nan"),
            "market_value": "-inf",
            "nav": "nan",
            "daily_return": float("inf"),
            "max_drawdown": "-inf",
        },
        account_id="acct-1",
        metric_date=date(2026, 5, 29),
    )

    assert metric["decision"] == "observe"
    assert metric["primary_effective_n"] == 0
    assert metric["metadata"]["profile"] is None
    json.dumps(metric, allow_nan=False)


@pytest.mark.asyncio
async def test_accelerator_rejects_non_finite_verification_metrics():
    class Db:
        async def list_strategy_incubation_metrics(self, strategy_id, limit):
            return [{"decision": "promote"} for _ in range(limit)]

    result = await IncubationAccelerator()._evaluate_single(
        Db(),
        {"id": "s1", "name": "bad"},
        {
            "primary_skill_lcb": float("inf"),
            "recent_primary_skill_lcb": "inf",
            "stability_gap": "-inf",
            "coverage_ratio": "inf",
            "primary_effective_n": 100,
        },
    )

    assert result["eligible"] is False
    assert result["reason"] == "skill_lcb_too_low"


@pytest.mark.asyncio
async def test_runner_pipeline_passes_paper_observation_strategies(monkeypatch):
    calls = {}

    class Pipeline:
        async def run_batch(self, db, **kwargs):
            calls.update(kwargs)
            return {
                "count": len(kwargs.get("strategies") or []),
                "auto_promoted": 0,
                "stage_counts": {},
            }

    monkeypatch.setattr(
        "akshare_mcp.services.incubation_pipeline.get_strategy_incubation_pipeline_service",
        lambda: Pipeline(),
    )
    runner = IncubationFactoryRunner(dry_run=False)
    strategies = [
        {"id": "incubating-1", "status": "incubating"},
        {"id": "paper-1", "status": "submitted", "_intake_stage": "paper"},
    ]

    result = await runner._run_pipeline(MagicMock(), strategies=strategies)

    assert result["count"] == 2
    assert calls["statuses"] == ["incubating"]
    assert [item["id"] for item in calls["strategies"]] == ["incubating-1", "paper-1"]
    assert calls["source"] == "incubation_factory"


@pytest.mark.asyncio
async def test_runner_execution_audit_acceptance_runs_bounded_backfill(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_BATCH_LIMIT", "2")
    calls = []

    class Db:
        async def run_execution_audit_acceptance(self, *, strategy_id=None, backfill=True):
            calls.append((strategy_id, backfill))
            return {
                "status": "ready" if strategy_id == "s1" else "pending_data",
                "execution_audit_gate_status": "passed" if strategy_id == "s1" else "bootstrap_pending",
                "execution_hard_gate_passed": strategy_id == "s1",
                "acceptance_matrix": {
                    "overall_ready": strategy_id == "s1",
                    "native_lineage_ready": True,
                    "trade_evidence_ready": strategy_id == "s1",
                },
                "backfill_result": {
                    "native_signal_evidence": {
                        "saved_signal_count": 2 if strategy_id == "s1" else 3,
                        "proxy_backfilled_signal_count": 1,
                        "compile_stable_signal_count": 1 if strategy_id == "s1" else 0,
                    },
                },
                "gap_categories": ["sample_gap"],
                "blockers": ["execution_audit_bootstrap_pending"],
                "execution_audit_snapshot_id": f"eas-{strategy_id}",
            }

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_execution_audit_acceptance(
        Db(),
        strategies=[
            {"id": "s1"},
            {"id": "s1"},
            {"id": "s2"},
            {"id": "s3"},
        ],
    )

    assert calls == [("s1", True), ("s2", True)]
    assert result["status"] == "ok"
    assert result["candidate_count"] == 3
    assert result["selected_count"] == 2
    assert result["evaluated"] == 2
    assert result["saved_signal_evidence_count"] == 5
    assert result["hard_gate_passed_count"] == 1
    assert result["native_lineage_ready_count"] == 2
    assert result["trade_evidence_ready_count"] == 1
    assert result["status_counts"] == {"ready": 1, "pending_data": 1}
    assert result["gate_status_counts"] == {"passed": 1, "bootstrap_pending": 1}


@pytest.mark.asyncio
async def test_runner_run_once_settles_orders_before_verification(monkeypatch):
    events: list[str] = []
    db = MagicMock()
    runner = IncubationFactoryRunner(dry_run=False)

    runner._get_db = AsyncMock(return_value=db)
    runner._close_db = AsyncMock()
    runner._intake.scan_and_accept = AsyncMock(return_value={"accepted": 0})
    runner._run_recompile_remediation = AsyncMock(return_value={})
    runner._list_incubating = AsyncMock(return_value=[{"id": "s1", "status": "incubating"}])
    runner._list_paper_observation = AsyncMock(return_value=[])
    runner._list_diagnostic_observation = AsyncMock(return_value=[])

    async def generate(_db, strategy):
        events.append("generate")
        return {
            "strategy_id": strategy["id"],
            "signal_date": "2026-06-19",
            "signals_generated": 1,
            "orders_created": 1,
        }

    async def settle(_db, strategy, *, signal_result=None):
        events.append("settle")
        return {
            "strategy_id": strategy["id"],
            "signal_date": signal_result["signal_date"],
            "filled_count": 1,
            "rejected_count": 0,
        }

    async def verify(_db, strategy):
        events.append("verify")
        return {"strategy_id": strategy["id"], "primary_effective_n": 1}

    runner._signal_generator.generate = AsyncMock(side_effect=generate)
    runner._settle_strategy_orders = AsyncMock(side_effect=settle)
    runner._forward_verifier.verify = AsyncMock(side_effect=verify)
    runner._metrics_recorder.record = AsyncMock(return_value={"stage": "warmup"})
    runner._trade_prediction_verifier = MagicMock()
    runner._trade_prediction_verifier.verify_pending = AsyncMock(return_value={})
    runner._run_stale_paper_position_closure = AsyncMock(return_value={})
    runner._run_execution_audit_acceptance = AsyncMock(return_value={})
    runner._run_execution_audit_remediation = AsyncMock(return_value={})
    runner._run_pipeline = AsyncMock(return_value={"count": 1, "auto_promoted": 0, "stage_counts": {"warmup": 1}})
    runner._reporter.generate = AsyncMock(return_value={"hit_rate_dashboard": {"overall": {}}})
    runner._feedback_writer.write = AsyncMock(return_value={})
    runner._accelerator.evaluate_batch = AsyncMock(return_value={})
    runner._alert_monitor.check = AsyncMock(return_value={})
    runner._heartbeat = AsyncMock(return_value=None)

    result = await runner.run_once()

    assert events == ["generate", "settle", "verify"]
    assert result["settlement"]["evaluated"] == 1
    assert result["settlement"]["filled"] == 1
    assert result["settlement"]["errors"] == 0


@pytest.mark.asyncio
async def test_signal_generator_reports_created_count_as_orders_created(monkeypatch):
    class Service:
        async def sync_signals_to_orders(self, db, strategy, signal_date):
            return {
                "strategy_id": strategy["id"],
                "created_count": 2,
                "signals_generated": 3,
                "errors": [],
            }

    monkeypatch.setattr(
        "akshare_mcp.services.incubation.get_strategy_incubation_service",
        lambda: Service(),
    )

    result = await SignalGenerator().generate(MagicMock(), {"id": "s1"}, signal_date=date(2026, 6, 19))

    assert result["signals_generated"] == 3
    assert result["orders_created"] == 2


@pytest.mark.asyncio
async def test_runner_signal_only_backlog_processes_strategies_by_latest_signal_date(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_BATCH_LIMIT", "10")
    calls = []

    class Service:
        async def process_strategies(self, db, strategies, signal_date=None):
            calls.append((signal_date, [item["id"] for item in strategies]))
            return {
                "count": len(strategies),
                "orders_created": len(strategies),
                "orders_filled": len(strategies),
                "rejected_orders": 0,
                "metrics_recorded": len(strategies),
                "skip_reason_counts": {"duplicate_order": 1},
                "items": [
                    {
                        "strategy_id": item["id"],
                        "orders_created": 1,
                        "orders_filled": 1,
                    }
                    for item in strategies
                ],
            }

    import akshare_mcp.services.incubation as incubation_module

    monkeypatch.setattr(
        incubation_module,
        "get_strategy_incubation_service",
        lambda: Service(),
    )

    class Db:
        async def get_signals(self, strategy_id, limit=1):
            if strategy_id == "with-order":
                return [{"signal_date": date(2026, 6, 18), "signal": 1}]
            return [{"signal_date": date(2026, 6, 19), "signal": 1}]

        async def list_strategy_paper_orders(self, strategy_id, limit=1):
            return [{"id": 1}] if strategy_id == "with-order" else []

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_signal_only_paper_execution_backlog(
        Db(),
        strategies=[
            {"id": "signal-only-a"},
            {"id": "with-order"},
            {"id": "signal-only-b"},
        ],
    )

    assert calls == [(date(2026, 6, 19), ["signal-only-a", "signal-only-b"])]
    assert result["status"] == "ok"
    assert result["signal_only_backlog_count"] == 2
    assert result["orders_created"] == 2
    assert result["orders_filled"] == 2
    assert result["metrics_recorded"] == 2
    assert result["skip_reason_counts"] == {"duplicate_order": 1}


@pytest.mark.asyncio
async def test_runner_native_evidence_backfill_runs_before_acceptance_candidates(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_BATCH_LIMIT", "10")
    calls = []

    class Db:
        async def list_strategy_paper_trades(self, strategy_id, limit=1):
            return [{"id": "trade-1"}] if strategy_id == "needs-evidence" else []

        async def list_strategy_trade_positions(self, *, strategy_id=None, limit=1):
            return []

        async def list_strategy_signal_evidence(self, *, strategy_id=None, limit=1):
            return []

        async def backfill_strategy_signal_evidence_native(self, strategy_id=None, limit=5000):
            calls.append(strategy_id)
            return {
                "status": "ok",
                "saved_signal_count": 2,
                "saved_row_count": 2,
                "proxy_backfilled_signal_count": 2,
                "compile_stable_signal_count": 0,
                "initial_existing_signal_count": 0,
            }

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_native_execution_evidence_backfill(
        Db(),
        strategies=[
            {"id": "needs-evidence"},
            {"id": "no-trade"},
        ],
    )

    assert calls == ["needs-evidence"]
    assert result["status"] == "ok"
    assert result["trades_without_signal_evidence_count"] == 1
    assert result["saved_signal_evidence_count"] == 2
    assert result["proxy_backfilled_signal_count"] == 2


@pytest.mark.asyncio
async def test_runner_execution_audit_remediation_bootstraps_and_reruns_acceptance(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_EXECUTION_AUDIT_REMEDIATION_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_EXECUTION_AUDIT_REMEDIATION_BATCH_LIMIT", "3")
    monkeypatch.setenv("INCUBATION_FACTORY_EXECUTION_AUDIT_REMEDIATION_TARGET_TRADE_COUNT", "20")
    calls = {"bootstrap": [], "remediate": [], "acceptance": []}

    class Service:
        async def bootstrap_import_strategy(self, db, strategy_id, **kwargs):
            calls["bootstrap"].append((strategy_id, kwargs))
            return {"strategy_id": strategy_id, "imported_round_trips": 4}

        async def remediate_failed_metrics_strategy(self, db, strategy_id):
            calls["remediate"].append(strategy_id)
            return {"strategy_id": strategy_id, "updated": True}

    monkeypatch.setattr(
        "akshare_mcp.services.strategy_acceptance_remediation.get_strategy_acceptance_remediation_service",
        lambda: Service(),
    )

    class Db:
        async def run_execution_audit_acceptance(self, *, strategy_id=None, backfill=True):
            calls["acceptance"].append((strategy_id, backfill))
            return {
                "execution_audit_gate_status": "passed",
                "execution_hard_gate_passed": True,
            }

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_execution_audit_remediation(
        Db(),
        strategies=[{"id": "sample-gap"}, {"id": "bad-metrics"}],
        acceptance_result={
            "items": [
                {"strategy_id": "sample-gap", "execution_audit_gate_status": "bootstrap_ready"},
                {"strategy_id": "bad-metrics", "execution_audit_gate_status": "failed_metrics"},
                {"strategy_id": "passed", "execution_audit_gate_status": "passed"},
            ]
        },
    )

    assert calls["bootstrap"] == [("sample-gap", {"target_trade_count": 20})]
    assert calls["remediate"] == ["bad-metrics"]
    assert calls["acceptance"] == [("sample-gap", True), ("bad-metrics", True)]
    assert result["status"] == "ok"
    assert result["evaluated"] == 2
    assert result["bootstrap_imported_round_trips"] == 4
    assert result["remediation_updated_count"] == 1
    assert result["post_acceptance_gate_counts"] == {"passed": 2}


@pytest.mark.asyncio
async def test_runner_stale_paper_position_closure_disabled_by_default():
    runner = IncubationFactoryRunner(dry_run=False)
    db = MagicMock()
    db.list_strategy_trade_positions = AsyncMock(return_value=[])

    result = await runner._run_stale_paper_position_closure(
        db,
        strategies=[{"id": "s1", "risk_rules": {"max_holding_days": 2}}],
        as_of=date(2026, 6, 19),
    )

    assert result == {"status": "skipped", "reason": "disabled", "evaluated": 0}
    db.list_strategy_trade_positions.assert_not_called()


@pytest.mark.asyncio
async def test_runner_stale_paper_position_closure_closes_only_time_stop_codes(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT", "3")
    close_calls = []

    class Service:
        async def force_close_open_positions(self, db, strategy, signal_date, **kwargs):
            close_calls.append((strategy["id"], signal_date, kwargs))
            return {"created_count": len(kwargs.get("codes") or []), "skipped_count": 0}

    import akshare_mcp.services.incubation as incubation_module

    monkeypatch.setattr(
        incubation_module,
        "get_strategy_incubation_service",
        lambda: Service(),
    )

    class Db:
        async def list_strategy_trade_positions(self, *, strategy_id=None, status=None, limit=200):
            assert status == "open"
            if strategy_id == "stale":
                return [
                    {"code": "000001", "status": "open", "opened_at": "2026-06-10T09:30:00+00:00"},
                    {"code": "000002", "status": "open", "opened_at": "2026-06-18T09:30:00+00:00"},
                ]
            if strategy_id == "fresh":
                return [
                    {"code": "000003", "status": "open", "opened_at": "2026-06-18T09:30:00+00:00"},
                ]
            return []

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_stale_paper_position_closure(
        Db(),
        strategies=[
            {"id": "stale", "runtime_playbook": {"exit_policy": {"time_stop_days": 3}}},
            {"id": "fresh", "risk_rules": {"max_holding_days": 3}},
            {"id": "stale", "risk_rules": {"max_holding_days": 3}},
        ],
        as_of=date(2026, 6, 19),
    )

    assert len(close_calls) == 1
    assert close_calls[0][0] == "stale"
    assert close_calls[0][1] == date(2026, 6, 19)
    assert close_calls[0][2]["source"] == "incubation_factory_stale_close"
    assert close_calls[0][2]["reason"] == "stale_paper_position_time_stop"
    assert close_calls[0][2]["codes"] == ["000001"]
    assert result["status"] == "ok"
    assert result["candidate_count"] == 2
    assert result["selected_count"] == 2
    assert result["evaluated"] == 1
    assert result["stale_position_count"] == 1
    assert result["created_count"] == 1
    assert result["skip_reasons"] == {"no_stale_open_positions": 1}


@pytest.mark.asyncio
async def test_runner_stale_paper_position_closure_settles_and_records_metrics(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED", "1")
    calls = []

    class Service:
        async def force_close_open_positions(self, db, strategy, signal_date, **kwargs):
            calls.append(("close", strategy["id"], signal_date, tuple(kwargs.get("codes") or [])))
            return {"created_count": 1, "skipped_count": 0}

        async def settle_orders(self, db, strategy, signal_date):
            calls.append(("settle", strategy["id"], signal_date))
            return {"filled_count": 1, "rejected_count": 0}

        async def record_metrics(self, db, strategy, metric_date):
            calls.append(("metrics", strategy["id"], metric_date))
            return {"strategy_id": strategy["id"], "metric_date": str(metric_date)}

    import akshare_mcp.services.incubation as incubation_module

    monkeypatch.setattr(
        incubation_module,
        "get_strategy_incubation_service",
        lambda: Service(),
    )

    class Db:
        async def list_strategy_trade_positions(self, *, strategy_id=None, status=None, limit=200):
            return [
                {
                    "code": "000001",
                    "status": "open",
                    "opened_at": "2026-06-01T09:30:00+00:00",
                }
            ]

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_stale_paper_position_closure(
        Db(),
        strategies=[{"id": "stale", "risk_rules": {"max_holding_days": 3}}],
        as_of=date(2026, 6, 19),
    )

    assert calls == [
        ("close", "stale", date(2026, 6, 19), ("000001",)),
        ("settle", "stale", date(2026, 6, 19)),
        ("metrics", "stale", date(2026, 6, 19)),
    ]
    assert result["created_count"] == 1
    assert result["orders_filled"] == 1
    assert result["metrics_recorded"] == 1
    assert result["closed_round_trip_candidates"] == 1


@pytest.mark.asyncio
async def test_runner_execution_audit_acceptance_keeps_per_strategy_errors_nonfatal():
    calls = []

    class Db:
        async def run_execution_audit_acceptance(self, *, strategy_id=None, backfill=True):
            calls.append(strategy_id)
            if strategy_id == "bad":
                raise RuntimeError("audit boom")
            return {
                "status": "pending_data",
                "execution_audit_gate_status": "bootstrap_pending",
                "execution_hard_gate_passed": False,
                "acceptance_matrix": {
                    "overall_ready": False,
                    "native_lineage_ready": False,
                    "trade_evidence_ready": False,
                },
                "backfill_result": {"native_signal_evidence": {"saved_signal_count": 0}},
            }

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_execution_audit_acceptance(
        Db(),
        strategies=[{"id": "ok"}, {"id": "bad"}],
    )

    assert calls == ["ok", "bad"]
    assert result["status"] == "partial"
    assert result["evaluated"] == 1
    assert result["errors"] == 1
    assert result["error_items"][0]["strategy_id"] == "bad"
    assert result["gate_status_counts"] == {"bootstrap_pending": 1}


@pytest.mark.asyncio
async def test_runner_execution_audit_acceptance_counts_existing_signal_evidence():
    class Db:
        async def run_execution_audit_acceptance(self, *, strategy_id=None, backfill=True):
            return {
                "status": "pending_data",
                "execution_audit_gate_status": "bootstrap_pending",
                "execution_hard_gate_passed": False,
                "acceptance_matrix": {
                    "overall_ready": False,
                    "native_lineage_ready": True,
                    "trade_evidence_ready": False,
                },
                "verification": {
                    "coverage": {"strategy_signal_evidence_count": 1},
                },
                "backfill_result": {
                    "native_signal_evidence": {
                        "status": "no_op",
                        "initial_existing_signal_count": 1,
                        "saved_signal_count": 0,
                    },
                },
            }

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_execution_audit_acceptance(
        Db(),
        strategies=[{"id": "existing-evidence"}],
    )

    assert result["status"] == "pending_evidence"
    assert result["blockers"] == []
    assert result["sample_blockers"] == [
        "execution_hard_gate_pending",
        "trade_evidence_not_ready",
    ]
    assert result["saved_signal_evidence_count"] == 0
    assert result["available_signal_evidence_count"] == 1
    assert result["items"][0]["available_signal_evidence_count"] == 1


@pytest.mark.asyncio
async def test_runner_execution_audit_acceptance_keeps_signal_only_out_of_gate_counts(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_EXECUTION_AUDIT_ACCEPTANCE_BATCH_LIMIT", "3")
    calls = []

    class Db:
        async def list_strategy_paper_trades(self, strategy_id, limit=1):
            return [{"id": "t1"}] if strategy_id == "with-trade" else []

        async def list_strategy_trade_positions(self, *, strategy_id=None, limit=1):
            return []

        async def list_strategy_paper_orders(self, strategy_id, limit=1):
            return [{"id": "o1"}] if strategy_id in {"with-trade", "order-only"} else []

        async def get_signals(self, strategy_id, limit=1):
            return [{"id": "sig1"}] if strategy_id in {"with-trade", "order-only", "signal-only"} else []

        async def run_execution_audit_acceptance(self, *, strategy_id=None, backfill=True):
            calls.append(strategy_id)
            return {
                "status": "pending_data",
                "execution_audit_gate_status": "bootstrap_pending",
                "execution_hard_gate_passed": False,
                "acceptance_matrix": {
                    "overall_ready": False,
                    "native_lineage_ready": True,
                    "trade_evidence_ready": False,
                },
                "verification": {
                    "coverage": {"strategy_signal_evidence_count": 1},
                },
                "backfill_result": {
                    "native_signal_evidence": {
                        "initial_existing_signal_count": 1,
                        "saved_signal_count": 0,
                    },
                },
            }

    runner = IncubationFactoryRunner(dry_run=False)

    result = await runner._run_execution_audit_acceptance(
        Db(),
        strategies=[
            {"id": "signal-only"},
            {"id": "order-only"},
            {"id": "with-trade"},
        ],
    )

    assert calls == ["with-trade"]
    assert result["status"] == "pending_evidence"
    assert result["candidate_count"] == 3
    assert result["execution_evidence_candidate_count"] == 1
    assert result["awaiting_paper_execution_count"] == 2
    assert result["no_execution_evidence_count"] == 2
    assert result["selected_count"] == 1
    assert result["gate_status_counts"] == {"bootstrap_pending": 1}
    assert "execution_audit_gate_missing" not in result["blockers"]


@pytest.mark.asyncio
async def test_runner_uses_active_paper_observation_query_when_available(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT", "50")
    runner = IncubationFactoryRunner(dry_run=True)
    db = MagicMock()
    db.list_active_paper_observation_strategies = AsyncMock(
        return_value=[{"id": "paper-warmup-1", "status": "submitted", "observation_stage": "warmup"}]
    )
    db.list_paper_observation_strategies = AsyncMock(
        side_effect=AssertionError("legacy backlog query should not be used when active query exists")
    )

    result = await runner._list_paper_observation(db)

    assert result == [{"id": "paper-warmup-1", "status": "submitted", "observation_stage": "warmup"}]
    db.list_active_paper_observation_strategies.assert_called_once_with(limit=50)
    db.list_paper_observation_strategies.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_run_batch_with_strategies_skips_status_query(monkeypatch):
    service = StrategyIncubationPipelineService()
    db = MagicMock()
    db.save_strategy_task_run = AsyncMock(return_value={"id": 7, "trace_id": "trace-1"})
    db.update_strategy_task_run = AsyncMock()
    db.save_strategy_domain_event = AsyncMock()
    db.list_strategies = AsyncMock(side_effect=AssertionError("status query should not run"))

    async def fake_run_strategy(db_arg, strategy, **kwargs):
        return {
            "strategy_id": strategy["id"],
            "snapshot": {"pipeline_stage": strategy.get("_expected_stage", "observe")},
            "auto_promoted": False,
            "task_run_id": kwargs.get("task_run_id"),
        }

    monkeypatch.setattr(service, "run_strategy", fake_run_strategy)

    result = await service.run_batch(
        db,
        strategies=[
            {"id": "paper-1", "_expected_stage": "warmup"},
            {"id": "paper-1", "_expected_stage": "warmup"},
            {"id": "paper-2", "_expected_stage": "observe"},
        ],
        source="incubation_factory",
    )

    assert result["count"] == 2
    assert result["stage_counts"] == {"warmup": 1, "observe": 1}
    db.list_strategies.assert_not_called()
    payload = db.save_strategy_task_run.call_args[0][0]["payload"]
    assert payload["provided_strategies"] is True
    assert payload["strategy_count"] == 3
