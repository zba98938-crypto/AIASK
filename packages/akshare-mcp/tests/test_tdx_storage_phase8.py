"""Phase 8 — TDX 表 schema migration + 写读 round-trip。"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "tdx_phase8.sqlite3"
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("TDX_LOCAL_ONLY", "1")
    yield db_path


def test_phase_8_tables_created(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tdx_%'"
            )
            names = {r["name"] for r in rows}
        await close_db()
        return names

    names = asyncio.run(_run())
    expected = {
        "tdx_financial_pro", "tdx_stock_extra", "tdx_consensus",
        "tdx_gpjy_daily", "tdx_bkjy_daily", "tdx_scjy_daily",
        "tdx_kzz_basic", "tdx_relation",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


def test_save_tdx_financial_roundtrip(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        res = await db.save_tdx_financial("600519.SH", [
            {"report_date": "20240630", "announce_date": "20240730",
             "fn_code": "FN1", "value": 33.5},
            {"report_date": "20240630", "announce_date": "20240730",
             "fn_code": "FN6", "value": 18.2},
        ])
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT fn_code, value FROM tdx_financial_pro WHERE code = $1 ORDER BY fn_code",
                "600519.SH",
            )
        await db.save_tdx_financial("600519.SH", [
            {"report_date": "20240630", "fn_code": "FN1", "value": 34.0},
        ])
        async with db.acquire() as conn:
            v = await conn.fetchval(
                "SELECT value FROM tdx_financial_pro WHERE code=$1 AND fn_code='FN1'",
                "600519.SH",
            )
        await close_db()
        return res, [dict(r) for r in rows], v

    res, rows, updated_v = asyncio.run(_run())
    assert res["accepted"] == 2
    assert len(rows) == 2
    assert rows[0]["fn_code"] == "FN1"
    assert abs(rows[0]["value"] - 33.5) < 1e-6
    assert abs(updated_v - 34.0) < 1e-6


def test_save_tdx_stock_extra_roundtrip(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        payload = {
            "HqDate": "20260518",
            "StaticPE_TTM": "20.18", "PB_MRQ": "6.12", "DYRatio": "3.90",
            "fHSL": "0.40", "fLianB": "0.89",
            "Zsz": "16567.53", "Ltsz": "16567.53",
            "ZTPrice": "1466.25", "DTPrice": "1199.65",
            "EverZTCount": "0", "ConZAFDateNum": "1",
            "FCAmo": "0.00", "MA5Value": "1339.36",
            "TPFlag": "0",
            "ReportDate": "20251031",
            "ZTDate_Recent": "20241008",
        }
        await db.save_tdx_stock_extra("600519.SH", payload)
        extras = await db.get_tdx_stock_extra("600519.SH", limit=1)
        await close_db()
        return extras

    extras = asyncio.run(_run())
    assert extras and extras[0]["pe_ttm"] == 20.18
    assert extras[0]["up_limit"] == 1466.25
    assert extras[0]["report_date"] == "2025-10-31"


def test_save_tdx_consensus_roundtrip(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        go = {"GO1": "31.39", "GO3": "1790.09", "GO5": "68.35", "GO20": "31.65",
              "GO29": "100", "GO35": "20251031"}
        await db.save_tdx_consensus("600519.SH", go, snapshot_date="2026-05-18")
        snap = await db.get_tdx_consensus("600519.SH")
        await close_db()
        return snap

    snap = asyncio.run(_run())
    assert snap is not None
    assert snap["target_price"] == 1790.09
    assert snap["roe_t"] == 31.65
    assert snap["inst_holding_count"] == 100
    assert snap["forecast_report_date"] == "2025-10-31"


def test_save_tdx_gpjy_daily_roundtrip(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        rows = [
            {"trade_date": "2026-05-18", "gp_code": "GP25", "value_a": 915.0, "value_b": 0.0},
            {"trade_date": "2026-05-18", "gp_code": "GP21", "value_a": 3.9, "value_b": None},
        ]
        await db.save_tdx_gpjy_daily("600519.SH", rows)
        async with db.acquire() as conn:
            cnt = await conn.fetchval(
                "SELECT count(*) FROM tdx_gpjy_daily WHERE code=$1", "600519.SH",
            )
        await close_db()
        return cnt

    assert asyncio.run(_run()) == 2


def test_save_tdx_bkjy_and_scjy(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        await db.save_tdx_bkjy_daily("880660.SH", [
            {"trade_date": "2026-05-18", "bk_code": "BK9", "value_a": 21.0, "value_b": 12.0},
            {"trade_date": "2026-05-18", "bk_code": "BK17", "value_a": 24484.05, "value_b": 0.0},
        ])
        await db.save_tdx_scjy_daily([
            {"trade_date": "2026-05-18", "sc_code": "SC25", "value_a": 100.08, "value_b": 0.0},
            {"trade_date": "2026-05-18", "sc_code": "SC02", "value_a": 0.0, "value_b": 0.0},
        ])
        async with db.acquire() as conn:
            bk_cnt = await conn.fetchval("SELECT count(*) FROM tdx_bkjy_daily")
            sc_cnt = await conn.fetchval("SELECT count(*) FROM tdx_scjy_daily")
        await close_db()
        return bk_cnt, sc_cnt

    bk_cnt, sc_cnt = asyncio.run(_run())
    assert bk_cnt == 2
    assert sc_cnt == 2


def test_market_quality_gate_blocks_stale_north_fund(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        old_date = date.today() - timedelta(days=30)
        async with db.acquire() as conn:
            for offset in range(5):
                trade_date = old_date - timedelta(days=offset)
                await conn.execute(
                    """
                    INSERT INTO north_fund_flow (
                        trade_date, north_money, net_amount, source, source_priority
                    ) VALUES ($1, $2, $2, 'akshare.stock_hsgt_hist_em', 'external_gap_fill_free')
                    ON CONFLICT (trade_date) DO UPDATE SET
                        north_money = EXCLUDED.north_money,
                        net_amount = EXCLUDED.net_amount,
                        source = EXCLUDED.source,
                        source_priority = EXCLUDED.source_priority
                    """,
                    trade_date,
                    float(offset + 1),
                )
        await db.save_tdx_data_completeness(
            "north_fund_flow",
            "stale",
            as_of_date=old_date,
            row_count=5,
            detail={"source": "akshare.stock_hsgt_hist_em"},
        )
        summary = await db.get_recent_north_fund_summary(days=3, sample_limit=5)
        await close_db()
        return summary

    summary = asyncio.run(_run())
    assert summary["blocked"] is True
    assert summary["degraded"] is True
    assert summary["hard_fact_eligible"] is False
    assert summary["sample_count"] == 0
    assert summary["raw_sample_count"] == 5
    assert summary["total_net"] is None
    assert "status:stale" in summary["block_reasons"]


def test_margin_detail_market_wide_gate_requires_sh_and_sz_coverage(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        today = date.today()
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO margin_detail (
                    trade_date, ts_code, rzye, rqye, rzmre, rqyl, rzche, rqchl,
                    rqmcl, rzrqye, source, source_priority
                ) VALUES ($1, '600000.SH', 100.0, 2.0, 10.0, 1.0, 3.0, 0.5,
                          4.0, 102.0, 'akshare.stock_margin_detail_sse',
                          'external_gap_fill_free')
                ON CONFLICT (trade_date, ts_code) DO UPDATE SET
                    rzye = EXCLUDED.rzye,
                    rqye = EXCLUDED.rqye,
                    rzmre = EXCLUDED.rzmre,
                    rqyl = EXCLUDED.rqyl,
                    rzche = EXCLUDED.rzche,
                    rqchl = EXCLUDED.rqchl,
                    rqmcl = EXCLUDED.rqmcl,
                    rzrqye = EXCLUDED.rzrqye,
                    source = EXCLUDED.source,
                    source_priority = EXCLUDED.source_priority
                """,
                today,
            )
        await db.save_tdx_data_completeness(
            "margin_detail",
            "ok",
            as_of_date=today,
            row_count=1,
            detail={"source": "akshare.stock_margin_detail_sse"},
        )
        market_wide = await db.get_margin_detail_latest(limit=10)
        sh_specific = await db.get_margin_detail_latest(limit=10, ts_code="600000.SH")
        ranking = await db.get_margin_ranking(top_n=10)
        coverage = await db._get_margin_detail_suffix_coverage()
        await close_db()
        return market_wide, sh_specific, ranking, coverage

    market_wide, sh_specific, ranking, coverage = asyncio.run(_run())
    assert market_wide == []
    assert ranking == []
    assert len(sh_specific) == 1
    assert sh_specific[0]["ts_code"] == "600000.SH"
    assert coverage["blocked"] is True
    assert coverage["missing_suffixes"] == ["SZ"]


