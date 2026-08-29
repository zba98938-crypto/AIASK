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

def get_more_info(code: str) -> Optional[dict]:
    tq = get_tq()
    if tq is None:
        return None
    stock = _normalize_code(code)
    try:
        info = _call_with_retry(tq.get_more_info, stock_code=stock, field_list=[])
    except Exception as exc:
        logger.warning("[TdxTQ] get_more_info fail %s: %s", code, exc)
        return None
    if not info:
        return None
    return dict(info)


# ---------------------------------------------------------------------------
# 4. 板块归属
# ---------------------------------------------------------------------------

def get_relation(code: str) -> list[dict]:
    """股票所属板块。返回 list[{block_code, block_name, block_type, gp_num}]。

    block_type 取值：行业 / 概念 / 风格 / 地区 / 指数。
    """
    tq = get_tq()
    if tq is None:
        return []
    stock = _normalize_code(code)
    try:
        rel = _call_with_retry(tq.get_relation, stock_code=stock)
    except Exception as exc:
        logger.warning("[TdxTQ] get_relation fail %s: %s", code, exc)
        return []
    if not rel:
        return []
    out: list[dict] = []
    for item in rel:
        if not isinstance(item, dict):
            continue
        out.append({
            "block_code": str(item.get("BlockCode", "") or ""),
            "block_name": str(item.get("BlockName", "") or ""),
            "block_type": str(item.get("BlockType", "") or ""),
            "gp_num": _safe_int(item.get("GPNume")),
        })
    return out


# ---------------------------------------------------------------------------
# 5. 分红送配
# ---------------------------------------------------------------------------

def get_divid_factors(code: str, start_time: str = "", end_time: str = "") -> list[dict]:
    """除权除息 / 送股 / 配股 历史。

    每条 dict：date / type / bonus(派息) / allot_price / share_bonus / allotment。
    Type=1 除权除息, 11 扩缩股, 15 重新调整。
    """
    tq = get_tq()
    if tq is None:
        return []
    stock = _normalize_code(code)
    try:
        df = _call_with_retry(tq.get_divid_factors,
                              stock_code=stock,
                              start_time=start_time,
                              end_time=end_time)
    except Exception as exc:
        logger.warning("[TdxTQ] divid_factors fail %s: %s", code, exc)
        return []
    if df is None or getattr(df, "empty", True):
        return []
    rows: list[dict] = []
    try:
        for ts, row in df.iterrows():
            rows.append({
                "date": _format_date(ts),
                "type": _safe_int(row.get("Type")),
                "bonus": _safe_float(row.get("Bonus")),
                "allot_price": _safe_float(row.get("AllotPrice")) or _safe_float(row.get("AlloPrice")),
                "share_bonus": _safe_float(row.get("ShareBonus")),
                "allotment": _safe_float(row.get("Allotment")),
            })
    except Exception as exc:
        logger.warning("[TdxTQ] divid_factors parse %s: %s", code, exc)
        return []
    return rows


# ---------------------------------------------------------------------------
# 6. 新股 / 新债申购
# ---------------------------------------------------------------------------

def get_ipo_info(ipo_type: int = 2, ipo_date: int = 1) -> list[dict]:
    """新股+新债申购信息。

    参数：
    - ipo_type：0 新股，1 新债，2 全部
    - ipo_date：0 今天，1 今天及以后

    返回：每条 ``{code, name, sg_code, sg_date, sg_price, max_sg, pe_issue, set_code, type}``。
    """
    tq = get_tq()
    if tq is None:
        return []
    try:
        items = _call_with_retry(tq.get_ipo_info, ipo_type=int(ipo_type), ipo_date=int(ipo_date))
    except Exception as exc:
        logger.warning("[TdxTQ] get_ipo_info fail: %s", exc)
        return []
    if not items:
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        set_code = str(item.get("SetCode", "") or item.get("setcode", "") or "")
        # 区分股票/债券：通达信文档里 SGCode 7 位 (787xxx/371xxx 等) 表示新债
        sg_code = str(item.get("SGCode", "") or "")
        is_bond = sg_code.startswith(("371", "718", "787", "072", "070", "073", "077", "082"))
        out.append({
            "code": str(item.get("code", "")),
            "name": str(item.get("name", "")),
            "sg_code": sg_code,
            "sg_date": _format_date(item.get("SGDate")),
            "sg_price": _safe_float(item.get("SGPrice")),
            "max_sg": _safe_float(item.get("MaxSG")),
            "pe_issue": _safe_float(item.get("PE_Issue")),
            "set_code": set_code,
            "type": "bond" if is_bond else "stock",
        })
    return out


