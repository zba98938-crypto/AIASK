"""Behavioural tests for the DSR trial-count denominator.

The multiple-testing report used to feed DSR the family *column* count
(1 candidate + 5 basis factors = 6), whatever the real number of trials
was. A recorded trial count that exceeds the columns must not be dropped
silently: the report discloses which denominator it actually used.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from akshare_mcp.services._factor_validation_pipeline_support import (
    _build_multiple_testing_report,
)
from akshare_mcp.services.factor_candidate_compiler import compile_factor_candidate


def _candidate(expression: str) -> dict:
    return {
        "name": "probe_factor",
        "hypothesis": "probe",
        "family": "momentum",
        "inputs": ["close"],
        "expression_dsl": expression,
        "expected_holding_period": 10,
        "expected_regime": [],
        "complexity_hint": "medium",
        "novelty_rationale": "probe",
    }


def _frames(n_dates: int = 60, n_codes: int = 8, seed: int = 7):
    """Small cross-section frames: enough for the report, cheap to build."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n_dates, freq="B")
    codes = [f"{600000 + i:06d}" for i in range(n_codes)]
    frames: dict[str, pd.DataFrame] = {}
    for code in codes:
        close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, size=n_dates))
        rows = pd.DataFrame(
            {
                "date": dates.astype(str),
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": rng.integers(1_000_000, 5_000_000, size=n_dates),
                "amount": rng.integers(1_000_000, 5_000_000, size=n_dates) * close,
                "turnover": 0.5,
            }
        )
        frames[code] = rows
    return frames


def _report(frames, recorded_trial_count=None):
    factor_df = pd.DataFrame()
    close_df = pd.DataFrame()
    from akshare_mcp.services._factor_validation_pipeline_support import (
        _build_panel,
        _build_horizon_return_panel,
        evaluate_compiled_factor,
    )
    factor_series_map, close_series_map = {}, {}
    compiled = compile_factor_candidate(_candidate("momentum_20d"))
    for code, frame in frames.items():
        s = evaluate_compiled_factor(compiled, frame)
        factor_series_map[code] = pd.Series(
            s.values, index=frame["date"].astype(str), dtype=float
        )
        close_series_map[code] = pd.Series(
            pd.to_numeric(frame["close"]).values,
            index=frame["date"].astype(str),
            dtype=float,
        )
    factor_df = _build_panel(factor_series_map)
    close_df = _build_panel(close_series_map)
    return_df = _build_horizon_return_panel(close_df, 10)
    return _build_multiple_testing_report(
        compiled, frames, factor_df, return_df, recorded_trial_count=recorded_trial_count
    )


def test_dsr_denominator_defaults_to_family_columns():
    report = _report(_frames())
    if not report.get("available"):
        raise AssertionError(f"report unexpectedly unavailable: {report}")
    assert report["n_trials"] == report["family_member_count"]
    assert report["n_trials_source"] == "family_columns"


def test_recorded_trial_count_widens_denominator_and_is_disclosed():
    report = _report(_frames(), recorded_trial_count=10)
    if not report.get("available"):
        raise AssertionError(f"report unexpectedly unavailable: {report}")
    assert report["n_trials"] == 10
    assert report["n_trials_source"] == "recorded_trial_count"


def test_recorded_trial_count_never_shrinks_denominator():
    report = _report(_frames(), recorded_trial_count=2)
    if not report.get("available"):
        raise AssertionError(f"report unexpectedly unavailable: {report}")
    assert report["n_trials"] == report["family_member_count"]
    assert report["n_trials_source"] == "family_columns"
