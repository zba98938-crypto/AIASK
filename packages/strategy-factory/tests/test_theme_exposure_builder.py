"""Tests for the Phase 6 v1 TDX-only theme exposure builder."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from strategy_factory.application.research.theme_exposure_builder import ThemeExposureBuilder


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.list_theme_nodes = AsyncMock(
        return_value=[
            {
                "theme_code": "upstream_oil_gas",
                "theme_name": "oil gas",
                "aliases": '["oil","gas"]',
                "industry_tags": '["oil exploration"]',
            }
        ]
    )
    db.list_stock_universe = AsyncMock(
        return_value=[
            {"code": "600028", "name": "China Oil Chem", "industry": "oil chemical", "list_status": "L"},
            {"code": "601857", "name": "China Oil", "industry": "oil exploration", "list_status": "L"},
            {"code": "600519", "name": "Kweichow Moutai", "industry": "liquor", "list_status": "L"},
        ]
    )
    db.list_industry_blocks = AsyncMock(
        return_value=[
            {"symbol": "601857", "industry_name": "oil exploration", "list_status": "L", "turnover_rate": 1.2},
            {"symbol": "600028", "industry_name": "oil chemical", "list_status": "L", "turnover_rate": 0.8},
        ]
    )
    db.list_company_concept_blocks = AsyncMock(
        return_value=[
            {
                "symbol": "601857",
                "block_code": "BK001",
                "block_name": "oil gas concept",
                "block_type": "concept",
                "member_count": 30,
                "turnover_rate": 1.2,
            }
        ]
    )
    db.list_company_mainbz = AsyncMock(return_value=[])
    db.bulk_upsert_theme_exposure = AsyncMock(return_value={"written": 2, "batch_count": 1, "skipped": 0})
    return db


@pytest.mark.asyncio
async def test_builder_basic(mock_db):
    builder = ThemeExposureBuilder(min_exposure=0.1)
    report = await builder.build(mock_db)

    assert report["status"] == "completed"
    assert report["source"] == "tdx_only_v1"
    assert report["theme_count"] == 1
    assert report["stock_count"] == 3
    assert report["rows_scanned"] == 3
    assert report["rows_written"] == 2
    assert report["batch_count"] == 1
    assert report["industry_coverage"] > 0
    assert report["concept_block_coverage"] > 0

    mock_db.bulk_upsert_theme_exposure.assert_awaited_once()
    rows = mock_db.bulk_upsert_theme_exposure.await_args.args[0]
    assert {row["symbol"] for row in rows} == {"600028", "601857"}
    assert all(row["mainbz_match_score"] == 0 for row in rows)
    assert all(row["evidence"]["source"] == "tdx_only_v1" for row in rows)
    mock_db.list_company_mainbz.assert_not_called()


@pytest.mark.asyncio
async def test_builder_uses_bulk_batch_size(mock_db):
    builder = ThemeExposureBuilder(min_exposure=0.1, batch_size=1000)
    await builder.build(mock_db)
    assert mock_db.bulk_upsert_theme_exposure.await_args.kwargs["batch_size"] == 1000


@pytest.mark.asyncio
async def test_builder_no_themes():
    db = MagicMock()
    db.list_theme_nodes = AsyncMock(return_value=[])
    builder = ThemeExposureBuilder()
    report = await builder.build(db)
    assert report["status"] == "skipped"
    assert report["reason"] == "no_active_theme_nodes"


@pytest.mark.asyncio
async def test_builder_no_stocks():
    db = MagicMock()
    db.list_theme_nodes = AsyncMock(return_value=[{"theme_code": "test", "theme_name": "Test"}])
    db.list_stock_universe = AsyncMock(return_value=[])
    db.list_company_mainbz = AsyncMock(return_value=[])
    builder = ThemeExposureBuilder()
    report = await builder.build(db)
    assert report["status"] == "skipped"
    assert report["reason"] == "no_stocks_in_universe"
    db.list_company_mainbz.assert_not_called()


@pytest.mark.asyncio
async def test_builder_skips_suspended_stock(mock_db):
    mock_db.list_stock_universe.return_value = [
        {"code": "601857", "name": "China Oil", "industry": "oil exploration", "list_status": "L", "tp_flag": "停牌"},
    ]
    mock_db.list_industry_blocks.return_value = [
        {"symbol": "601857", "industry_name": "oil exploration", "tp_flag": "停牌"},
    ]
    mock_db.list_company_concept_blocks.return_value = []
    mock_db.bulk_upsert_theme_exposure.return_value = {"written": 0, "batch_count": 0, "skipped": 0}

    report = await ThemeExposureBuilder(min_exposure=0.1).build(mock_db)
    assert report["skipped_low_liquidity"] == 1
    mock_db.bulk_upsert_theme_exposure.assert_awaited_once_with([], batch_size=1000)


def test_keyword_match():
    from strategy_factory.application.research.theme_exposure_builder import _keyword_match_score

    assert _keyword_match_score("China oil natural gas", ["oil", "natural gas"]) == 1.0
    assert _keyword_match_score("Kweichow Moutai", ["oil", "natural gas"]) == 0.0
    assert _keyword_match_score("", ["oil"]) == 0.0