def test_save_tdx_kzz_and_relation(tmp_db):
    from akshare_mcp.storage import get_db, close_db

    async def _run():
        db = get_db()
        await db.save_tdx_kzz({
            "kzz_code": "123054", "stock_code": "300608",
            "convert_price": 9.88, "force_redeem_price": 12.84,
            "putback_price": 6.92, "convert_date": "2020-12-16",
            "end_date": "2026-05-29", "kzz_score": "AA-",
        })
        kz = await db.get_tdx_kzz("123054")
        rel_rows = [
            {"block_code": "881130.SH", "block_name": "白酒",
             "block_type": "行业", "gp_num": 37},
            {"block_code": "880229.SH", "block_name": "贵州板块",
             "block_type": "地区", "gp_num": 34},
        ]
        await db.save_tdx_relation("600519.SH", rel_rows)
        found = await db.get_tdx_relation("600519.SH")
        # 二次写应 replace
        await db.save_tdx_relation("600519.SH", [
            {"block_code": "881130.SH", "block_name": "白酒",
             "block_type": "行业", "gp_num": 38},
        ])
        found_after = await db.get_tdx_relation("600519.SH")
        await close_db()
        return kz, found, found_after

    kz, found, found_after = asyncio.run(_run())
    assert kz["convert_price"] == 9.88
    assert kz["end_date"] == "2026-05-29"
    assert len(found) == 2
    assert {r["block_type"] for r in found} == {"行业", "地区"}
    assert len(found_after) == 1
    assert found_after[0]["gp_num"] == 38


