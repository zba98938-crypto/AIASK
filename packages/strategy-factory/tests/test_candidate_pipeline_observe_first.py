from __future__ import annotations

import asyncio

from strategy_factory.application.services.candidate_pipeline import CandidatePipeline


class FakeBacktestFilter:
    def __init__(self) -> None:
        self._kline_cache = {}
        self._last_report = {
            "summary": {
                "input_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "failed_reason_counts": {},
            },
            "passed": [],
            "failed": [],
        }

    def get_last_report(self) -> dict:
        return self._last_report

    async def filter(self, candidates, db):
        passed = []
        failed = []
        for item in list(candidates or []):
            candidate = dict(item or {})
            if candidate.get("backtest_pass") is False:
                failed.append(
                    {
                        **candidate,
                        "backtest_outcome": {
                            "passed": False,
                            "reason_code": "adaptive_gate_2_failed",
                        },
                    }
                )
                continue
            passed.append(
                {
                    **candidate,
                    "backtest_metrics": {
                        "trade_count": 8,
                        "trades_count": 8,
                        "sharpe_ratio": 0.9,
                        "max_drawdown": 0.08,
                        "parameter_perturbation_trade_stability": 0.82,
                    },
                    "backtest_outcome": {"passed": True, "reason_code": "passed"},
                }
            )
        self._last_report = {
            "summary": {
                "input_count": len(list(candidates or [])),
                "passed_count": len(passed),
                "failed_count": len(failed),
                "failed_reason_counts": {"adaptive_gate_2_failed": len(failed)} if failed else {},
            },
            "passed": passed,
            "failed": failed,
        }
        return passed


class FakeDeduplicator:
    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def deduplicate(self, candidates, db):
        self.seen = [dict(item or {}) for item in list(candidates or [])]
        return self.seen

    def get_last_report(self) -> dict:
        return {"input_count": len(self.seen), "kept_count": len(self.seen)}


class FakeSubmitter:
    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def submit(self, candidates, snapshot, db, *, read_only=False):
        self.seen = [dict(item or {}) for item in list(candidates or [])]
        strategies = [
            {
                "strategy_id": f"strategy_{idx}",
                "status": "submitted",
                "submission_lane": "observe_incubation",
            }
            for idx, _candidate in enumerate(self.seen)
        ]
        return {
            "submitted": 0,
            "created_total": len(self.seen),
            "created_strategy_pool": len(self.seen),
            "gate_3_input": len(self.seen),
            "gate_3_passed": 0,
            "gate_3_failed": len(self.seen),
            "strategies": strategies,
        }


class FakeScheduler:
    def __init__(self) -> None:
        self.deduplicator = FakeDeduplicator()
        self.submitter = FakeSubmitter()

    def _now(self):
        return datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)

    def _with_stage_meta(self, stage, trace_id, payload, **kwargs):
        data = dict(payload or {})
        data.update({"stage": stage, "trace_id": trace_id, **kwargs})
        return data

    def _build_layered_run_summary(self, summary, _submit=None):
        return dict(summary or {})

    async def _run_startup_warmup(self):
        return {"status": "skipped", "ok": True, "failed": 0}

    def _apply_run_audit(self, results, persistence_failures=None):
        return results

    def _build_deduplicator(self, pkg):
        return self.deduplicator

    def _build_submitter(self, pkg):
        return self.submitter


class FakePkg:
    BacktestFilter = FakeBacktestFilter

    def __init__(self) -> None:
        self.gated_filter_calls = 0
        self.gated_candidates: list[dict] = []

    async def run_gated_filter(self, candidates, db, backtest_filter, *, kline_cache=None):
        self.gated_filter_calls += 1
        self.gated_candidates = [dict(item or {}) for item in list(candidates or [])]
        return {
            "passed": [dict(candidates[0])],
            "gate_report": {
                "gate_1": {"passed_count": 1, "failed_count": 1},
                "gate_2": {
                    "passed_count": 1,
                    "failed_count": 1,
                    "report": backtest_filter.get_last_report(),
                },
            },
        }

    def finalize_gate_report(self, quality_gate_report, submit_result):
        report = dict(quality_gate_report or {})
        report["gate_3"] = {
            "input_count": submit_result.get("gate_3_input", 0),
            "passed_count": submit_result.get("gate_3_passed", 0),
            "failed_count": submit_result.get("gate_3_failed", 0),
        }
        return report


class FakeDb:
    async def get_klines(self, *args, **kwargs):
        return []


