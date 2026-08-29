"""通达信 tqcenter SDK 数据源适配器。

本模块是项目获取金融数据的**主路径**。通过通达信量化平台 Python SDK
(tqcenter) 与本地运行的通达信客户端通信，覆盖行情、财务、交易、板块、
ETF/可转债 等数据维度。

设计与契约：
- 单例模式 + 线程安全。SDK 调用本身是同步的；调用方在异步路径需用
  ``asyncio.to_thread`` 包裹。
- 所有公开函数失败时返回空 (``None`` / ``[]`` / ``{}``)，不抛异常上溢。
  调用方依据空值判断是否需要降级。
- 输出字段命名与 ``data_source.quotes.py`` / ``market_data.py`` 现有
  契约保持一致：``date='YYYY-MM-DD'`` / ``open/high/low/close/volume/amount``
  / ``source='tqcenter'``。
- 数据归一化：tqcenter 大量字符串字段（含价格、量、PE/PB 等），统一通过
  ``_safe_float`` 转 float；交易日整数 ``YYYYMMDD`` 转 ``YYYY-MM-DD``。

实测的数据形态 (来源 scripts/tdx_probe/result*.json + probe_shapes.py)：
- ``get_market_data`` 返回 ``{field: DataFrame}``；columns 永远是 stock_code，
  index 永远是 ``pd.Timestamp``。单股 shape=(N,1)，多股 shape=(N,M)。
- ``get_market_snapshot`` 返回 26 字段 dict（字符串），含五档盘口数组。
- ``get_more_info`` 返回 88 字段 dict（字符串），含涨跌停价/PE/PB/换手/
  量比/总市值/封单/涨停天/最近大事日。
- ``get_relation`` 返回 list[dict]，每个 ``{BlockCode, BlockName, BlockType,
  GPNume}``；BlockType 取值 行业/地区/概念/风格/指数。
- ``get_divid_factors`` 返回 DataFrame，列 ``Type/Bonus/AllotPrice/
  ShareBonus/Allotment``，index 为 Timestamp。

环境变量：
- ``TDX_INSTALL_DIR``     通达信安装目录（默认 C:\\new_tdx_test）
- ``TDX_PYPLUGINS_PATH``  tqcenter.py 所在目录（默认 ${TDX_INSTALL_DIR}\\PYPlugins\\sys）
- ``TDX_TQCENTER_REQUIRED``  ``1`` 时客户端不可用直接 raise；``0`` (默认) 让
  调用方按空返回降级到 tdx_local
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import contextlib
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SDK 加载（线程安全单例）
# ---------------------------------------------------------------------------

_tq: Any = None
_tq_lock = threading.Lock()
_tq_initialized = False

from ._core import (
    get_tq,
    _normalize_code,
    _format_date,
    _safe_float,
    _safe_int,
    _call_with_retry,
    _to_tq_period,
)

def get_gp_one_data(codes: list[str], fields: list[str]) -> dict:
    """单个数据点查询：一致预期、解禁、机构持股、业绩预告/快报、派现/募资。

    fields 形如 ``["GO1","GO2","GO3","GO5",...]``。返回 ``{code: {field: value}}``，
    value 为字符串（保留 SDK 原貌；上层选择性转 float）。
    """
    tq = get_tq()
    if tq is None:
        return {}
    stock_list = [_normalize_code(c) for c in (codes or [])]
    if not stock_list or not fields:
        return {}
    try:
        data = _call_with_retry(tq.get_gp_one_data, stock_list=stock_list, field_list=list(fields))
    except Exception as exc:
        logger.warning("[TdxTQ] get_gp_one_data fail: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 10. 个股交易数据 (GP 字段) — 龙虎榜/融资融券/陆股通/大宗交易/涨跌停盘中
# ---------------------------------------------------------------------------

def get_gpjy_value(
    codes: list[str],
    fields: list[str],
    start_time: str = "",
    end_time: str = "",
) -> dict:
    tq = get_tq()
    if tq is None:
        return {}
    stock_list = [_normalize_code(c) for c in (codes or [])]
    if not stock_list or not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_gpjy_value,
            stock_list=stock_list,
            field_list=list(fields),
            start_time=start_time,
            end_time=end_time,
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_gpjy_value fail: %s", exc)
        return {}


def get_gpjy_value_by_date(
    codes: list[str],
    fields: list[str],
    year: int = 0,
    mmdd: int = 0,
) -> dict:
    tq = get_tq()
    if tq is None:
        return {}
    stock_list = [_normalize_code(c) for c in (codes or [])]
    if not stock_list or not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_gpjy_value_by_date,
            stock_list=stock_list,
            field_list=list(fields),
            year=int(year),
            mmdd=int(mmdd),
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_gpjy_value_by_date fail: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 11. 板块交易数据 (BK 字段)
# ---------------------------------------------------------------------------

def get_bkjy_value(
    blocks: list[str],
    fields: list[str],
    start_time: str = "",
    end_time: str = "",
) -> dict:
    tq = get_tq()
    if tq is None:
        return {}
    if not blocks or not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_bkjy_value,
            stock_list=list(blocks),
            field_list=list(fields),
            start_time=start_time,
            end_time=end_time,
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_bkjy_value fail: %s", exc)
        return {}


def get_bkjy_value_by_date(
    blocks: list[str],
    fields: list[str],
    year: int = 0,
    mmdd: int = 0,
) -> dict:
    tq = get_tq()
    if tq is None:
        return {}
    if not blocks or not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_bkjy_value_by_date,
            stock_list=list(blocks),
            field_list=list(fields),
            year=int(year),
            mmdd=int(mmdd),
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_bkjy_value_by_date fail: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 12. 市场交易数据 (SC 字段)
# ---------------------------------------------------------------------------

def get_scjy_value(fields: list[str], start_time: str = "", end_time: str = "") -> dict:
    tq = get_tq()
    if tq is None:
        return {}
    if not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_scjy_value,
            field_list=list(fields),
            start_time=start_time,
            end_time=end_time,
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_scjy_value fail: %s", exc)
        return {}


def get_scjy_value_by_date(fields: list[str], year: int = 0, mmdd: int = 0) -> dict:
    tq = get_tq()
    if tq is None:
        return {}
    if not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_scjy_value_by_date,
            field_list=list(fields),
            year=int(year),
            mmdd=int(mmdd),
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_scjy_value_by_date fail: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 13. 专业财务 (FN 字段)
# ---------------------------------------------------------------------------

def get_financial_data(
    codes: list[str],
    fields: list[str],
    start_time: str = "",
    end_time: str = "",
    report_type: str = "announce_time",
) -> dict:
    """专业财务数据 (FN1-FN584)。

    需要客户端预先下载"专业财务数据包"，否则字段全部返回 ``"--"``。

    report_type: ``"announce_time"`` 按公告日期 / ``"tag_time"`` 按报告期。
    """
    tq = get_tq()
    if tq is None:
        return {}
    stock_list = [_normalize_code(c) for c in (codes or [])]
    if not stock_list or not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_financial_data,
            stock_list=stock_list,
            field_list=list(fields),
            start_time=start_time,
            end_time=end_time,
            report_type=report_type,
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_financial_data fail: %s", exc)
        return {}


def get_financial_data_by_date(
    codes: list[str],
    fields: list[str],
    year: int = 0,
    mmdd: int = 0,
) -> dict:
    """指定日期专业财务数据。

    year=0,mmdd=0 取最新；mmdd in {331,630,930,1231} 指定季度。
    """
    tq = get_tq()
    if tq is None:
        return {}
    stock_list = [_normalize_code(c) for c in (codes or [])]
    if not stock_list or not fields:
        return {}
    try:
        return _call_with_retry(
            tq.get_financial_data_by_date,
            stock_list=stock_list,
            field_list=list(fields),
            year=int(year),
            mmdd=int(mmdd),
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] get_financial_data_by_date fail: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 14. 股票列表 / 板块列表 / 板块成份股
# ---------------------------------------------------------------------------
