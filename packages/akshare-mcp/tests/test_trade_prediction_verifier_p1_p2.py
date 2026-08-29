from __future__ import annotations

import asyncio
import json
from datetime import date

from akshare_mcp.services.incubation_factory import trade_prediction_verifier as verifier_mod
from akshare_mcp.services.incubation_factory.feedback_writer import FeedbackWriter
from akshare_mcp.services.incubation_factory.hit_rate_reporter import HitRateReporter
from akshare_mcp.services.incubation_factory.trade_prediction_verifier import (
    DAILY_SCORE_VERSION,
    INTRADAY_SCORE_VERSION,
    IntradayReplayService,
    TradePredictionDailyVerifier,
)


def _prediction() -> dict:
    contract = {
        "strategy_id": "strategy-p1",
        "stock_code": "600000.SH",
        "prediction_as_of": "2026-06-05T09:30:00+08:00",
        "target_trading_date": "2026-06-08",
        "direction": "up",
        "confidence": 0.72,
        "horizon": "next_day",
        "contract_version": "strategy_factory.trade_prediction_contract.v1",
        "contract_source": "explicit",
        "target_price_range": [10.4, 10.8],
        "entry_window": "09:30-09:45",
        "exit_window": "14:45-15:00",
        "time_buckets": ["09:30-10:00", "14:30-15:00"],
        "risk_rules": {"stop_loss_pct": 0.06, "take_profit_pct": 0.12},
        "evidence_refs": [{"id": "ev-before", "observed_at": "2026-06-05T09:00:00+08:00"}],
        "family": "breakout",
        "regime": "risk_on",
    }
    return {
        "prediction_id": "prediction-p1",
        "strategy_id": "strategy-p1",
        "stock_code": "600000.SH",
        "target_trading_date": "2026-06-08",
        "direction": "up",
        "prediction_status": "pending",
        "contract_json": contract,
        "metadata": {"family": "breakout", "stage": "observe", "factor": ["momentum"]},
    }


class _VerifierDb:
    def __init__(self):
        self.saved: list[dict] = []

    async def list_strategy_trade_predictions(self, **_kwargs):
        return [_prediction()]

    async def get_klines(self, code, start_date=None, end_date=None, limit=None):
        if code not in {"600000.SH", "600000", "sh600000"}:
            return []
        return [
            {
                "date": "2026-06-08",
                "open": 10.0,
                "high": 10.7,
                "low": 9.9,
                "close": 10.5,
                "volume": 1000,
            }
        ]

    async def save_strategy_trade_prediction_outcome(self, payload):
        self.saved.append(dict(payload))
        return payload


def test_daily_verifier_scores_direction_target_and_risk_proxy():
    db = _VerifierDb()

    async def _run():
        return await TradePredictionDailyVerifier().verify_pending(
            db,
            as_of=date(2026, 6, 8),
            include_intraday=False,
        )

    result = asyncio.run(_run())
    assert result["evaluated"] == 1
    assert db.saved[0]["score_version"] == DAILY_SCORE_VERSION
    assert db.saved[0]["score_status"] == "ok"
    assert db.saved[0]["outcome_json"]["direction_hit"] is True
    assert db.saved[0]["outcome_json"]["target_touch"] is True
    assert db.saved[0]["outcome_json"]["risk_proxy_score"] > 0


class _NonFiniteDailyBarDb(_VerifierDb):
    async def get_klines(self, code, start_date=None, end_date=None, limit=None):
        return [
            {
                "date": "2026-06-08",
                "open": "nan",
                "high": "inf",
                "low": 9.9,
                "close": 10.5,
            }
        ]


def test_daily_verifier_rejects_non_finite_ohlc_without_leaking_json_values():
    db = _NonFiniteDailyBarDb()

    async def _run():
        return await TradePredictionDailyVerifier().verify_pending(
            db,
            as_of=date(2026, 6, 8),
            include_intraday=False,
        )

    result = asyncio.run(_run())
    outcome = result["outcomes"][0]
    assert outcome["score_status"] == "pending_market_data"
    assert outcome["data_quality_status"] == "invalid_ohlc"
    assert outcome["trade_prediction_score"] is None
    json.dumps(result, allow_nan=False)


