"""MCP_FIX_PLAN 第二批回归测试（FIX-13~16）。

- FIX-16 (F-N27-1): parse_selection_query "连续N天上涨" 不再同时产出 upn+downn
- FIX-14 (F-N26-2): industry_chain SQL 不再使用裸 code 列（静态扫描）
- FIX-15 (F-N38-2): data_sync_manager 不再使用 PostgreSQL array_to_string（静态扫描）
- FIX-13 (F-N27-6): screener_manager run_strategy 不再用 criteria= 关键字调用（静态扫描）
"""

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "akshare_mcp"


# ── FIX-16: parse_selection_query 连续上涨/下跌方向 ──────────────────

def _parse_consecutive(query: str):
    """复现 query_parser 的连续涨跌解析（与源码同逻辑），返回命中的 condition id 集合。"""
    from akshare_mcp.tools.semantic.query_parser import parse_selection_query as _p
    res = _p(query)
    data = res.get("data", res)
    tech = data.get("technical_conditions", []) or []
    return {c.get("id") for c in tech if isinstance(c, dict)}


def test_fix16_consecutive_up_only():
    ids = _parse_consecutive("市值大于100亿且PB小于2且连续3天上涨")
    assert "upn" in ids
    assert "downn" not in ids, "连续上涨不应产出 downn（矛盾条件）"


def test_fix16_consecutive_down_only():
    ids = _parse_consecutive("连续3天下跌的股票")
    assert "downn" in ids
    assert "upn" not in ids


def test_fix16_consecutive_up_default_days():
    ids = _parse_consecutive("连涨的股票")
    assert "upn" in ids
    assert "downn" not in ids


# ── 静态扫描：确保不回归到错误的 SQL / 调用形式 ──────────────────────

def test_fix14_industry_chain_no_bare_code_column():
    src = (_SRC / "tools" / "managers" / "industry_chain_manager.py").read_text(encoding="utf-8")
    # 不应再出现 "FROM stocks WHERE code =" 或 "SELECT code," 这类裸 code 列用法
    assert "WHERE code = $1" not in src
    assert "WHERE code != $2" not in src
    assert re.search(r"SELECT\s+code\s*,\s*stock_name\s+FROM\s+stocks", src) is None
    # 应使用 stock_code（或 stock_code AS code）
    assert "stock_code" in src


def test_fix15_data_sync_no_postgres_array_to_string():
    src = (_SRC / "tools" / "managers" / "data_sync_manager.py").read_text(encoding="utf-8")
    assert "array_to_string" not in src, "不应使用 PostgreSQL 专有函数 array_to_string（SQLite 不支持）"


def test_fix13_screener_run_strategy_no_criteria_kwarg():
    src = (_SRC / "tools" / "managers" / "screener_manager.py").read_text(encoding="utf-8")
    # run_strategy 分支不应再用 criteria= 关键字调用 screener_manager（签名无此参数）
    assert "action='screen',\n                            criteria=" not in src
    assert "action='screen',\n                    criteria=" not in src
    # 应改为 kwargs={'criteria': ...}
    assert "kwargs={'criteria':" in src
