from __future__ import annotations

import json

import pytest

from strategy_factory.application.factory_execution import build_run_artifacts
from strategy_factory import mcp_control


def test_tradeability_certificate_is_persisted_as_separate_artifact():
    certificate = {
        "certificate_version": "test.v1",
        "status": "research_only",
        "reason_codes": ["real_oos_backtest_evidence_missing"],
    }

    artifacts = build_run_artifacts({"tradeability_certificates": [certificate]})

    assert len(artifacts) == 1
    assert artifacts[0]["artifact_type"] == "tradeability_certificate"
    assert artifacts[0]["payload_json"] == certificate
    assert len(artifacts[0]["payload_hash"]) == 64


@pytest.mark.asyncio
async def test_certificate_read_api_is_read_only_and_never_authorizes_live_trading(monkeypatch):
    class _DB:
        async def initialize(self):
            return None

        async def list_strategy_factory_run_artifacts(self, run_id: str):
            assert run_id == "run-1"
            return [
                {
                    "artifact_type": "tradeability_certificate",
                    "artifact_version": "1",
                    "payload_hash": "a" * 64,
                    "storage_mode": "inline_compact_json",
                    "payload_json": json.dumps({"status": "research_only"}),
                }
            ]

    monkeypatch.setattr(mcp_control, "get_db", lambda: _DB())

    result = await mcp_control.factory_tradeability_certificates("run-1")

    assert result["read_only"] is True
    assert result["certificate_only_no_strategy_promotion"] is True
    assert result["live_trading_enabled"] is False
    assert result["count"] == 1
    assert result["certificates"][0]["certificate"]["status"] == "research_only"