def test_verifier_syncs_intraday_before_replay(monkeypatch):
    db = _VerifierDb()
    calls = []

    async def fake_sync(db_arg, code, **kwargs):
        calls.append((db_arg, code, kwargs))
        return {"status": "ok", "accepted_count": 9}

    monkeypatch.setattr(verifier_mod, "sync_minute_kline_to_storage", fake_sync)

    async def _run():
        return await TradePredictionDailyVerifier().verify_pending(
            db,
            as_of=date(2026, 6, 8),
            include_intraday=True,
            sync_intraday_before_replay=True,
            intraday_period="5m",
            intraday_limit=120,
        )

    result = asyncio.run(_run())
    assert [(code, kwargs["period"], kwargs["limit"]) for _, code, kwargs in calls] == [
        ("600000.SH", "5m", 120)
    ]
    assert result["evaluated"] == 1
    assert result["intraday_evaluated"] == 1
    assert result["intraday_sync"]["attempted"] == 1
    assert result["intraday_sync"]["succeeded"] == 1
    assert result["intraday_sync"]["failed"] == 0
    assert any(item["score_version"] == DAILY_SCORE_VERSION for item in db.saved)
    assert any(item["score_version"] == INTRADAY_SCORE_VERSION for item in db.saved)


def test_verifier_intraday_sync_failure_keeps_daily_and_partial_v2(monkeypatch):
    db = _VerifierDb()

    async def failing_sync(*_args, **_kwargs):
        raise RuntimeError("minute source down")

    monkeypatch.setattr(verifier_mod, "sync_minute_kline_to_storage", failing_sync)

    async def _run():
        return await TradePredictionDailyVerifier().verify_pending(
            db,
            as_of=date(2026, 6, 8),
            include_intraday=True,
            sync_intraday_before_replay=True,
        )

    result = asyncio.run(_run())
    assert result["evaluated"] == 1
    assert result["intraday_evaluated"] == 1
    assert result["intraday_sync"]["attempted"] == 1
    assert result["intraday_sync"]["failed"] == 1
    assert result["intraday_sync"]["status_counts"]["exception"] == 1
    assert "minute source down" in result["intraday_sync"]["errors"][0]["reason"]
    daily = [item for item in db.saved if item["score_version"] == DAILY_SCORE_VERSION]
    intraday = [item for item in db.saved if item["score_version"] == INTRADAY_SCORE_VERSION]
    assert daily and daily[0]["score_status"] == "ok"
    assert intraday and intraday[0]["score_status"] == "partial_intraday_missing"


class _IntradayDb:
    def __init__(self, bars):
        self.bars = bars
        self.saved: list[dict] = []

    async def list_intraday_bars(self, code, period, **_kwargs):
        return list(self.bars) if code == "600000.SH" and period == "5m" else []

    async def save_strategy_trade_prediction_outcome(self, payload):
        self.saved.append(dict(payload))
        return payload


