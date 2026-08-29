from __future__ import annotations

from aiask_agent.trade_risk_guard import TradeRiskConfig


def test_trade_risk_config_rejects_invalid_numeric_env(monkeypatch) -> None:
    monkeypatch.setenv("TRADE_MAX_SINGLE_AMOUNT", "nan")
    monkeypatch.setenv("TRADE_MAX_DAILY_COUNT", "0")
    monkeypatch.setenv("TRADE_MAX_DAILY_AMOUNT", "inf")
    monkeypatch.setenv("TRADE_MAX_POSITION_RATIO", "2")
    monkeypatch.setenv("TRADE_FORBIDDEN_HOURS_START", "-5")
    monkeypatch.setenv("TRADE_FORBIDDEN_HOURS_END", "99")
    monkeypatch.setenv("TRADE_CONFIRM_TIMEOUT", "bad")

    config = TradeRiskConfig()

    assert config.max_single_order_amount == 100000.0
    assert config.max_daily_trade_count == 1
    assert config.max_daily_trade_amount == 500000.0
    assert config.max_position_ratio == 1.0
    assert config.forbidden_hours_start == 0
    assert config.forbidden_hours_end == 23
    assert config.confirmation_timeout_seconds == 300
