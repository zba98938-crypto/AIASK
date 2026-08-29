from __future__ import annotations

import asyncio
from datetime import date, timedelta

from akshare_mcp.services import get_artifact_async
from akshare_mcp.services.factor_candidate_storage import (
    get_factor_candidate_record_async,
    save_factor_candidate_record_async,
)
from akshare_mcp.services.factor_validation_bootstrap import run_factor_validation_bootstrap
from akshare_mcp.storage import close_db, get_db
from akshare_mcp.tools.managers._data_sync_manager_support_sync import (
    _sync_factor_validation_bootstrap_now,
)


def _candidate(name: str = "bootstrap_momentum") -> dict:
    return {
        "name": name,
        "family": "momentum",
        "hypothesis": "Local validation bootstrap test candidate.",
        "inputs": ["momentum_20d", "momentum_60d"],
        "expression_dsl": "zscore(momentum_20d, 20) + zscore(momentum_60d, 20)",
        "expected_holding_period": 10,
        "expected_regime": ["test"],
    }


async def _seed_market_data(db, codes: list[str], *, days: int = 190) -> None:
    start = date(2025, 1, 1)
    async with db.acquire() as conn:
        for idx, code in enumerate(codes):
            await conn.execute(
                """
                INSERT INTO stocks (stock_code, stock_name, market)
                VALUES ($1, $2, $3)
                ON CONFLICT (stock_code) DO UPDATE SET
                    stock_name = EXCLUDED.stock_name,
                    market = EXCLUDED.market
                """,
                code,
                f"Test Stock {idx}",
                "A",
            )
    for idx, code in enumerate(codes):
        rows = []
        base = 10.0 + idx
        for day in range(days):
            current = start + timedelta(days=day)
            close = base + day * (0.02 + idx * 0.003) + ((day % 7) - 3) * 0.015
            open_ = close * (0.998 + idx * 0.0005)
            rows.append(
                {
                    "date": current.isoformat(),
                    "open": round(open_, 4),
                    "high": round(max(open_, close) * 1.01, 4),
                    "low": round(min(open_, close) * 0.99, 4),
                    "close": round(close, 4),
                    "volume": 100000 + idx * 1000 + day * 10,
                    "amount": round((100000 + idx * 1000 + day * 10) * close, 2),
                    "turnover": 1.0 + idx * 0.1,
                }
            )
        await db.save_klines(code, rows)


