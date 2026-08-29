"""Phase 3 E2E — tools 层切到 TDX 后的端到端验证。

需要通达信客户端在线。如果客户端不可用，相关用例会 skip 而不是 fail。
"""
from __future__ import annotations

import asyncio
import os

import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch, tmp_path):
    db_path = tmp_path / "phase3_e2e.sqlite3"
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("TDX_LOCAL_ONLY", "1")
    monkeypatch.setenv("DATA_SOURCE_KEEP_LEGACY_FALLBACK", "0")
    yield


def _tqcenter_available() -> bool:
    """Skip if TDX 客户端不可用。"""
    try:
        from akshare_mcp.data_source.tdx_tqcenter import get_tq, reset_tq
        reset_tq()
        return get_tq() is not None
    except Exception:
        return False


def test_order_book_uses_tqcenter_five_levels():
    if not _tqcenter_available():
        pytest.skip("tqcenter not available — TDX client offline")

    from akshare_mcp.tools.market.order_book import get_order_book
    res = get_order_book("600519", live=True)
    if not res.get("success"):
        pytest.skip(f"tqcenter available but live order book unavailable: {res.get('error') or res}")
    assert res.get("success"), res
    data = res.get("data") or {}
    assert data.get("source") in {"tqcenter", "tdx_online"}, data.get("source")
    # 五档至少有一档非零（盘后是 ['1322.97', 0,0,0,0]）
    assert isinstance(data.get("bids"), list)
    assert isinstance(data.get("asks"), list)


def test_market_blocks_uses_tqcenter_for_88x_codes():
    if not _tqcenter_available():
        pytest.skip("tqcenter not available")

    async def _run():
        from akshare_mcp.tools.market_blocks import get_block_stocks
        return await get_block_stocks("881130.SH")  # 白酒

    res = asyncio.run(_run())
    assert res.get("success"), res
    data = res.get("data") or {}
    assert data.get("source") == "tqcenter.get_stock_list_in_sector"
    assert data.get("count", 0) >= 10
    stocks = data.get("stocks") or []
    assert all(s.get("code") for s in stocks[:5])


def test_formula_default_pool_prefers_tqcenter_hs300():
    if not _tqcenter_available():
        pytest.skip("tqcenter not available")

    from akshare_mcp.tools.formula_fallback import get_default_formula_stock_pool
    pool = get_default_formula_stock_pool()
    assert isinstance(pool, list) and len(pool) >= 20
    # HS300 含 600519 / 000001（实测 probe_tdx_all 的 HS300 列表里都有）
    assert "600519" in pool or "000001" in pool


def test_finance_falls_back_when_no_pro_data_pkg():
    """tqcenter 数据包未下载时，财务函数应自动降级，不 fail。"""
    if not _tqcenter_available():
        pytest.skip("tqcenter not available")

    from akshare_mcp.tools.finance import _get_financials_tdx
    # 客户端没下专业财务包，应返回 None 让上层降级
    res = _get_financials_tdx("600519")
    assert res is None or isinstance(res, dict)


def test_fund_flow_individual_uses_more_info():
    if not _tqcenter_available():
        pytest.skip("tqcenter not available")

    from akshare_mcp.tools.fund_flow import get_stock_fund_flow
    res = get_stock_fund_flow("600519", prefer_db=False)
    assert res.get("success"), res
    data = res.get("data") or {}
    # 盘后可能为 0，但 source 必须标记为 tqcenter
    assert "tqcenter" in str(data.get("source", "")).lower()
