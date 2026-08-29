"""Smoke tests for BacktestFilter import and basic structure."""

from __future__ import annotations

import asyncio
import json

import pytest


def test_backtest_filter_import():
    from strategy_factory.application.backtest_filter import BacktestFilter

    bf = BacktestFilter()
    assert bf is not None
    assert hasattr(bf, "filter")


def test_backtest_filter_thresholds_loaded():
    from strategy_factory.domain.constants import (
        BACKTEST_DEFAULT_THRESHOLDS,
        BACKTEST_AI_PROTOTYPE_THRESHOLDS,
        BACKTEST_TYPE_THRESHOLDS,
    )

    assert BACKTEST_DEFAULT_THRESHOLDS["sharpe_min"] > 0
    assert BACKTEST_AI_PROTOTYPE_THRESHOLDS["sharpe_min"] > 0
    assert "momentum" in BACKTEST_TYPE_THRESHOLDS
    assert "ma_cross" in BACKTEST_TYPE_THRESHOLDS


def test_backtest_trade_profile_from_round_trips():
    from strategy_factory.application.backtest_filter import BacktestFilter

    payload = BacktestFilter._merge_trade_profile_metrics(
        {
            "initial_capital": 100000,
            "round_trip_positions": [
                {"status": "closed", "realized_pnl": 100.0, "realized_return": 0.01, "hold_days": 5},
                {"status": "closed", "realized_pnl": -50.0, "realized_return": -0.005, "hold_days": 3},
            ],
        }
    )

    assert payload["trade_profile_available"] is True
    assert payload["expectancy"] == 25.0
    assert payload["profit_factor"] == 2.0
    assert payload["payoff_ratio"] == 2.0
    assert payload["breakeven_win_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert payload["trade_distribution"]["sample_count"] == 2


def test_backtest_trade_profile_from_trades_fallback():
    from strategy_factory.application.backtest_filter import BacktestFilter

    payload = BacktestFilter._merge_trade_profile_metrics(
        {
            "initial_capital": 100000,
            "trades": [
                {"signal": 1, "price": 10.0, "shares": 100, "profit": 0.0},
                {"signal": -1, "price": 11.0, "shares": 100, "profit": 100.0, "holding_days": 4},
                {"signal": -1, "price": 9.5, "shares": 100, "profit": -50.0, "holding_days": 2},
            ],
        }
    )

    assert payload["trade_profile_available"] is True
    assert payload["trade_profile_source"] == "trades"
    assert payload["expectancy"] == 25.0
    assert payload["profit_factor"] == 2.0
    assert payload["trade_distribution"]["win_count"] == 1
    assert payload["trade_distribution"]["loss_count"] == 1


def test_backtest_filter_digests_large_result_without_candidate_raw_payload():
    from strategy_factory.application.backtest_filter import BacktestFilter

    bf = BacktestFilter()
    candidate = {
        "strategy_type": "momentum",
        "generator_type": "rule",
        "params": {"lookback": 20},
        "target_symbols": ["600519"],
        "research_task": {"task_id": "task-1", "target_symbols": ["600519"]},
    }
    result = {
        "passed": True,
        "reason_code": "passed",
        "strategy_type": "momentum",
        "sample_count": 1,
        "required_sample_count": 1,
        "evaluated_code_count": 1,
        "successful_code_count": 1,
        "metrics": {
            "sharpe_ratio": 1.2,
            "total_return": 0.18,
            "max_drawdown": 0.08,
            "trades_count": 32,
        },
        "layers": {
            "target": {"metrics": {"sharpe_ratio": 1.2, "total_return": 0.18}},
            "combined": {"metrics": {"sharpe_ratio": 1.1, "total_return": 0.16}},
        },
        "event_window_metrics": {
            "event_sample_count": 100000,
            "event_time_anchors": list(range(100000)),
            "raw_events": [{"i": i} for i in range(2000)],
        },
        "equity_curve": list(range(100000)),
        "cash_curve": list(range(100000)),
        "trades": [{"i": i, "price": i * 0.1} for i in range(5000)],
        "fills": [{"i": i} for i in range(5000)],
    }
    passed: list[dict] = []
    failed: list[dict] = []

    bf._apply_result_to_candidate(candidate, result, passed, failed)
    report = bf._build_last_report([candidate], passed, failed)

    assert "backtest_result" not in candidate
    assert candidate["backtest_outcome"]["dropped_heavy_fields"] == [
        "cash_curve",
        "equity_curve",
        "fills",
        "trades",
    ]
    assert "trades" not in candidate["backtest_outcome"]
    assert "fills" not in candidate["backtest_outcome"]
    assert len(candidate["backtest_metrics"]["event_window_metrics"]["event_time_anchors"]) <= 8
    assert "parameter_perturbation_trade_stability" in candidate["backtest_metrics_contract"]
    assert "parameter_perturbation_trade_stability" in candidate["backtest_metrics"]
    assert "passed" not in report
    assert "failed" not in report
    assert report["diagnostics"]["passed_preview_count"] == 1
    assert len(json.dumps(report, ensure_ascii=False, default=str).encode("utf-8")) < 64 * 1024


def test_run_gated_filter_returns_briefs_not_candidate_sets(monkeypatch):
    from strategy_factory.application import quality_gates as qg

    async def fake_gate_1(candidate, db, *, kline_cache=None):
        return qg.GateResult(
            passed=True,
            gate="gate_1",
            metrics={"avg_sharpe": 1.0, "gate_2_priority_score": 10.0},
        )

    class FakeBacktestFilter:
        async def filter(self, candidates, db):
            return list(candidates)

        def get_last_report(self):
            return {
                "summary": {"input_count": 20, "passed_count": 20, "failed_count": 0},
                "passed": [
                    {
                        "strategy_type": "momentum",
                        "backtest_result": {
                            "metrics": {"sharpe_ratio": 1.1},
                            "equity_curve": list(range(100000)),
                            "trades": [{"i": i} for i in range(2000)],
                        },
                    }
                    for _ in range(20)
                ],
                "failed": [],
            }

    monkeypatch.setattr(qg, "FACTORY_PRE_GATE_ENABLED", False)
    monkeypatch.setattr(
        qg,
        "gate_0_structural",
        lambda candidate: qg.GateResult(passed=True, gate="gate_0"),
    )
    monkeypatch.setattr(qg, "_compat_gate_1_fast_screen", fake_gate_1)
    candidates = [
        {
            "strategy_type": "momentum",
            "params": {"lookback": 20},
            "target_symbols": ["600519"],
            "research_task": {"task_id": f"task-{i}", "target_symbols": ["600519"]},
            "trades": [{"i": j} for j in range(1000)],
        }
        for i in range(20)
    ]

    result = asyncio.run(qg.run_gated_filter(candidates, object(), FakeBacktestFilter()))
    gate_report = result["gate_report"]
    gate_2 = gate_report["gate_2"]

    assert len(gate_2["passed_candidates"]) <= 12
    assert gate_2["passed_candidates_is_brief"] is True
    assert gate_2["passed_candidates_count"] == len(result["passed"])
    assert "passed" not in gate_2["report"]
    assert "failed" not in gate_2["report"]
    assert len(json.dumps(gate_report, ensure_ascii=False, default=str).encode("utf-8")) < 64 * 1024


def test_build_run_artifacts_omits_quality_gate_and_backtest_payload_artifacts():
    from strategy_factory.application.factory_execution import build_run_artifacts

    artifacts = build_run_artifacts(
        {
            "research_plane": {
                "contract_version": "research.v1",
                "task_artifact": {"available": True},
            },
            "governance_plane": {
                "contract_version": "governance.v1",
                "submission_artifact": {"available": True},
            },
            "quality_gate": {"gate_2": {"passed_candidates": [{"trades": list(range(100000))}]}},
            "backtest_report": {"passed": [{"equity_curve": list(range(100000))}]},
        }
    )
    artifact_types = {item["artifact_type"] for item in artifacts}

    assert "quality_gate" not in artifact_types
    assert "backtest_report" not in artifact_types
    for artifact in artifacts:
        assert len(json.dumps(artifact["payload_json"], ensure_ascii=False, default=str).encode("utf-8")) < 64 * 1024