def test_observe_first_pipeline_screens_evidence_before_gate3_record(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED", "1")
    scheduler = FakeScheduler()
    candidates = [
        {"candidate_id": "c1", "params": {"family": "trend"}},
        {"candidate_id": "c2", "params": {"family": "quality"}, "backtest_pass": False},
    ]

    pkg = FakePkg()
    result = asyncio.run(
        CandidatePipeline(pkg, scheduler).run(
            candidates,
            {"date": "2026-06-04"},
            FakeDb(),
        )
    )

    observe_first = result.quality_gate_report["observe_first"]
    assert observe_first["enabled"] is True
    assert observe_first["mode"] == "backtest_evidence_screen"
    assert observe_first["pre_observe_gate_removed"] is True
    assert observe_first["legacy_gate_executed"] is False
    assert observe_first["legacy_funnel_executed"] is False
    assert observe_first["evidence_scoring_mode"] == "observe_first_backtest_evidence_screen"
    assert observe_first["gate_passed_count"] == 1
    assert observe_first["observe_intake_count"] == 1
    assert observe_first["deduped_observe_intake_count"] == 1
    assert observe_first["pre_observe_hard_reject_count"] == 0
    assert observe_first["pre_observe_evidence_reject_count"] == 1
    assert observe_first["gate3_pre_observe_block_count"] == 1
    assert result.quality_gate_report["legacy_gate_executed"] is False
    assert result.quality_gate_report["legacy_funnel_executed"] is False
    assert result.quality_gate_report["evidence_scoring_mode"] == "observe_first_backtest_evidence_screen"
    assert result.backtest_report["summary"]["passed_count"] == 1
    assert result.backtest_report["summary"]["failed_count"] == 1
    assert result.gate_artifact["legacy_gate_executed"] is False
    assert result.gate_artifact["legacy_funnel_executed"] is False
    assert result.gate_artifact["evidence_scoring_mode"] == "observe_first_backtest_evidence_screen"
    assert pkg.gated_filter_calls == 0
    assert all(candidate["observe_first_intake"] for candidate in scheduler.submitter.seen)
    assert {
        candidate["incubation_budget"]["track"] for candidate in scheduler.submitter.seen
    } == {"observe_incubation"}
    assert [candidate["candidate_id"] for candidate in scheduler.submitter.seen] == ["c1"]


def test_stock_first_execution_mode_enables_observe_first_without_env(monkeypatch) -> None:
    monkeypatch.delenv("STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED", raising=False)
    scheduler = FakeScheduler()
    candidates = [
        {"candidate_id": "c1", "params": {"family": "trend"}},
        {"candidate_id": "c2", "params": {"family": "quality"}},
    ]

    pkg = FakePkg()
    result = asyncio.run(
        CandidatePipeline(pkg, scheduler).run(
            candidates,
            {"date": "2026-06-04"},
            FakeDb(),
            execution_mode="stock_first_observe_primary",
        )
    )

    assert result.quality_gate_report["observe_first"]["enabled"] is True
    assert result.quality_gate_report["observe_first"]["legacy_gate_executed"] is False
    assert result.quality_gate_report["observe_first"]["execution_mode"] == "stock_first_observe_primary"
    assert pkg.gated_filter_calls == 0
    assert len(scheduler.submitter.seen) == 2


def test_candidate_pipeline_attaches_snapshot_prediction_context_before_gate(monkeypatch) -> None:
    from strategy_factory.application.candidate_contract import apply_resolved_candidate_envelope
    from strategy_factory.application.trade_prediction_contract import TRADE_PREDICTION_CONTRACT_READY

    monkeypatch.setenv("STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED", "0")
    scheduler = FakeScheduler()
    candidates = [
        {
            "id": "runtime-candidate",
            "strategy_type": "momentum",
            "target_symbols": ["600000"],
            "params": {
                "prediction_contract": {
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "direction": "bullish",
                            "confidence": 0.68,
                            "horizon": "next_day",
                            "evidence_ids": ["ev-1"],
                        }
                    ]
                },
                "evidence_chain": {
                    "evidences": [{"evidence_id": "ev-1", "source": "market_data_runtime"}]
                },
            },
        }
    ]

    pkg = FakePkg()
    asyncio.run(
        CandidatePipeline(pkg, scheduler).run(
            candidates,
            {"date": "2026-06-05"},
            FakeDb(),
            execution_mode="legacy_primary",
        )
    )

    gated_candidate = pkg.gated_candidates[0]
    assert gated_candidate["prediction_as_of"] == "2026-06-05"
    assert gated_candidate["params"]["prediction_as_of"] == "2026-06-05"

    resolved = apply_resolved_candidate_envelope(gated_candidate)
    assert resolved["trade_prediction_contract_status"] == TRADE_PREDICTION_CONTRACT_READY
    assert resolved["trade_prediction_contract_hash"]
    assert resolved["trade_prediction_contract"]["target_trading_date"] == "2026-06-08"
