from __future__ import annotations

import asyncio
import sqlite3

from aiask_quant_core.storage.sqlite import SQLiteAdapter


def _prediction_payload() -> dict:
    contract = {
        "strategy_id": "strategy-p0",
        "stock_code": "600000.SH",
        "prediction_as_of": "2026-06-05T01:30:00+00:00",
        "target_trading_date": "2026-06-08",
        "direction": "up",
        "confidence": 0.72,
        "horizon": "next_day",
        "contract_version": "strategy_factory.trade_prediction_contract.v1",
        "contract_source": "explicit",
        "evidence_refs": ["ev-1"],
        "contract_hash": "hash-p0",
    }
    return {
        "prediction_id": "prediction-p0",
        "contract_json": contract,
        "metadata": {"source": "unit"},
    }


def test_strategy_trade_prediction_schema_is_initialized(tmp_path) -> None:
    db_path = tmp_path / "trade_prediction_schema.sqlite3"
    db = SQLiteAdapter(path=db_path)

    async def _run() -> None:
        try:
            await db.initialize()
        finally:
            await db.close()

    asyncio.run(_run())

    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "strategy_trade_predictions" in names
    assert "strategy_trade_prediction_outcomes" in names
    assert "kline_intraday" in names
    assert "idx_strategy_trade_predictions_strategy" in names
    assert "idx_strategy_trade_prediction_outcomes_prediction" in names
    assert "idx_kline_intraday_code_period_time" in names


