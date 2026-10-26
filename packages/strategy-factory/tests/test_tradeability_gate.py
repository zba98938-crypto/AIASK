from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from strategy_factory.application.candidate_contract import (
    build_execution_contract_hash,
    build_tested_object_hash,
)
from strategy_factory.application.tradeability_gate import (
    TRADEABILITY_GATE_VERSION,
    build_tradeability_certificate,
    validate_tradeability_certificate_freshness,
    validate_tradeability_profile,
)


def _profile() -> dict:
    return {
        "profile_version": "strategy_factory.a_share_daily_tradeability.v1",
        "market": "A_share",
        "bar_frequency": "daily",
        "signal_timing": "after_close",
        "execution": {
            "timing": "next_tradable_session",
            "price_policy": "next_session_open",
            "order_policy": "limit_order_with_explicit_cancel_rule",
            "missing_bar_policy": "reject_and_record",
            "partial_fill_policy": "carry_remainder_to_next_tradable_session",
        },
        "universe": {
            "symbols": ["600000"],
            "membership_policy": "point_in_time_index_membership",
            "point_in_time_source": "fixture://universe-v1",
            "delisted_security_coverage": "retain_delisted_constituents",
        },
        "holding_exit": {
            "holding_rule": {"max_sessions": 10},
            "entry_rule": "signal_confirmed_after_close",
            "exit_rule": "fixed_exit_and_risk_stop",
            "corporate_action_policy": "adjust_prices_and_quantities_from_source_events",
        },
        "position_sizing": {"method": "fixed_fraction", "fraction": 0.1},
        "risk_limits": {"max_drawdown": 0.2, "max_position_pct": 0.3},
        "cost_model": {
            "version": "fixture-cost-v1",
            "commission_rate": 0.0003,
            "sell_tax_rate": 0.0005,
            "slippage_model": "measured_spread_and_participation",
            "slippage_bps": 5,
            "market_impact_model": "square_root_participation",
            "market_impact_bps": 4,
            "evidence_refs": ["fixture://costs"],
        },
        "liquidity_capacity": {
            "adv_source": "fixture://daily-volume",
            "adv_window_days": 20,
            "max_adv_participation": 0.01,
            "capacity_notional": 1000000,
            "evidence_refs": ["fixture://capacity"],
        },
        "validation": {
            "train_window": "expanding_120_sessions",
            "purge_days": 5,
            "embargo_days": 5,
            "oos_fold_count": 3,
            "holdout_start": "2026-10-01",
            "holdout_end": "2026-12-31",
            "holdout_sealed": True,
            "holdout_excluded_from_selection": True,
        },
    }


def _candidate() -> dict:
    candidate = {
        "candidate_id": "candidate-fixture-1",
        "strategy_id": "strategy-fixture-1",
        "strategy_type": "ma_cross",
        "params": {"short_period": 5, "long_period": 20},
        "tradeability_profile": _profile(),
    }
    candidate["tested_object_hash"] = build_tested_object_hash(candidate)
    candidate["execution_contract_hash"] = build_execution_contract_hash(candidate)
    return candidate


def _backtest_evidence(*, negative: bool = False) -> dict:
    daily = [(-0.001 - (index % 3) * 0.0001) if negative else (0.002 + (index % 3) * 0.0001) for index in range(240)]
    folds = []
    offset = 100
    for fold_index in range(3):
        start = offset + fold_index * 100
        end = start + 80
        fold_returns = daily[fold_index * 80 : (fold_index + 1) * 80]
        trade_ids = [f"round-trip-{fold_index}-{i}" for i in range(10)]
        trading_dates = []
        day = date(2025, 1, 1) + timedelta(days=fold_index * 120)
        while len(trading_dates) < 80:
            if day.weekday() < 5:
                trading_dates.append(day.isoformat())
            day += timedelta(days=1)
        folds.append(
            {
                "fold_id": f"fold-{fold_index + 1}",
                "train_end_index": start - 20,
                "test_start_index": start,
                "test_end_index": end,
                "trading_dates": trading_dates,
                "daily_net_returns": fold_returns,
                "round_trip_net_returns": [0.02] * 9 + [-0.005],
                "independent_trade_ids": trade_ids,
                "derived": False,
            }
        )
    family = np.column_stack(
        [
            np.asarray(daily),
            np.full(240, -0.002),
            np.full(240, -0.003),
            np.full(240, -0.004),
        ]
    ).tolist()
    return {
        "evidence_ref": "fixture://real-oos-backtest-evidence",
        "purge_days": 5,
        "embargo_days": 5,
        "bootstrap_block_length": 5,
        "oos_folds": folds,
        "final_holdout": {
            "sealed": True,
            "excluded_from_selection": True,
            "opened_for_candidate_selection": False,
            "start": "2026-10-01",
            "end": "2026-12-31",
            "start_index": 450,
            "end_index": 515,
        },
        "execution": {
            "matches_profile": True,
            "data_coverage_complete": True,
            "unmeasurable_fields": [],
            "order_fill_events": [
                {
                    "signal_date": "2025-01-02",
                    "execution_date": "2025-01-03",
                    "status": "filled",
                    "price_source": "next_session_open",
                    "fill_price": 10.1,
                    "fee_breakdown": {"commission": 1.0, "tax": 0.5, "slippage": 0.5},
                    "derived": False,
                }
            ],
            "derived": False,
        },
        "cost_scenarios": {
            "derived": False,
            "base": {"total_cost": 100.0, "evidence_ref": "fixture://base-cost"},
            "conservative": {
                "cost_multiple": 2.0,
                "total_cost": 200.0,
                "daily_net_returns": [0.001] * 240,
            },
        },
        "multiple_testing": {
            "n_trials": 4,
            "family_net_returns": family,
            "derived": False,
        },
    }