# ---------------------------------------------------------------------------
# 7. 股本数据
# ---------------------------------------------------------------------------

def get_gb_info(code: str, dates: list[str]) -> list[dict]:
    """每日总股本/流通股本。

    ``dates`` 必须升序 (TDX 文档要求)，``YYYYMMDD``。返回 list[{date, total_shares,
    float_shares}]。
    """
    tq = get_tq()
    if tq is None:
        return []
    stock = _normalize_code(code)
    cleaned = [str(d).replace("-", "") for d in (dates or []) if d]
    if not cleaned:
        return []
    cleaned.sort()
    try:
        items = _call_with_retry(tq.get_gb_info,
                                 stock_code=stock,
                                 date_list=cleaned,
                                 count=len(cleaned))
    except Exception as exc:
        logger.warning("[TdxTQ] get_gb_info fail %s: %s", code, exc)
        return []
    if not items:
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append({
            "date": _format_date(item.get("Date")),
            "total_shares": _safe_float(item.get("Zgb")),  # 股
            "float_shares": _safe_float(item.get("Ltgb")),  # 股
        })
    return out


# ---------------------------------------------------------------------------
# 8. 可转债基础数据
# ---------------------------------------------------------------------------

def get_kzz_info(code: str) -> Optional[dict]:
    """单只可转债基础信息。25 字段透传 + 数值归一化。"""
    tq = get_tq()
    if tq is None:
        return None
    stock = _normalize_code(code)
    try:
        info = _call_with_retry(tq.get_kzz_info, stock_code=stock, field_list=[])
    except Exception as exc:
        logger.warning("[TdxTQ] get_kzz_info fail %s: %s", code, exc)
        return None
    if not info:
        return None
    out: dict = {
        "kzz_code": str(info.get("KZZCode", "")),
        "stock_code": str(info.get("HSCode", "")),
        "set_code": str(info.get("SetCode", "") or info.get("setcode", "")),
        "convert_price": _safe_float(info.get("ZGPrice")),
        "current_rate": _safe_float(info.get("CurRate")),
        "remain_size_wan": _safe_float(info.get("RestScope")),
        "putback_price": _safe_float(info.get("PutBack")),
        "force_redeem_price": _safe_float(info.get("ForceRedeem")),
        "convert_date": _format_date(info.get("ZGDate")),
        "end_price": _safe_float(info.get("EndPrice")),
        "end_date": _format_date(info.get("EndDate")),
        "convert_rate": _safe_float(info.get("ZGRate")),
        "real_value": _safe_float(info.get("RealValue")),
        "expire_yield": _safe_float(info.get("ExpireYield")),
        "kzz_score": str(info.get("KZZScore", "")),
        "stock_score": str(info.get("HSScore", "")),
        "redeem_date": _format_date(info.get("RedeemDate")),
        "redeem_price": _safe_float(info.get("RedeemPrice")),
        "put_date": _format_date(info.get("PutDate")),
        "put_price": _safe_float(info.get("PutPrice")),
        "convert_code": str(info.get("ZGCode", "")),
        "stock_price": _safe_float(info.get("AGPrice")),
        "kzz_price": _safe_float(info.get("KZZPrice")),
        "premium_rate": _safe_float(info.get("KZZYj")),
        "convert_value": _safe_float(info.get("ZGValue")),
    }
    return out


# ---------------------------------------------------------------------------
# 9. 一致预期 / 业绩预告 / 业绩快报 (GO 字段)
# ---------------------------------------------------------------------------