def test_record_tdx_data_completeness_includes_sector_and_relation_keys(tmp_db):
    from akshare_mcp.services.tdx_sync_service import TdxSyncService
    from akshare_mcp.storage import close_db, get_db

    async def _run():
        db = get_db()
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO market_blocks (block_code, block_name, block_type, updated_at)
                VALUES ('880001', 'Demo Concept', 'tdx', '2026-05-24 09:30:00')
                ON CONFLICT(block_code, block_type) DO UPDATE SET
                    block_name = EXCLUDED.block_name,
                    updated_at = EXCLUDED.updated_at
                """
            )
            await conn.execute(
                """
                INSERT INTO block_stocks (block_code, stock_code, stock_name, updated_at)
                VALUES ('880001', '600100', 'Demo Stock', '2026-05-24 09:31:00')
                ON CONFLICT(block_code, stock_code) DO UPDATE SET
                    stock_name = EXCLUDED.stock_name,
                    updated_at = EXCLUDED.updated_at
                """
            )
        await db.save_tdx_relation("600100", [
            {
                "block_code": "880001",
                "block_name": "Demo Concept",
                "block_type": "概念",
                "gp_num": 1,
            }
        ])
        result = await TdxSyncService(universe=[])._record_tdx_data_completeness(db)
        sector = await db.get_tdx_data_completeness("sync_sector_basic")
        relation = await db.get_tdx_data_completeness("sync_relation")
        await close_db()
        return result, sector, relation

    result, sector, relation = asyncio.run(_run())
    assert result["updated"] >= 2
    assert sector["status"] == "ok"
    assert sector["row_count"] == 2
    assert {item["table"] for item in sector["detail"]["tables"]} == {
        "market_blocks",
        "block_stocks",
    }
    assert relation["status"] == "ok"
    assert relation["row_count"] == 1
    assert relation["detail"]["table"] == "tdx_relation"


def test_sync_sector_basic_writes_completeness_directly(tmp_db, monkeypatch):
    from akshare_mcp.data_source import data_source
    from akshare_mcp.services.tdx_sync_service import TdxSyncService
    from akshare_mcp.storage import close_db, get_db

    monkeypatch.setattr(
        data_source,
        "get_sector_list",
        lambda list_type=1: [{"block_code": "880001", "block_name": "Demo Concept"}],
    )
    monkeypatch.setattr(
        data_source,
        "get_stock_list_in_sector",
        lambda block_code, block_type=0, list_type=0: ["600100.SH", {"Code": "000001.SZ"}],
    )

    async def _run():
        db = get_db()
        result = await TdxSyncService(universe=[])._sync_sector_basic(db)
        completeness = await db.get_tdx_data_completeness("sync_sector_basic")
        await close_db()
        return result, completeness

    result, completeness = asyncio.run(_run())
    assert result["sectors"] == 1
    assert result["members"] == 2
    assert result["completeness"]["data_key"] == "sync_sector_basic"
    assert completeness["status"] == "ok"
    assert completeness["row_count"] == 3
    assert {item["table"] for item in completeness["detail"]["tables"]} == {
        "market_blocks",
        "block_stocks",
    }


def test_sync_relation_writes_completeness_directly(tmp_db, monkeypatch):
    from akshare_mcp.data_source import data_source
    from akshare_mcp.services.tdx_sync_service import TdxSyncService
    from akshare_mcp.storage import close_db, get_db

    monkeypatch.setattr(
        data_source,
        "get_relation",
        lambda code: [
            {
                "block_code": "880001",
                "block_name": "Demo Concept",
                "block_type": "概念",
                "gp_num": 1,
            }
        ],
    )

    async def _run():
        db = get_db()
        service = TdxSyncService(universe=["600100.SH"])
        result = await service._sync_relation(db)
        completeness = await db.get_tdx_data_completeness("sync_relation")
        await close_db()
        return result, completeness

    result, completeness = asyncio.run(_run())
    assert result["updated"] == 1
    assert result["completeness"]["data_key"] == "sync_relation"
    assert completeness["status"] == "ok"
    assert completeness["row_count"] == 1
    assert completeness["detail"]["table"] == "tdx_relation"
