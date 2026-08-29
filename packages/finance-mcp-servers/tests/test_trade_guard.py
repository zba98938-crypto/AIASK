"""Tests for the trade-risk explicit-token guard."""

from __future__ import annotations

import os

import pytest

from aiask_finance_mcp._shared import trade_guard
from aiask_finance_mcp._shared.trade_guard import (
    TradeGuardError,
    require_broker_token,
    trade_risk_envelope,
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("AIASK_FINANCE_TEST_TOKEN", raising=False)
    yield


def test_rejects_when_env_var_unset() -> None:
    with pytest.raises(TradeGuardError, match="not configured"):
        require_broker_token({"broker_token": "anything"}, env_var="AIASK_FINANCE_TEST_TOKEN")


def test_rejects_when_arg_missing(monkeypatch) -> None:
    monkeypatch.setenv("AIASK_FINANCE_TEST_TOKEN", "secret")
    with pytest.raises(TradeGuardError, match="broker_token is required"):
        require_broker_token({}, env_var="AIASK_FINANCE_TEST_TOKEN")


def test_rejects_on_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("AIASK_FINANCE_TEST_TOKEN", "secret")
    with pytest.raises(TradeGuardError, match="broker_token mismatch"):
        require_broker_token({"broker_token": "wrong"}, env_var="AIASK_FINANCE_TEST_TOKEN")


def test_passes_when_match(monkeypatch) -> None:
    monkeypatch.setenv("AIASK_FINANCE_TEST_TOKEN", "secret")
    require_broker_token({"broker_token": "secret"}, env_var="AIASK_FINANCE_TEST_TOKEN")


def test_broker_token_guard_uses_constant_time_compare(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_compare_digest(provided: str, expected: str) -> bool:
        calls.append((provided, expected))
        return provided == expected

    monkeypatch.setenv("AIASK_FINANCE_TEST_TOKEN", "secret")
    monkeypatch.setattr(trade_guard.hmac, "compare_digest", fake_compare_digest)

    require_broker_token({"broker_token": "secret"}, env_var="AIASK_FINANCE_TEST_TOKEN")
    with pytest.raises(TradeGuardError, match="broker_token mismatch"):
        require_broker_token({"broker_token": "wrong"}, env_var="AIASK_FINANCE_TEST_TOKEN")

    assert calls == [("secret", "secret"), ("wrong", "secret")]


def test_envelope_shape() -> None:
    envelope = trade_risk_envelope(TradeGuardError("missing"), tool="ths_place_order")
    assert envelope["success"] is False
    assert envelope["error_code"] == "TRADE_RISK_TOKEN_REQUIRED"
    side_effect = envelope["meta"]["side_effect"]
    assert side_effect["level"] == "trade_risk"
    assert side_effect["confirmation_required"] is True
    assert side_effect["explicit_token_required"] is True
    assert envelope["meta"]["tool"] == "ths_place_order"
