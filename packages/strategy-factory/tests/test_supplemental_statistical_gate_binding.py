"""Behaviour tests for the Gate-3 supplemental statistical verdict.

``_merge_trade_primary_gate`` used to fold every supplemental reason into a
single ``supplemental_statistical_gate_failed`` *warning* and take ``passed``
from the trade gate alone. Walk-forward IC/IR, purged-k-fold IC, bootstrap CI
lower bound and parameter sensitivity were computed and then discarded, so on
the trade-primary profiles they were advisory everywhere — including on tracks
that lead to capital.

The ruling: binding on capital-bound tracks (formal_incubation,
live_ready_review), advisory on research lanes.
"""

from __future__ import annotations

import pytest

from strategy_factory.application.submission_gate.runner import (
    _merge_trade_primary_gate,
    _supplemental_statistical_binding_mode,
)


def _trade_gate(passed: bool = True, **extra) -> dict:
    return {
        "passed": passed,
        "gate_protocol": "trade_rule_validation:trade_primary",
        "reasons": [],
        "warnings": [],
        **extra,
    }


def _supplemental(passed: bool, reasons: list[str] | None = None, **extra) -> dict:
    return {
        "passed": passed,
        "reasons": list(reasons or []),
        "warnings": [],
        **extra,
    }


class TestBindingModeResolution:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"submission_lane": "live_ready_review"},
            {"incubation_budget_track": "formal_incubation"},
            {"incubation_budget_track": "live_ready_review"},
            {"submission_lane": "LIVE_READY_REVIEW"},
        ],
    )
    def test_capital_bound_tracks_are_binding(self, kwargs: dict) -> None:
        assert _supplemental_statistical_binding_mode(**kwargs) == "binding"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"submission_lane": "research"},
            {"incubation_budget_track": "research_only"},
            {"submission_lane": None, "incubation_budget_track": None},
        ],
    )
    def test_research_lanes_stay_advisory(self, kwargs: dict) -> None:
        assert _supplemental_statistical_binding_mode(**kwargs) == "advisory"


class TestBindingBlocks:
    def test_missing_statistical_evidence_blocks_on_a_binding_track(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(
                False,
                [
                    "insufficient_statistical_evidence",
                    "missing_wf_ic_ir",
                    "missing_bootstrap_ci_lower",
                ],
            ),
            binding_mode="binding",
        )
        assert merged["passed"] is False
        assert "supplemental_statistical_gate_failed" in merged["reasons"]
        # The specific reasons survive into the merged verdict, not just a label.
        assert "missing_wf_ic_ir" in merged["reasons"]
        assert "missing_bootstrap_ci_lower" in merged["reasons"]
        assert merged["supplemental_statistical_binding_mode"] == "binding"

    def test_weak_statistical_evidence_blocks_on_a_binding_track(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(False, ["weak_pkf_ic", "weak_bootstrap_ci_lower"]),
            binding_mode="binding",
        )
        assert merged["passed"] is False
        assert "weak_pkf_ic" in merged["reasons"]

    def test_supplemental_gate_error_blocks_rather_than_warning(self) -> None:
        # The call site builds passed=False on exception; that must not be
        # laundered into a pass on a capital-bound track.
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(False, ["Supplemental statistical gate error: boom"]),
            binding_mode="binding",
        )
        assert merged["passed"] is False

    def test_protocol_names_the_binding_decision(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(False, ["weak_pkf_ic"]),
            binding_mode="binding",
        )
        assert merged["gate_protocol"] == (
            "trade_rule_validation:trade_primary_with_binding_supplemental_audit"
        )
        assert merged["primary_gate_protocol"] == "trade_rule_validation:trade_primary"


class TestAdvisoryPreservesResearchFlow:
    def test_statistical_failure_stays_a_warning_on_a_research_lane(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(False, ["weak_pkf_ic", "missing_wf_ic_ir"]),
            binding_mode="advisory",
        )
        assert merged["passed"] is True
        assert "supplemental_statistical_gate_failed" in merged["warnings"]
        assert merged["reasons"] == []
        assert merged["supplemental_statistical_binding_mode"] == "advisory"

    def test_advisory_is_the_default(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(False, ["weak_pkf_ic"]),
        )
        assert merged["passed"] is True

    def test_the_supplemental_detail_is_recorded_either_way(self) -> None:
        for mode in ("binding", "advisory"):
            merged = _merge_trade_primary_gate(
                _trade_gate(passed=True),
                _supplemental(False, ["weak_pkf_ic"]),
                binding_mode=mode,
            )
            detail = merged["supplemental_statistical_gate"]
            assert detail["passed"] is False
            assert detail["reasons"] == ["weak_pkf_ic"]


class TestUnaffectedPaths:
    def test_a_clean_supplemental_gate_passes_in_both_modes(self) -> None:
        for mode in ("binding", "advisory"):
            merged = _merge_trade_primary_gate(
                _trade_gate(passed=True), _supplemental(True), binding_mode=mode
            )
            assert merged["passed"] is True, mode
            assert merged["reasons"] == []

    def test_a_failing_trade_gate_still_fails_when_statistics_are_clean(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=False, reasons=["post_cost_sharpe 0.10 < 0.50"]),
            _supplemental(True),
            binding_mode="binding",
        )
        assert merged["passed"] is False
        assert "post_cost_sharpe 0.10 < 0.50" in merged["reasons"]

    def test_supplemental_metric_fields_are_carried_through(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(True, wf_ic_ir=0.42, pkf_ic=0.031, bootstrap_ci_lower=0.004),
            binding_mode="binding",
        )
        assert merged["wf_ic_ir"] == 0.42
        assert merged["pkf_ic"] == 0.031
        assert merged["bootstrap_ci_lower"] == 0.004

    def test_reason_codes_are_regenerated_for_promoted_reasons(self) -> None:
        merged = _merge_trade_primary_gate(
            _trade_gate(passed=True),
            _supplemental(False, ["weak_pkf_ic"]),
            binding_mode="binding",
        )
        assert merged["reason_codes"]
        assert len(merged["reason_codes"]) == len(merged["reasons"])