def _bars():
    return [
        {"timestamp": "2026-06-08 09:31:00", "open": 10.00, "high": 10.10, "low": 9.98, "close": 10.08, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 09:36:00", "open": 10.08, "high": 10.25, "low": 10.05, "close": 10.22, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 09:50:00", "open": 10.22, "high": 10.42, "low": 10.20, "close": 10.38, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 10:00:00", "open": 10.38, "high": 10.50, "low": 10.30, "close": 10.45, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 11:00:00", "open": 10.45, "high": 10.62, "low": 10.40, "close": 10.55, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 13:30:00", "open": 10.55, "high": 10.70, "low": 10.48, "close": 10.60, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 14:40:00", "open": 10.60, "high": 10.72, "low": 10.55, "close": 10.66, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 14:50:00", "open": 10.66, "high": 10.78, "low": 10.60, "close": 10.75, "data_quality_status": "ok"},
        {"timestamp": "2026-06-08 14:55:00", "open": 10.75, "high": 10.82, "low": 10.70, "close": 10.80, "data_quality_status": "ok"},
    ]


def test_intraday_replay_writes_v2_when_bars_complete():
    db = _IntradayDb(_bars())

    async def _run():
        return await IntradayReplayService(min_bars=8).replay_prediction(db, _prediction())

    outcome = asyncio.run(_run())
    assert outcome["score_version"] == INTRADAY_SCORE_VERSION
    assert outcome["score_status"] == "ok"
    assert outcome["outcome_json"]["entry_window_hit"] is True
    assert outcome["outcome_json"]["exit_window_hit"] is True
    assert outcome["outcome_json"]["time_bucket_hit_rate"] == 1.0
    assert db.saved[0]["score_version"] == INTRADAY_SCORE_VERSION


def test_intraday_replay_marks_partial_when_intraday_missing():
    db = _IntradayDb([])

    async def _run():
        return await IntradayReplayService().replay_prediction(db, _prediction())

    outcome = asyncio.run(_run())
    assert outcome["score_version"] == INTRADAY_SCORE_VERSION
    assert outcome["score_status"] == "partial_intraday_missing"
    assert outcome["data_quality_status"] == "intraday_missing"


def test_intraday_replay_rejects_non_finite_ohlc_without_leaking_json_values():
    bars = _bars()
    bars[0] = {**bars[0], "high": float("inf")}
    db = _IntradayDb(bars)

    async def _run():
        return await IntradayReplayService(min_bars=8).replay_prediction(db, _prediction())

    outcome = asyncio.run(_run())
    assert outcome["score_version"] == INTRADAY_SCORE_VERSION
    assert outcome["score_status"] == "partial_intraday_missing"
    assert outcome["outcome_json"]["reason"] == "invalid_ohlc"
    json.dumps(outcome, allow_nan=False)


class _ReporterDb:
    def __init__(self):
        self.events: list[dict] = []

    async def summarize_strategy_trade_predictions(self, **_kwargs):
        return {
            "sample_n": 1,
            "partial_count": 1,
            "score_status_counts": {"partial_intraday_missing": 1},
            "data_quality_status_counts": {"intraday_missing": 1},
        }

    async def aggregate_trade_prediction_matrix(self, **_kwargs):
        return {
            "rows": [
                {
                    "dimension": "family",
                    "value": "breakout",
                    "sample_n": 12,
                    "score_avg": 0.72,
                    "score_lcb_95": 0.56,
                }
            ],
            "row_count": 1,
        }

    async def save_strategy_domain_event(self, payload):
        self.events.append(dict(payload))
        return payload


def test_reporter_adds_prediction_dashboard_and_diagnostic_feedback():
    reporter = HitRateReporter()

    async def _run():
        return await reporter.generate(
            _ReporterDb(),
            [],
            {},
            {"auto_promoted": 0, "stage_counts": {}},
            trade_prediction_result={"status": "ok", "evaluated": 1},
        )

    report = asyncio.run(_run())
    dashboard = report["trade_prediction_dashboard"]
    assert dashboard["summary"]["sample_n"] == 1
    assert dashboard["matrix"]["row_count"] == 1
    prediction_feedback = report["feedback_actions"]["prediction_feedback"]
    assert prediction_feedback["enabled_for_controls"] is False
    assert any(item["action"] == "repair_data" for item in prediction_feedback["suggestions"])


def test_feedback_writer_prediction_feedback_is_diagnostic_by_default(monkeypatch):
    monkeypatch.setenv("STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED", "0")
    db = _ReporterDb()
    report = {
        "report_date": "2026-06-08",
        "feedback_actions": {
            "prediction_feedback": {
                "suggestions": [{"action": "boost", "dimension": "family", "value": "breakout"}],
                "sample_n": 12,
                "partial_count": 0,
            }
        },
        "trade_prediction_dashboard": {"summary": {"sample_n": 12}},
    }

    result = asyncio.run(FeedbackWriter().write(db, report))
    prediction_events = [
        item for item in db.events if item.get("event_type") == "incubation_factory.prediction_feedback_written"
    ]
    assert result["prediction_budget_feedback_enabled"] is False
    assert result["written_controls"] == 0
    assert prediction_events
    assert prediction_events[0]["payload"]["enabled_for_budget_feedback"] is False
    assert prediction_events[0]["payload"]["budget_suggestions"] == []
