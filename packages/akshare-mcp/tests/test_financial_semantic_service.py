from __future__ import annotations

import asyncio
import math

from akshare_mcp.services.financial_semantic_service import FinancialSemanticConfig, FinancialSemanticService


def _assert_all_finite(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_all_finite(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _assert_all_finite(nested)
        return
    if isinstance(value, float):
        assert math.isfinite(value)


def test_financial_semantic_aggregate_sanitizes_non_finite_remote_scores() -> None:
    rows = [
        {
            "index": 0,
            "entity_sentiment": float("inf"),
            "event_sentiment": "-inf",
            "surprise": "nan",
            "credibility": "inf",
            "recency": float("nan"),
            "event_types": ["earnings"],
            "risk_tags": ["governance_risk"],
        }
    ]

    result = FinancialSemanticService._aggregate_rows(
        rows,
        provider="openai_compatible",
        model="mock-semantic",
    )

    assert result["available"] is True
    assert result["score"] == 50.0
    assert result["entity_sentiment"] == 0.0
    assert result["event_sentiment"] == 0.0
    assert result["surprise"] == 0.0
    assert result["credibility"] == 0.6
    assert result["documents"][0]["entity_sentiment"] == 0.0
    assert result["documents"][0]["event_sentiment"] == 0.0
    assert result["documents"][0]["surprise"] == 0.0
    assert result["documents"][0]["credibility"] == 0.6
    assert result["documents"][0]["recency"] == 0.85
    _assert_all_finite(result)


def test_financial_semantic_timeout_and_temperature_reject_non_finite_config() -> None:
    service = FinancialSemanticService(
        FinancialSemanticConfig(
            enabled=True,
            provider="openai_compatible",
            base_url="https://semantic.example.test/v1",
            api_key="test-key",
            model="test-semantic",
            timeout_sec=float("inf"),
            connect_timeout_sec=float("nan"),
            write_timeout_sec="-inf",
            pool_timeout_sec="inf",
            temperature=float("inf"),
        )
    )

    timeout = service._timeout()

    assert timeout.connect == 5.0
    assert timeout.read == 20.0
    assert timeout.write == 10.0
    assert timeout.pool == 5.0

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"documents":[]}'}}]}

    class FakeClient:
        def __init__(self):
            self.payload = None

        async def post(self, endpoint, *, headers=None, json=None, timeout=None):
            self.payload = json
            return FakeResponse()

    fake_client = FakeClient()

    async def _fake_ensure_client():
        return fake_client

    service._ensure_client = _fake_ensure_client  # type: ignore[method-assign]

    asyncio.run(service._request_openai_compatible([{"index": 0, "text": "sample"}]))

    assert fake_client.payload["temperature"] == 0.1