def test_factor_validation_bootstrap_persists_factor_outputs(tmp_path, monkeypatch):
    db_path = str(tmp_path / "factor_validation.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            codes = ["000001", "000002", "000003", "000004"]
            await _seed_market_data(db, codes)
            artifact_id = "factor_validation_candidate_persist"
            await save_factor_candidate_record_async(
                {
                    "artifact_id": artifact_id,
                    "status": "review",
                    "codes": codes,
                    "candidate": _candidate(),
                    "tags": ["requires_validation"],
                    "rating": {"grade": "C", "recommendation": "review"},
                },
                artifact_id=artifact_id,
            )

            result = await run_factor_validation_bootstrap(
                db,
                candidate_ids=[artifact_id],
                max_candidates=1,
                horizon_days=5,
                max_dates=20,
                lookback_bars=120,
                min_cross_section=3,
                universe_limit=4,
                promote=False,
                resume=False,
            )
            assert result["status"] == "completed"
            assert result["processed"] == 1
            assert result["saved"] == 1
            assert result["factor_value_rows"] > 0
            assert result["ic_history_rows"] > 0

            factor_key = f"factor_candidate:{artifact_id}"
            async with db.acquire() as conn:
                factor_rows = await conn.fetchval(
                    "SELECT COUNT(*) FROM factor_values WHERE factor_name = $1",
                    factor_key,
                )
                ic_rows = await conn.fetchval(
                    "SELECT COUNT(*) FROM factor_ic_history WHERE factor_name = $1 AND period = $2",
                    factor_key,
                    "5",
                )
            assert factor_rows == result["factor_value_rows"]
            assert ic_rows == result["ic_history_rows"]

            artifact = await get_artifact_async("factor_validation_bootstrap_factor_validation_candidate_persist_h5")
            assert artifact is not None
            payload = artifact["payload"]
            assert artifact["strategy"] == "quant_factor_candidate_validation"
            assert payload["persisted_outputs"]["factor_key"] == factor_key

            record = await get_factor_candidate_record_async(artifact_id)
            assert record["status"] == "review"
            assert record["latest_validation"]["artifact_id"] == artifact["artifact_id"]
            assert "local_validation" in record["tags"]

            repeat = await run_factor_validation_bootstrap(
                db,
                candidate_ids=[artifact_id],
                max_candidates=1,
                horizon_days=5,
                max_dates=20,
                lookback_bars=120,
                min_cross_section=3,
                universe_limit=4,
                promote=False,
                resume=False,
            )
            async with db.acquire() as conn:
                repeated_factor_rows = await conn.fetchval(
                    "SELECT COUNT(*) FROM factor_values WHERE factor_name = $1",
                    factor_key,
                )
                repeated_ic_rows = await conn.fetchval(
                    "SELECT COUNT(*) FROM factor_ic_history WHERE factor_name = $1 AND period = $2",
                    factor_key,
                    "5",
                )
            assert repeat["factor_value_rows"] == result["factor_value_rows"]
            assert repeated_factor_rows == factor_rows
            assert repeated_ic_rows == ic_rows
        finally:
            await close_db()

    asyncio.run(_run())


def test_factor_validation_bootstrap_strict_promotion_gate(tmp_path, monkeypatch):
    db_path = str(tmp_path / "factor_validation_gate.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        import akshare_mcp.services.factor_validation_bootstrap as bootstrap_mod

        db = get_db()
        try:
            await db.initialize()
            codes = ["000101", "000102", "000103"]
            await _seed_market_data(db, codes, days=150)
            pass_id = "factor_validation_candidate_pass"
            block_id = "factor_validation_candidate_block"
            for artifact_id in (pass_id, block_id):
                await save_factor_candidate_record_async(
                    {
                        "artifact_id": artifact_id,
                        "status": "review",
                        "codes": codes,
                        "candidate": _candidate(artifact_id),
                        "tags": ["requires_validation"],
                    },
                    artifact_id=artifact_id,
                )

            async def fake_pipeline(_db, candidate, **kwargs):
                name = str(candidate.get("name") or "")
                blocked = "block" in name
                return {
                    "success": True,
                    "stage": "validated",
                    "compiled": {"candidate": candidate},
                    "metrics": {"rank_ic_mean": 0.08, "sample_dates": 12},
                    "coverage": {"processed_codes": len(kwargs.get("codes") or [])},
                    "cross_section_dates": [{"date": "2026-01-02", "sample_size": 3, "normal_ic": 0.1, "rank_ic": 0.2}],
                    "latest_snapshot": {},
                    "lookahead_audit": {"available": True, "risk_level": "low"},
                    "multiple_testing": {"available": True, "risk_level": "low"},
                    "oos_validation": {"available": not blocked, "rating": {"grade": "A"}},
                    "robustness": {"available": True, "grade": "strong"},
                    "similarity": {"available": True},
                    "turnover": {"available": True},
                    "cost_capacity": {"available": True},
                    "rating": {
                        "grade": "A",
                        "recommendation": "promote",
                        "governance": {
                            "registry_stage": "governed",
                            "admission_blocked": False,
                            "admission_block_reasons": [],
                        },
                    },
                    "validation_report": {},
                    "factor_validation_report": {},
                    "warnings": [],
                    "persisted_outputs": {
                        "enabled": True,
                        "factor_key": kwargs.get("factor_key"),
                        "factor_value_rows": 3,
                        "ic_history_rows": 1,
                        "errors": [],
                    },
                    "source_chain": ["test.fake_pipeline"],
                }

            monkeypatch.setattr(bootstrap_mod, "validate_factor_candidate_pipeline", fake_pipeline)
            result = await bootstrap_mod.run_factor_validation_bootstrap(
                db,
                candidate_ids=[pass_id, block_id],
                max_candidates=2,
                horizon_days=10,
                max_dates=20,
                lookback_bars=100,
                min_cross_section=3,
                universe_limit=3,
                promote=True,
                resume=False,
            )
            assert result["processed"] == 2
            assert result["promoted"] == 1
            passed = await get_factor_candidate_record_async(pass_id)
            blocked = await get_factor_candidate_record_async(block_id)
            assert passed["status"] == "success"
            assert passed["memory_flags"]["active_pool_eligible"] is True
            assert blocked["status"] == "review"
            assert "oos_validation_unavailable" in blocked["promotion_block_reasons"]
            assert blocked["memory_flags"]["active_pool_eligible"] is False
        finally:
            await close_db()

    asyncio.run(_run())


def test_data_sync_factor_validation_bootstrap_dry_run(tmp_path, monkeypatch):
    db_path = str(tmp_path / "factor_validation_sync.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            artifact_id = "factor_validation_candidate_dry_run"
            await save_factor_candidate_record_async(
                {
                    "artifact_id": artifact_id,
                    "status": "review",
                    "candidate": _candidate("dry_run_candidate"),
                    "tags": ["requires_validation"],
                },
                artifact_id=artifact_id,
            )
            result = await _sync_factor_validation_bootstrap_now(
                {
                    "dry_run": True,
                    "candidate_ids": [artifact_id],
                    "max_candidates": 1,
                    "universe_limit": 3,
                }
            )
            assert result["success"] == 1
            assert result["bootstrap"]["status"] == "planned"
            assert result["bootstrap"]["candidate_count"] == 1
            assert result["bootstrap"]["candidate_plan"][0]["artifact_id"] == artifact_id
        finally:
            await close_db()

    asyncio.run(_run())
