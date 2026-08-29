"""TDX 路由测试 — 验证 data_source 在各种条件下选择正确的数据源。

不需要客户端，全部用 monkeypatch 模拟 tqcenter / tdx_local。
"""
from __future__ import annotations

import os
import sys

import pytest


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_data_source(monkeypatch):
    """
    每个测试用一个干净的 DataSourceManager 实例。

    DataSourceManager 是 __new__-单例，不能简单 reload；这里通过把
    `_instance = None` 复位 + 立即重建 + 替换 module-global ``data_source``。
    """
    # 强制 TDX-only：不调用 Tushare init，不读取真实 .env
    monkeypatch.setenv("TDX_LOCAL_ONLY", "1")
    monkeypatch.setenv("DATA_SOURCE_KEEP_LEGACY_FALLBACK", "0")
    monkeypatch.setenv("TDX_TQCENTER_REQUIRED", "0")

    # 让相关模块复位
    import akshare_mcp.data_source as ds_init_mod
    monkeypatch.setattr(ds_init_mod.DataSourceManager, "_instance", None, raising=False)
    new_ds = ds_init_mod.DataSourceManager()
    monkeypatch.setattr(ds_init_mod, "data_source", new_ds, raising=False)
    return new_ds


# ---------------------------------------------------------------------------
# K 线优先级 — tqcenter > tdx_local > 空
# ---------------------------------------------------------------------------