def _paper_evidence() -> dict:
    start = date(2026, 1, 1)
    nav = 100_000.0
    observations = []
    for index in range(60):
        nav *= 1.0015 + (0.0001 if index % 2 else -0.0001)
        observations.append({"date": (start + timedelta(days=index)).isoformat(), "nav": nav, "derived": False})
    trips = [
        {
            "position_id": f"paper-position-{index}",
            "net_pnl": 150.0 if index < 26 else -50.0,
            "net_return": 0.015 if index < 26 else -0.005,
            "status": "closed",
            "derived": False,
        }
        for index in range(30)
    ]
    return {
        "trial_started": True,
        "evidence_track": "real",
        "candidate_hash_bound": True,
        "evidence_ref": "fixture://paper-trial-v1",
        "evidence_refs": ["paper_fill:fill-1", "paper_nav:nav-1"],
        "nav_observations": observations,
        "closed_round_trips": trips,
    }


def test_tradeability_profile_is_complete_only_with_real_a_share_operating_contract():
    result = validate_tradeability_profile(_profile())

    assert result["valid"] is True
    assert result["missing_fields"] == []
    assert len(result["profile_hash"]) == 64

    incomplete = _profile()
    incomplete["cost_model"]["evidence_refs"] = []
    assert "cost_evidence_missing" in validate_tradeability_profile(incomplete)["missing_fields"]


def test_candidate_cannot_gain_certification_from_legacy_pass_or_claimed_status():
    candidate = _candidate()
    candidate["certification_status"] = "paper_validated"
    candidate["legacy_backtest_pass"] = True

    result = build_tradeability_certificate(candidate, data_snapshot={"as_of": "2026-10-20"})

    assert result["status"] == "research_only"
    assert result["reason_codes"]
    assert result["gate_version"] == TRADEABILITY_GATE_VERSION
    assert result["derived"] is False


def test_full_real_oos_execution_and_paper_evidence_can_reach_paper_validated():
    candidate = _candidate()
    snapshot = {
        "snapshot_hash": "a" * 64,
        "snapshot_ref": "fixture://market-data-snapshot-1",
        "as_of": "2026-10-20T16:00:00+08:00",
    }
    result = build_tradeability_certificate(
        candidate,
        data_snapshot=snapshot,
        backtest_evidence=_backtest_evidence(),
        paper_evidence=_paper_evidence(),
        issued_at="2026-10-20T16:00:00+08:00",
    )

    assert result["status"] == "paper_validated"
    assert result["reason_codes"] == []
    assert result["backtest"]["oos"]["fold_count"] == 3
    assert result["backtest"]["multiple_testing"]["checks"] == {
        "dsr": True,
        "pbo": True,
        "white_reality_check": True,
        "hansen_spa": True,
    }
    assert result["paper"]["valid_trading_days"] == 60
    assert result["paper"]["closed_round_trip_count"] == 30
    assert result["paper"]["net_return_block_bootstrap"]["ci_lower"] > 0
    assert len(result["certificate_hash"]) == 64


def test_certificate_is_stale_after_candidate_parameter_change():
    candidate = _candidate()
    snapshot = {"snapshot_hash": "b" * 64, "as_of": "2026-10-20"}
    certificate = build_tradeability_certificate(candidate, data_snapshot=snapshot)
    changed = {**candidate, "params": {**candidate["params"], "short_period": 7}}

    result = validate_tradeability_certificate_freshness(
        certificate,
        changed,
        data_snapshot=snapshot,
    )

    assert result["valid"] is False
    assert "certificate_stale_tested_object_hash" in result["reason_codes"]
