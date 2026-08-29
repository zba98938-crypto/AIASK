"""DEV-V1 P1: paper observation intake 单元测试."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from akshare_mcp.config._strategy_factory_toggles import paper_intake_batch_limit
from akshare_mcp.services.incubation_factory.intake import IncubationIntake


@pytest.fixture
def intake():
    return IncubationIntake()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in (
        "INCUBATION_FACTORY_PAPER_INTAKE_ENABLED",
        "INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT",
        "INCUBATION_FACTORY_DIAGNOSTIC_INTAKE_ENABLED",
        "INCUBATION_FACTORY_DIAGNOSTIC_BATCH_LIMIT",
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED",
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT",
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.mark.asyncio
async def test_paper_intake_enabled_by_default(intake):
    """默认启用: 查询 paper 候选,让 observe 样本进入孵化消费。"""
    db = MagicMock()
    db.list_paper_observation_strategies = AsyncMock(return_value=[
        {"id": "s1", "strategy_type": "volatility_breakout"},
    ])
    result = await intake._list_paper_observation_strategies(db)
    assert result == [{"id": "s1", "strategy_type": "volatility_breakout"}]
    db.list_paper_observation_strategies.assert_called_once_with(limit=paper_intake_batch_limit())


@pytest.mark.asyncio
async def test_paper_intake_can_be_disabled_by_env(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "0")
    db = MagicMock()
    db.list_paper_observation_strategies = AsyncMock(return_value=[
        {"id": "s1", "strategy_type": "volatility_breakout"},
    ])
    result = await intake._list_paper_observation_strategies(db)
    assert result == []
    db.list_paper_observation_strategies.assert_not_called()


@pytest.mark.asyncio
async def test_paper_intake_enabled_returns_candidates(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    db = MagicMock()
    db.list_paper_observation_strategies = AsyncMock(return_value=[
        {"id": "s1", "strategy_type": "volatility_breakout"},
        {"id": "s2", "strategy_type": "value_factor"},
    ])
    result = await intake._list_paper_observation_strategies(db)
    assert len(result) == 2
    db.list_paper_observation_strategies.assert_called_once_with(limit=paper_intake_batch_limit())


@pytest.mark.asyncio
async def test_paper_intake_respects_batch_limit(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT", "10")
    db = MagicMock()
    db.list_paper_observation_strategies = AsyncMock(return_value=[])
    await intake._list_paper_observation_strategies(db)
    db.list_paper_observation_strategies.assert_called_once_with(limit=10)


@pytest.mark.asyncio
async def test_paper_intake_db_method_missing_returns_empty(intake, monkeypatch):
    """db 不实现 list_paper_observation_strategies 时降级返回空,不抛异常。"""
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    db = MagicMock(spec=[])  # 不实现任何 method
    result = await intake._list_paper_observation_strategies(db)
    assert result == []


@pytest.mark.asyncio
async def test_paper_intake_db_call_exception_returns_empty(intake, monkeypatch):
    """db 调用异常时降级返回空,Phase 2 不被打断。"""
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    db = MagicMock()
    db.list_paper_observation_strategies = AsyncMock(side_effect=RuntimeError("boom"))
    result = await intake._list_paper_observation_strategies(db)
    assert result == []


@pytest.mark.asyncio
async def test_record_paper_intake_event_writes_domain_event(intake):
    db = MagicMock()
    db.save_strategy_domain_event = AsyncMock()
    strategy = {
        "id": "s1",
        "name": "test_volatility",
        "strategy_type": "volatility_breakout",
    }
    await intake._record_paper_intake_event(db, strategy)
    db.save_strategy_domain_event.assert_called_once()
    payload = db.save_strategy_domain_event.call_args[0][0]
    assert payload["event_type"] == "incubation_factory.paper_observation_recognized"
    assert payload["aggregate_type"] == "incubation_factory"
    assert payload["source"] == "incubation_factory_intake_paper"
    assert payload["payload"]["stage"] == "paper"
    assert payload["payload"]["strategy_explanation"]["version"] == "strategy_explanation.v1"
    assert payload["payload"]["strategy_explanation_summary"]
    assert "strategy_explained" in payload["payload"]["strategy_explanation_labels"]


@pytest.mark.asyncio
async def test_record_acceptance_event_writes_strategy_explanation(intake):
    db = MagicMock()
    db.save_strategy_domain_event = AsyncMock()
    strategy = {
        "id": "s_accept",
        "name": "accepted_momentum",
        "description": "Accepted strategy with explanation.",
        "strategy_type": "momentum",
        "target_symbols": ["600000"],
        "params": {
            "generation_reason": {
                "source": "external_llm",
                "rationale": "Momentum and liquidity confirmation.",
            },
            "research_task": {"candidate_family": "momentum"},
        },
    }

    await intake._record_acceptance_event(db, strategy, {"account_id": "acct-1"})

    db.save_strategy_domain_event.assert_called_once()
    payload = db.save_strategy_domain_event.call_args[0][0]
    assert payload["event_type"] == "incubation_factory.strategy_accepted"
    assert payload["payload"]["strategy_explanation"]["version"] == "strategy_explanation.v1"
    assert payload["payload"]["strategy_explanation_summary"] == "Accepted strategy with explanation."
    assert "type:momentum" in payload["payload"]["strategy_explanation_labels"]


@pytest.mark.asyncio
async def test_record_paper_intake_event_swallows_db_exception(intake):
    """db.save_strategy_domain_event 抛异常时不再上抛,只 debug log。"""
    db = MagicMock()
    db.save_strategy_domain_event = AsyncMock(side_effect=RuntimeError("boom"))
    strategy = {"id": "s1", "name": "test", "strategy_type": "volatility_breakout"}
    # 不抛异常即为通过
    await intake._record_paper_intake_event(db, strategy)


@pytest.mark.asyncio
async def test_record_paper_intake_event_no_db_method(intake):
    """db 不实现 save_strategy_domain_event 时静默返回。"""
    db = MagicMock(spec=[])
    strategy = {"id": "s1"}
    await intake._record_paper_intake_event(db, strategy)


@pytest.mark.asyncio
async def test_diagnostic_intake_disabled_by_default(intake):
    db = MagicMock()
    db.list_diagnostic_observation_strategies = AsyncMock(return_value=[
        {"id": "s1", "strategy_type": "volatility_breakout"},
    ])
    result = await intake._list_diagnostic_observation_strategies(db)
    assert result == []
    db.list_diagnostic_observation_strategies.assert_not_called()


@pytest.mark.asyncio
async def test_diagnostic_intake_enabled_respects_batch_limit(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_DIAGNOSTIC_INTAKE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_DIAGNOSTIC_BATCH_LIMIT", "3")
    db = MagicMock()
    db.list_diagnostic_observation_strategies = AsyncMock(return_value=[
        {"id": "s1", "strategy_type": "volatility_breakout"},
    ])
    result = await intake._list_diagnostic_observation_strategies(db)
    assert len(result) == 1
    db.list_diagnostic_observation_strategies.assert_called_once_with(limit=3)


@pytest.mark.asyncio
async def test_diagnostic_intake_db_failure_returns_empty(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_DIAGNOSTIC_INTAKE_ENABLED", "1")
    db = MagicMock()
    db.list_diagnostic_observation_strategies = AsyncMock(side_effect=RuntimeError("boom"))
    result = await intake._list_diagnostic_observation_strategies(db)
    assert result == []


@pytest.mark.asyncio
async def test_record_diagnostic_intake_event_writes_domain_event(intake):
    db = MagicMock()
    db.save_strategy_domain_event = AsyncMock()
    strategy = {
        "id": "s_diag",
        "name": "diag_test",
        "strategy_type": "volatility_breakout",
    }
    await intake._record_diagnostic_intake_event(db, strategy)
    db.save_strategy_domain_event.assert_called_once()
    payload = db.save_strategy_domain_event.call_args[0][0]
    assert payload["event_type"] == "incubation_factory.diagnostic_observation_recognized"
    assert payload["source"] == "incubation_factory_intake_diagnostic"
    assert payload["payload"]["stage"] == "diagnostic"
    assert payload["payload"]["diagnostic_observation"] is True
    assert payload["payload"]["strategy_explanation"]["version"] == "strategy_explanation.v1"
    assert payload["payload"]["strategy_explanation_summary"]
    assert "strategy_explained" in payload["payload"]["strategy_explanation_labels"]


@pytest.mark.asyncio
async def test_gate3_record_only_intake_disabled_by_default(intake):
    db = MagicMock()
    db.list_factory_task_evidence = AsyncMock(return_value=[
        {"evidence_type": "gate3_record_only_audit"},
    ])

    result = await intake._list_gate3_record_only_candidates(db)

    assert result == []
    db.list_factory_task_evidence.assert_not_called()


@pytest.mark.asyncio
async def test_gate3_record_only_audit_filters_s_plus_records(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT", "10")
    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE", "S")
    db = MagicMock()
    db.list_factory_task_evidence = AsyncMock(return_value=[
        {
            "id": 1,
            "task_key": "candidate-s",
            "symbol": "600000",
            "evidence_type": "gate3_record_only_audit",
            "created_at": "2026-06-08 05:18:38",
            "evidence_payload": {
                "candidate_id": "candidate-s",
                "validation_grade": "S",
                "validation_total_score": 80.5,
                "strategy_created": False,
                "lifecycle_action_executed": False,
                "factory_run_id": "run-1",
            },
        },
        {
            "id": 2,
            "task_key": "candidate-b",
            "evidence_type": "gate3_record_only_audit",
            "evidence_payload": {
                "candidate_id": "candidate-b",
                "validation_grade": "B",
                "strategy_created": False,
                "lifecycle_action_executed": False,
            },
        },
        {
            "id": 3,
            "task_key": "candidate-created",
            "evidence_type": "gate3_record_only_audit",
            "evidence_payload": {
                "candidate_id": "candidate-created",
                "validation_grade": "SS",
                "strategy_created": True,
                "lifecycle_action_executed": False,
            },
        },
        {
            "id": 4,
            "task_key": "other",
            "evidence_type": "other_evidence",
            "evidence_payload": {"candidate_id": "other", "validation_grade": "SSS"},
        },
    ])

    result = await intake._list_gate3_record_only_candidates(db)

    assert [item["candidate_id"] for item in result] == ["candidate-s"]
    assert result[0]["validation_grade"] == "S"
    assert result[0]["validation_total_score"] == 80.5
    assert result[0]["factory_run_id"] == "run-1"
    db.list_factory_task_evidence.assert_called_once_with(
        evidence_type="gate3_record_only_audit",
        limit=10,
    )


@pytest.mark.asyncio
async def test_gate3_record_only_audit_recognizes_without_strategy_actions(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED", "1")
    db = MagicMock()
    db.list_factory_task_evidence = AsyncMock(return_value=[
        {
            "id": 7,
            "task_key": "candidate-ss",
            "symbol": "600000",
            "theme_code": "",
            "evidence_type": "gate3_record_only_audit",
            "created_at": "2026-06-08 05:18:58",
            "evidence_payload": {
                "candidate_id": "candidate-ss",
                "validation_grade": "SS",
                "validation_total_score": 85.8,
                "strategy_created": False,
                "lifecycle_action_executed": False,
                "factory_run_id": "run-2",
                "experiment_id": "exp-2",
                "quality_summary": {"validation_grade": "SS"},
                "backtest_metrics": {"total_return": 0.12},
            },
        }
    ])
    db.list_strategy_domain_events = AsyncMock(return_value=[])
    db.save_strategy_domain_event = AsyncMock()
    db.save_strategy_incubation_account = AsyncMock()
    db.save_strategy = AsyncMock()

    result = await intake._recognize_gate3_record_only_candidates(db)

    assert result["scanned"] == 1
    assert result["recognized"] == 1
    db.save_strategy_incubation_account.assert_not_called()
    db.save_strategy.assert_not_called()
    db.save_strategy_domain_event.assert_called_once()
    event = db.save_strategy_domain_event.call_args[0][0]
    assert event["strategy_id"] is None
    assert event["aggregate_id"] == "candidate-ss"
    assert event["event_type"] == "incubation_factory.gate3_record_only_candidate_recognized"
    assert event["source"] == "incubation_factory_intake_gate3_record_only"
    assert event["payload"]["stage"] == "record_only_intake"
    assert event["payload"]["record_only"] is True
    assert event["payload"]["action_boundary"] == "no_strategy_or_account_created"
    assert event["payload"]["validation_grade"] == "SS"
    assert event["payload"]["strategy_explanation"]["version"] == "strategy_explanation.v1"
    assert event["payload"]["strategy_explanation_summary"]
    assert "strategy_explained" in event["payload"]["strategy_explanation_labels"]
    db.list_strategy_domain_events.assert_called_once_with(
        aggregate_type="incubation_factory",
        aggregate_id="candidate-ss",
        event_type="incubation_factory.gate3_record_only_candidate_recognized",
        limit=1,
    )


@pytest.mark.asyncio
async def test_gate3_record_only_audit_skips_existing_event(intake, monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED", "1")
    db = MagicMock()
    db.list_factory_task_evidence = AsyncMock(return_value=[
        {
            "id": 8,
            "task_key": "candidate-s",
            "evidence_type": "gate3_record_only_audit",
            "evidence_payload": {
                "candidate_id": "candidate-s",
                "validation_grade": "S",
                "strategy_created": False,
                "lifecycle_action_executed": False,
            },
        }
    ])
    db.list_strategy_domain_events = AsyncMock(return_value=[
        {
            "aggregate_id": "candidate-s",
            "payload": {"candidate_id": "candidate-s"},
        }
    ])
    db.save_strategy_domain_event = AsyncMock()

    result = await intake._recognize_gate3_record_only_candidates(db)

    assert result["recognized"] == 0
    assert result["skipped_existing"] == 1
    db.save_strategy_domain_event.assert_not_called()