def test_kline_uses_tqcenter_when_available(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import quotes as quotes_mod

    expected = [{
        "date": "2026-05-18", "open": 100.0, "high": 102.0, "low": 99.0,
        "close": 101.5, "volume": 12345, "amount": 6789.0, "source": "tqcenter",
    }]
    monkeypatch.setattr(quotes_mod._tqcenter, "get_kline",
                         lambda code, period="daily", limit=100: list(expected))
    # tdx_local 永远不应被调用——给它装个会爆炸的 mock
    def _local_should_not_be_called(*a, **kw):
        raise AssertionError("tdx_local should not be called when tqcenter succeeded")
    fake_local = type("X", (), {"get_kline": staticmethod(_local_should_not_be_called)})()
    monkeypatch.setattr(quotes_mod, "_get_tdx_local", lambda: fake_local)

    rows = fresh_data_source.get_kline("600519", "daily", 5)
    assert rows == expected
    assert rows[0]["source"] == "tqcenter"


def test_kline_falls_back_to_tdx_local_when_tqcenter_returns_empty(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import quotes as quotes_mod

    monkeypatch.setattr(quotes_mod._tqcenter, "get_kline",
                         lambda code, period="daily", limit=100: [])

    local_rows = [{
        "date": "2026-05-18", "open": 100.0, "high": 101.0, "low": 99.0,
        "close": 100.5, "volume": 1, "amount": 0.0, "source": "tdx_local",
    }]
    fake_local = type("X", (), {"get_kline": staticmethod(lambda code, period="daily", limit=100: list(local_rows))})()
    monkeypatch.setattr(quotes_mod, "_get_tdx_local", lambda: fake_local)

    rows = fresh_data_source.get_kline("600519", "daily", 5)
    assert rows == local_rows
    assert rows[0]["source"] == "tdx_local"


def test_kline_returns_empty_when_both_fail_and_legacy_disabled(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import quotes as quotes_mod

    monkeypatch.setattr(quotes_mod._tqcenter, "get_kline",
                         lambda code, period="daily", limit=100: [])
    fake_local = type("X", (), {"get_kline": staticmethod(lambda code, period="daily", limit=100: [])})()
    monkeypatch.setattr(quotes_mod, "_get_tdx_local", lambda: fake_local)

    rows = fresh_data_source.get_kline("600519", "daily", 5)
    assert rows == []


def test_kline_tqcenter_exception_falls_back(fresh_data_source, monkeypatch):
    """tqcenter 抛异常时降级到 tdx_local，不是冒泡。"""
    from akshare_mcp.data_source import quotes as quotes_mod

    def _boom(*a, **kw):
        raise RuntimeError("tqcenter timeout")
    monkeypatch.setattr(quotes_mod._tqcenter, "get_kline", _boom)

    local_rows = [{"date": "2026-05-18", "close": 100.0, "source": "tdx_local"}]
    fake_local = type("X", (), {"get_kline": staticmethod(lambda *a, **kw: list(local_rows))})()
    monkeypatch.setattr(quotes_mod, "_get_tdx_local", lambda: fake_local)

    rows = fresh_data_source.get_kline("600519", "daily", 5)
    assert rows[0]["source"] == "tdx_local"


# ---------------------------------------------------------------------------
# Realtime quote — tqcenter 必须包含 88 字段拼接的关键 key
# ---------------------------------------------------------------------------

def test_realtime_quote_uses_tqcenter_with_full_field_set(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import quotes as quotes_mod

    expected = {
        "code": "600519",
        "name": "贵州茅台",
        "price": 1323.0,
        "preClose": 1332.95,
        "changePercent": -0.75,
        "open": 1336.0,
        "high": 1342.68,
        "low": 1319.61,
        "volume": 49660,
        "amount": 659498.38,
        "turnoverRate": 0.4,
        "pe_ttm": 20.18,
        "pb": 6.12,
        "market_cap": 16567.53,
        "up_limit": 1466.25,
        "down_limit": 1199.65,
        "bid1": 1322.97, "bid2": None, "bid3": None, "bid4": None, "bid5": None,
        "ask1": 1323.0, "ask2": None, "ask3": None, "ask4": None, "ask5": None,
        "even_zt_count": 0,
        "source": "tqcenter",
    }
    monkeypatch.setattr(quotes_mod._tqcenter, "get_realtime_quote",
                         lambda code: dict(expected))

    q = fresh_data_source.get_realtime_quote("600519")
    assert q is not None
    assert q["source"] == "tqcenter"
    # 关键字段断言（覆盖 88 字段拼接的核心子集）
    for key in ["price", "preClose", "changePercent", "open", "high", "low",
                "volume", "amount", "turnoverRate", "pe_ttm", "pb",
                "market_cap", "up_limit", "down_limit", "bid1", "ask1"]:
        assert key in q, f"missing key {key}"
    assert q["price"] == 1323.0
    assert q["up_limit"] == 1466.25
    assert q["bid1"] == 1322.97


def test_realtime_quote_falls_back_to_tdx_local(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import quotes as quotes_mod

    monkeypatch.setattr(quotes_mod._tqcenter, "get_realtime_quote", lambda code: None)

    local_q = {"code": "600519", "name": "", "price": 1320.0,
               "preClose": 1330.0, "source": "tdx_local"}
    fake_local = type("X", (), {"get_realtime_quote": staticmethod(lambda code: dict(local_q))})()
    monkeypatch.setattr(quotes_mod, "_get_tdx_local", lambda: fake_local)

    q = fresh_data_source.get_realtime_quote("600519")
    assert q is not None
    assert q["source"] == "tdx_local"


def test_realtime_quote_returns_none_when_both_fail(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import quotes as quotes_mod

    monkeypatch.setattr(quotes_mod._tqcenter, "get_realtime_quote", lambda code: None)
    fake_local = type("X", (), {"get_realtime_quote": staticmethod(lambda code: None)})()
    monkeypatch.setattr(quotes_mod, "_get_tdx_local", lambda: fake_local)

    assert fresh_data_source.get_realtime_quote("600519") is None


# ---------------------------------------------------------------------------
# 12 个 Mixin 直通方法 - 简单存在性 + 转发性测试
# ---------------------------------------------------------------------------

def test_mixin_methods_exist(fresh_data_source):
    """Phase 1 新增的 12 个方法都挂在 manager 上。"""
    expected_methods = [
        "get_more_info", "get_relation", "get_divid_factors",
        "get_gp_one_data",
        "get_gpjy_value", "get_gpjy_value_by_date",
        "get_bkjy_value", "get_bkjy_value_by_date",
        "get_scjy_value", "get_scjy_value_by_date",
        "get_financial_data", "get_financial_data_by_date",
        "get_sector_list", "get_stock_list_in_sector", "get_tdx_stock_list",
        "formula_zb_batch", "formula_xg_batch", "download_tdx_file",
    ]
    for m in expected_methods:
        assert callable(getattr(fresh_data_source, m, None)), f"missing {m}"


def test_mixin_methods_proxy_to_tqcenter(fresh_data_source, monkeypatch):
    """Mixin 方法应该转发到 tdx_tqcenter 模块。"""
    import akshare_mcp.data_source as ds_init_mod

    calls = []
    def _spy(name):
        def _impl(*args, **kwargs):
            calls.append((name, args, kwargs))
            return {"_marker_": name}
        return _impl

    for fname in ["get_more_info", "get_relation", "get_gp_one_data",
                  "get_financial_data", "get_gpjy_value", "get_bkjy_value",
                  "get_scjy_value"]:
        monkeypatch.setattr(ds_init_mod._tqcenter, fname, _spy(fname))

    fresh_data_source.get_more_info("600519")
    fresh_data_source.get_relation("600519")
    fresh_data_source.get_gp_one_data(["600519"], ["GO1"])
    fresh_data_source.get_financial_data(["600519"], ["FN1"])
    fresh_data_source.get_gpjy_value(["600519"], ["GP01"])
    fresh_data_source.get_bkjy_value(["880001.SH"], ["BK5"])
    fresh_data_source.get_scjy_value(["SC01"])

    fnames = [c[0] for c in calls]
    assert fnames == [
        "get_more_info", "get_relation", "get_gp_one_data",
        "get_financial_data", "get_gpjy_value", "get_bkjy_value",
        "get_scjy_value",
    ]


# ---------------------------------------------------------------------------
# get_trading_dates / get_ipo_info / get_cb_info / get_gb_info 主路径
# ---------------------------------------------------------------------------

def test_trading_dates_uses_tqcenter(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import market_data as md_mod

    monkeypatch.setattr(md_mod._tqcenter, "get_trading_dates",
                         lambda **kw: ["20260512", "20260513", "20260514"])

    res = fresh_data_source.get_trading_dates("SH", count=3)
    assert res["success"] is True
    assert res["source"] == "tqcenter"
    assert res["data"] == ["20260512", "20260513", "20260514"]


def test_ipo_info_uses_tqcenter(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import market_data as md_mod

    monkeypatch.setattr(md_mod._tqcenter, "get_ipo_info",
                         lambda ipo_type, ipo_date: [
                             {"code": "688001", "name": "X", "sg_code": "787001",
                              "sg_date": "2026-05-18", "sg_price": 10.0,
                              "max_sg": 1.0, "pe_issue": 20.0,
                              "set_code": "1", "type": "stock"}])
    res = fresh_data_source.get_ipo_info(ipo_type=2, ipo_date=1)
    assert res["success"] is True
    assert res["source"] == "tqcenter"
    assert res["data"][0]["SGDate"] == "20260518"
    assert res["data"][0]["type"] == "stock"


def test_cb_info_uses_tqcenter(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import market_data as md_mod

    monkeypatch.setattr(md_mod._tqcenter, "get_kzz_info",
                         lambda code: {
                             "kzz_code": "123054", "stock_code": "300608",
                             "convert_price": 9.88, "remain_size_wan": 8966.14,
                             "force_redeem_price": 12.84, "putback_price": 6.92,
                             "convert_date": "2020-12-16", "end_date": "2026-05-29",
                             "kzz_score": "AA-", "stock_score": "AA-",
                             "convert_code": "123054"})
    res = fresh_data_source.get_cb_info("123054.SZ")
    assert res["success"] is True
    assert res["source"] == "tqcenter"
    assert res["data"]["ZGPrice"] == 9.88
    assert res["data"]["ForceRedeem"] == 12.84


def test_gb_info_uses_tqcenter(fresh_data_source, monkeypatch):
    from akshare_mcp.data_source import market_data as md_mod

    monkeypatch.setattr(md_mod._tqcenter, "get_gb_info",
                         lambda code, dates: [
                             {"date": "2024-01-01", "total_shares": 1.256e9,
                              "float_shares": 1.256e9}])
    res = fresh_data_source.get_gb_info("600519", date_list=["20240101"], count=1)
    assert res["success"] is True
    assert res["source"] == "tqcenter"
    assert res["data"][0]["Date"] == 20240101


# ---------------------------------------------------------------------------
# tdx_tqcenter._normalize_code 正则
# ---------------------------------------------------------------------------

def test_normalize_code_routing():
    from akshare_mcp.data_source.tdx_tqcenter import _normalize_code
    assert _normalize_code("600519") == "600519.SH"
    assert _normalize_code("000001") == "000001.SZ"
    assert _normalize_code("300001") == "300001.SZ"
    assert _normalize_code("688001") == "688001.SH"
    assert _normalize_code("920001") == "920001.BJ"  # 新北交所
    assert _normalize_code("510050") == "510050.SH"  # ETF
    assert _normalize_code("123039") == "123039.SZ"  # 深可转债
    assert _normalize_code("110001") == "110001.SH"  # 沪可转债（11 开头）
    # 已带后缀的不要破坏
    assert _normalize_code("600519.SH") == "600519.SH"
    assert _normalize_code("000001.SZ") == "000001.SZ"