def test_strategy_trade_prediction_crud_round_trips_json(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "trade_prediction_crud.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            saved = await db.save_strategy_trade_prediction(_prediction_payload())
            loaded = await db.get_strategy_trade_prediction("prediction-p0")
            listed = await db.list_strategy_trade_predictions(strategy_id="strategy-p0")

            assert saved["prediction_id"] == "prediction-p0"
            assert loaded is not None
            assert loaded["contract_hash"] == "hash-p0"
            assert loaded["contract_json"]["evidence_refs"] == ["ev-1"]
            assert loaded["metadata"] == {"source": "unit"}
            assert [row["prediction_id"] for row in listed] == ["prediction-p0"]

            outcome = await db.save_strategy_trade_prediction_outcome(
                {
                    "prediction_id": "prediction-p0",
                    "score_version": "trade_prediction_score_daily_v1",
                    "score_status": "partial_daily_only",
                    "trade_prediction_score": 0.61,
                    "actual_trading_date": "2026-06-08",
                    "outcome_json": {"direction_hit": True},
                    "metadata": {"phase": "p0"},
                }
            )
            outcomes = await db.list_strategy_trade_prediction_outcomes(prediction_id="prediction-p0")

            assert outcome["strategy_id"] == "strategy-p0"
            assert outcome["stock_code"] == "600000.SH"
            assert outcome["outcome_json"] == {"direction_hit": True}
            assert outcomes[0]["score_version"] == "trade_prediction_score_daily_v1"
            assert outcomes[0]["trade_prediction_score"] == 0.61
        finally:
            await db.close()

    asyncio.run(_run())


def test_strategy_trade_prediction_rejects_hash_mutation(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "trade_prediction_hash_guard.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            await db.save_strategy_trade_prediction(_prediction_payload())
            mutated = _prediction_payload()
            mutated["contract_json"] = {
                **mutated["contract_json"],
                "contract_hash": "hash-mutated",
            }
            try:
                await db.save_strategy_trade_prediction(mutated)
            except ValueError as exc:
                assert "contract_hash mismatch" in str(exc)
            else:
                raise AssertionError("expected immutable contract_hash guard")
        finally:
            await db.close()

    asyncio.run(_run())


def test_intraday_bars_upsert_list_and_quality_status(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "trade_prediction_intraday.sqlite3")

    async def _run() -> None:
        try:
            await db.initialize()
            summary = await db.save_intraday_bars(
                "600000.SH",
                [
                    {
                        "timestamp": "2026-06-08 09:35:00",
                        "open": 10.0,
                        "high": 10.2,
                        "low": 9.9,
                        "close": 10.1,
                        "volume": 1000,
                        "source_chain": ["akshare", "unit"],
                    },
                    {
                        "timestamp": "2026-06-08 09:31:00",
                        "open": 9.8,
                        "high": 9.9,
                        "low": 9.7,
                        "close": 9.85,
                        "volume": 800,
                    },
                    {
                        "timestamp": "2026-06-08 09:40:00",
                        "open": 10.1,
                        "high": 10.0,
                        "low": 10.2,
                        "close": 10.15,
                        "volume": 100,
                    },
                ],
                period="5",
                source="unit",
            )
            rows = await db.list_intraday_bars("600000.SH", "5m")

            assert summary["accepted_count"] == 3
            assert summary["rejected_count"] == 0
            assert summary["data_quality_status_counts"] == {"ok": 2, "invalid_ohlc": 1}
            assert [row["timestamp"][11:16] for row in rows] == ["09:31", "09:35", "09:40"]
            assert rows[0]["period"] == "5m"
            assert rows[1]["source_chain"] == ["akshare", "unit"]
            assert rows[2]["data_quality_status"] == "invalid_ohlc"
        finally:
            await db.close()

    asyncio.run(_run())


def test_trade_prediction_pending_filters_and_aggregates(tmp_path) -> None:
    db = SQLiteAdapter(path=tmp_path / "trade_prediction_aggregates.sqlite3")

    def payload(prediction_id: str, target: str, *, status: str = "pending", family: str = "breakout") -> dict:
        base = _prediction_payload()
        contract = {
            **base["contract_json"],
            "strategy_id": "strategy-p1",
            "target_trading_date": target,
            "contract_hash": f"hash-{prediction_id}",
            "family": family,
            "regime": "risk_on",
        }
        return {
            "prediction_id": prediction_id,
            "contract_json": contract,
            "prediction_status": status,
            "metadata": {"family": family, "stage": "observe", "factor": ["momentum"]},
        }

    async def _run() -> None:
        try:
            await db.initialize()
            await db.save_strategy_trade_prediction(payload("prediction-p1-a", "2026-06-08"))
            await db.save_strategy_trade_prediction(payload("prediction-p1-b", "2026-06-09", status="frozen"))
            await db.save_strategy_trade_prediction(payload("prediction-p1-c", "2026-06-15"))
            await db.save_strategy_trade_prediction_outcome(
                {
                    "prediction_id": "prediction-p1-a",
                    "score_version": "trade_prediction_score_daily_v1",
                    "score_status": "ok",
                    "trade_prediction_score": 0.76,
                    "actual_trading_date": "2026-06-08",
                    "data_quality_status": "ok",
                    "outcome_json": {
                        "direction_hit": True,
                        "target_touch": True,
                        "event": "earnings",
                    },
                }
            )

            pending = await db.list_strategy_trade_predictions(
                strategy_id="strategy-p1",
                target_trading_date_lte="2026-06-10",
                pending_for_outcome=True,
                exclude_outcome_score_version="trade_prediction_score_daily_v1",
            )
            status = await db.summarize_strategy_trade_predictions(strategy_id="strategy-p1")
            matrix = await db.aggregate_trade_prediction_matrix(strategy_id="strategy-p1")

            assert [row["prediction_id"] for row in pending] == ["prediction-p1-b"]
            assert status["prediction_count"] == 3
            assert status["outcome_count"] == 1
            assert status["sample_n"] == 1
            assert status["pending_count"] == 2
            assert status["score_version_counts"] == {"trade_prediction_score_daily_v1": 1}
            family_rows = [row for row in matrix["rows"] if row["dimension"] == "family"]
            assert family_rows[0]["value"] == "breakout"
            assert family_rows[0]["sample_n"] == 1
            assert family_rows[0]["score_avg"] == 0.76
            assert family_rows[0]["direction_hit_rate"] == 1.0
        finally:
            await db.close()

    asyncio.run(_run())
