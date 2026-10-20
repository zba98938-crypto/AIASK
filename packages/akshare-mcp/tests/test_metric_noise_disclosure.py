"""The unsubscribed metric-noise disclosure must be readable in the response.

``STRATEGY_METRIC_NOISE_ENABLED`` (default on) perturbs Sharpe/Calmar for
non-subscribed readers. The perturbation must be accompanied by a
deterministic ``metrics_disclosure`` block so consumers can tell exact from
approximate without guessing.
"""

from __future__ import annotations

import os


def _metrics_rows() -> list[dict]:
    return [
        {"period": "1d", "total_return": 0.02, "annual_return": 0.3, "sharpe_ratio": 1.2, "calmar_ratio": 1.5},
        {"period": "7d", "total_return": -0.01, "sharpe_ratio": 0.4, "calmar_ratio": 0.5},
    ]


def test_unsubscribed_noise_applies_and_flags_rows(monkeypatch) -> None:
    from akshare_mcp.tools.managers.strategy_mgr_crud.handlers_catalog import apply_metric_noise

    monkeypatch.setenv("STRATEGY_METRIC_NOISE_ENABLED", "1")
    metrics = _metrics_rows()
    applied, scale = apply_metric_noise(metrics, is_subscribed=False)
    assert applied is True
    assert scale is not None and abs(scale - 1.0) <= 0.001
    for row in metrics:
        assert row["approximate"] is True


def test_subscribed_reader_gets_exact_metrics(monkeypatch) -> None:
    from akshare_mcp.tools.managers.strategy_mgr_crud.handlers_catalog import apply_metric_noise

    monkeypatch.setenv("STRATEGY_METRIC_NOISE_ENABLED", "1")
    metrics = _metrics_rows()
    applied, scale = apply_metric_noise(metrics, is_subscribed=True)
    assert applied is False
    assert scale is None
    assert metrics[0]["sharpe_ratio"] == 1.2
    assert "approximate" not in metrics[0]


def test_noise_can_be_disabled_via_env(monkeypatch) -> None:
    from akshare_mcp.tools.managers.strategy_mgr_crud.handlers_catalog import apply_metric_noise

    monkeypatch.setenv("STRATEGY_METRIC_NOISE_ENABLED", "0")
    metrics = _metrics_rows()
    applied, scale = apply_metric_noise(metrics, is_subscribed=False)
    assert applied is False
    assert metrics[0]["sharpe_ratio"] == 1.2
