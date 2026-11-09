"""Index context must never become a tradable target or portfolio sleeve."""
from __future__ import annotations

from copy import deepcopy

import pytest

from strategy_factory.application.backtest_filter import BacktestFilter
from strategy_factory.application.factory_market_views import build_full_market_topn_payload, select_full_market_topn_constituents
from strategy_factory.application.research.runner import ResearchPlaneRunner
from strategy_factory.application.tradeability_gate import build_a_share_tradeability_profile, validate_tradeability_profile
from strategy_factory.domain.spawner import StrategySpawner
from strategy_factory.domain.targets import _is_index_reference_code, _non_tradable_index_target_codes, _normalize_target_codes


@pytest.mark.parametrize("code", ["sh000001", "sz399001", "SH000300", "000001.SH", "399006.SZ", "bj899050", "399001"])
def test_explicit_index_namespaces_are_rejected(code):
    assert _is_index_reference_code(code)
    assert not StrategySpawner._is_tradable_target_code(code)
    assert _non_tradable_index_target_codes({"target_symbols": [code]})


@pytest.mark.parametrize("code", ["000001", "600519", "510300", "sh510300", "159915.SZ", "sz159915", "600519.SH"])
def test_stock_and_etf_codes_are_not_blanket_excluded(code):
    assert not _is_index_reference_code(code)
    assert StrategySpawner._is_tradable_target_code(code)


def test_index_context_is_preserved_without_becoming_a_target():
    candidate = {"target_symbols": ["000001"], "event_context": {"benchmark_symbols": ["sh000001"]}}
    assert _non_tradable_index_target_codes(candidate) == []
    assert _normalize_target_codes(["000001", "000001.SH"]) == ["000001", "sh000001"]
    assert candidate["event_context"]["benchmark_symbols"] == ["sh000001"]


def test_topn_keeps_raw_scoring_evidence_and_reports_target_exclusion():
    rows = [
        {"code": "sh000001", "composite_score": 100.0},
        {"code": "sz399001", "composite_score": 90.0},
        {"code": "sh510300", "composite_score": 80.0},
        {"code": "000001", "composite_score": 70.0},
    ]
    before = deepcopy(rows)
    selected = select_full_market_topn_constituents(rows, max_per_industry=10)
    assert [r["code"] for r in selected] == ["sh510300", "000001"]
    payload = build_full_market_topn_payload(as_of_date="2026-11-06", universe_count=4, eligible_count=4, score_rows=rows, max_per_industry=10)
    assert payload["metadata"]["excluded_target_codes"] == ["sh000001", "sz399001"]
    assert payload["metadata"]["target_exclusion_reason"] == "non_tradable_index_target"
    assert rows == before


def test_injection_filters_historical_topn_and_reports_all_index_empty_pool():
    payload = {"constituents": [{"code": "sh000001"}, {"code": "sz399001"}]}
    candidate = {"strategy_type": "momentum", "params": {}}
    result, audit = ResearchPlaneRunner._inject_topn_targets_into_local_candidates([candidate], payload, {})
    assert result == [candidate]
    assert audit["injected_candidate_count"] == 0
    assert audit["excluded_target_codes"] == ["sh000001", "sz399001"]
    assert audit["target_exclusion_reason"] == "non_tradable_index_target"
    assert ResearchPlaneRunner._topn_target_codes({"constituents": [{"code": "sh000001"}, {"code": "000001"}, {"code": "sh510300"}]}) == ["000001", "sh510300"]


@pytest.mark.asyncio
async def test_explicit_index_target_rejected_before_data_or_engine_access():
    class MustNotRead:
        async def get_klines(self, *args, **kwargs):
            raise AssertionError("invalid index target reached market data")

    class MustNotRun:
        def run_backtest(self, *args, **kwargs):
            raise AssertionError("invalid index target reached engine")

    candidate = {"strategy_type": "volatility_breakout", "params": {}, "target_symbols": ["002594", "sh000001", "sz399001"]}
    bf = BacktestFilter()
    result = await bf._test_one(candidate, MustNotRead(), MustNotRun())
    assert result["reason_code"] == "non_tradable_index_target"
    assert result["target_codes"] == candidate["target_symbols"]
    assert result["successful_code_count"] == 0
    assert result["metrics"] == {}
    passed, failed = [], []
    bf._apply_result_to_candidate(candidate, result, passed, failed)
    assert not passed and failed == [candidate]
    assert candidate["backtest_metrics"]["post_cost_sharpe"] is None
    assert candidate["backtest_metrics"]["trade_count"] is None
    assert candidate["backtest_metrics_contract"]["available"] is False


def test_index_target_blocks_certificate_profile():
    result = validate_tradeability_profile(build_a_share_tradeability_profile({"target_symbols": ["sh000001"]}))
    assert not result["valid"]
    assert "non_tradable_index_target" in result["missing_fields"]


@pytest.mark.parametrize("track", ["observe_incubation", "deferred_budget_queue", "formal_incubation"])
def test_real_runtime_bootstrap_blocks_index_even_when_quality_passes(track):
    from _admission_helpers import _runtime_ready_candidate
    from strategy_factory.application.submitter import StrategySubmitter
    from strategy_factory.application.services.admission_authority import SubmissionAdmissionAuthority

    candidate = _runtime_ready_candidate(observe_first=True)
    gate = {"passed": True, "strict_incubation_ready": True, "validation_grade": "B"}
    baseline = StrategySubmitter._runtime_bootstrap_context(gate, candidate=candidate)
    assert baseline["pre_observe_hard_reject_reasons"] == []
    candidate["target_symbols"] = ["002594", "sh000001"]
    result = SubmissionAdmissionAuthority.resolve(
        gate, candidate=candidate,
        refresh_existing=False, existing_status="draft", incubation_budget_track=track,
        runtime_bootstrap_resolver=StrategySubmitter._runtime_bootstrap_context,
    )
    assert result["submission_lane"] == "rejected"
    assert "non_tradable_index_target" in result["submission_action_gaps"]
