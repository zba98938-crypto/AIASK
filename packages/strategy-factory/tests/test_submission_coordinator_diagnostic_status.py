from __future__ import annotations

import pytest

from strategy_factory.application.services.submission_coordinator import StrategyUpsertService
from strategy_factory.application.trade_prediction_contract import TRADE_PREDICTION_CONTRACT_VERSION


class _DB:
    def __init__(self) -> None:
        self.saved_strategy: dict | None = None
        self.saved_trade_predictions: list[dict] = []

    async def save_strategy(self, data: dict) -> dict:
        self.saved_strategy = dict(data)
        return dict(data)

    async def save_strategy_trade_prediction(self, payload: dict) -> dict:
        self.saved_trade_predictions.append(dict(payload))
        return dict(payload)


class _Submitter:
    def __init__(self) -> None:
        self.status_updates: list[dict] = []

    async def _persist_metrics(self, strategy_id, metrics, validation_report, risk_report, db) -> None:
        return None

    async def _update_strategy_status(
        self,
        db,
        strategy_id,
        status,
        *,
        actor_id,
        reason,
        metadata,
    ) -> None:
        self.status_updates.append(
            {
                "strategy_id": strategy_id,
                "status": status,
                "actor_id": actor_id,
                "reason": reason,
                "metadata": dict(metadata or {}),
            }
        )


@pytest.mark.asyncio
async def test_diagnostic_candidate_save_payload_uses_diagnostic_status(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_STATUS", raising=False)
    db = _DB()
    submitter = _Submitter()

    persisted = await StrategyUpsertService(submitter).persist_candidate(
        strategy_id="diag-1",
        candidate={
            "diagnostic_observation": True,
            "diagnostic_fingerprint": "diag_same",
            "diagnostic_reason": "win_rate_0_372_0_400",
        },
        data={"id": "diag-1", "status": "draft"},
        metrics={},
        validation_report=None,
        risk_report=None,
        gate={"passed": False},
        db=db,
        refresh_existing=False,
        read_only=False,
    )

    assert persisted is True
    assert db.saved_strategy is not None
    assert db.saved_strategy["status"] == "diagnostic"
    assert submitter.status_updates[0]["status"] == "diagnostic"
    assert submitter.status_updates[0]["reason"] == "factory_submit_diagnostic_observation"


@pytest.mark.asyncio
async def test_diagnostic_candidate_save_payload_honors_status_rollback(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_STATUS", "submitted")
    db = _DB()
    submitter = _Submitter()

    await StrategyUpsertService(submitter).persist_candidate(
        strategy_id="diag-rollback",
        candidate={"diagnostic_observation": True},
        data={"id": "diag-rollback", "status": "draft"},
        metrics={},
        validation_report=None,
        risk_report=None,
        gate={"passed": False},
        db=db,
        refresh_existing=False,
        read_only=False,
    )

    assert db.saved_strategy is not None
    assert db.saved_strategy["status"] == "submitted"
    assert submitter.status_updates[0]["status"] == "submitted"


@pytest.mark.asyncio
async def test_status_update_metadata_includes_strategy_explanation() -> None:
    db = _DB()
    submitter = _Submitter()

    explanation = {
        "version": "strategy_explanation.v1",
        "summary": "Explained strategy summary",
        "labels": ["strategy_explained", "type:momentum"],
        "why_generated": "source=external_llm; rationale=relative strength",
    }

    persisted = await StrategyUpsertService(submitter).persist_candidate(
        strategy_id="explained-1",
        candidate={"id": "candidate-explained", "strategy_explanation": explanation},
        data={
            "id": "explained-1",
            "status": "draft",
            "params": {"strategy_explanation": explanation},
        },
        metrics={},
        validation_report=None,
        risk_report=None,
        gate={"passed": True},
        db=db,
        refresh_existing=False,
        read_only=False,
    )

    assert persisted is True
    metadata = submitter.status_updates[0]["metadata"]
    assert metadata["strategy_explanation"] == explanation
    assert metadata["strategy_explanation_summary"] == "Explained strategy summary"
    assert metadata["strategy_explanation_labels"] == ["strategy_explained", "type:momentum"]


@pytest.mark.asyncio
async def test_ready_trade_prediction_is_persisted_after_strategy_save() -> None:
    db = _DB()
    submitter = _Submitter()
    contract = {
        "strategy_id": "temporary-candidate-id",
        "stock_code": "600000.SH",
        "prediction_as_of": "2026-06-05T01:30:00+00:00",
        "target_trading_date": "2026-06-08",
        "direction": "up",
        "confidence": 0.71,
        "horizon": "next_day",
        "evidence_refs": ["ev-1"],
        "contract_version": TRADE_PREDICTION_CONTRACT_VERSION,
        "contract_source": "explicit",
        "contract_hash": "a" * 64,
    }

    persisted = await StrategyUpsertService(submitter).persist_candidate(
        strategy_id="strategy-final-id",
        candidate={"id": "temporary-candidate-id"},
        data={
            "id": "strategy-final-id",
            "status": "draft",
            "params": {
                "trade_prediction_contract": contract,
                "trade_prediction_contract_status": "ready",
                "trade_prediction_contract_hash": contract["contract_hash"],
            },
        },
        metrics={},
        validation_report=None,
        risk_report=None,
        gate={"passed": True, "submission_lane": "formal_incubation"},
        db=db,
        refresh_existing=False,
        read_only=False,
    )

    assert persisted is True
    assert db.saved_strategy is not None
    assert len(db.saved_trade_predictions) == 1
    prediction = db.saved_trade_predictions[0]
    assert prediction["prediction_id"] == "tp_strategy-final-id_aaaaaaaaaaaaaaaa"
    assert prediction["strategy_id"] == "strategy-final-id"
    assert prediction["contract_json"]["strategy_id"] == "strategy-final-id"
    assert prediction["contract_hash"] == contract["contract_hash"]
    assert prediction["metadata"]["candidate_id"] == "temporary-candidate-id"
    assert prediction["metadata"]["submission_lane"] == "formal_incubation"
