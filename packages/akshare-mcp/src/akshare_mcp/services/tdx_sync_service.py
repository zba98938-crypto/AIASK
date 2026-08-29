"""TDX sync service — Phase 2 of TDX migration.

Provides 13 incremental sync tasks for the 8 TDX-specific tables and the
``stocks`` / ``market_blocks`` / ``block_stocks`` shared tables. Each task is
idempotent; a failure of one task should not block the others.

The service is invoked from ``data_sync_scheduler.run_once`` after the
existing K-line / financial phases (so we keep the legacy pipeline as a
fallback during the cut-over).

Tasks:
- sync_stock_basic              全 A 股票名称 → ``stocks``
- sync_sector_basic             板块列表 + 成分 → ``market_blocks`` / ``block_stocks``
- sync_relation                 板块归属 → ``tdx_relation``
- sync_more_info                88 字段每日快照 → ``tdx_stock_extra``
- sync_consensus                GO 一致预期 → ``tdx_consensus``
- sync_gpjy_daily               个股 GP 字段（按 _by_date 取最新）→ ``tdx_gpjy_daily``
- sync_bkjy_daily               板块 BK 字段 → ``tdx_bkjy_daily``
- sync_scjy_daily               市场 SC 字段 → ``tdx_scjy_daily``
- sync_kzz_basic                可转债基础数据 → ``tdx_kzz_basic``
- sync_ipo_events               新股新债申购 → ``events`` 表
- sync_divid_events             分红配股 → ``events`` 表（增量）
- sync_financial_pro            专业财务 (FN) → ``tdx_financial_pro``
- sync_trading_dates            交易日历 → ``trading_dates``
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, date
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 默认 GP/BK/SC/FN 字段集合（取信号最强、字段最稳定的子集；
# 全字段同步可通过 .env 覆盖）
#
# 注意：通达信"股票数据包"是付费功能。免费版本下 GP/BK/SC 仅以下子集
# 实测能返回真值（probe_tdx_all + probe_shapes 实测）。其他字段（龙虎榜、
# 融资融券明细、陆股通、大宗交易等）需购买股票数据包。
# 用 ``TDX_SYNC_GP_FIELDS / TDX_SYNC_BK_FIELDS / TDX_SYNC_SC_FIELDS``
# 环境变量覆盖；或通过 ``TDX_SYNC_FREE_TIER=0`` 关闭"仅免费字段"过滤。
FREE_GP_FIELDS = ["GP03", "GP11", "GP12", "GP13", "GP25", "GP36"]
FULL_GP_FIELDS = [
    "GP01", "GP02", "GP03", "GP04", "GP06", "GP07", "GP11", "GP12", "GP13",
    "GP14", "GP15", "GP16", "GP21", "GP24", "GP25", "GP31", "GP42",
]

FREE_BK_FIELDS = ["BK9", "BK12", "BK13", "BK17"]   # 涨跌家数 / 涨停家数 / 跌停家数 / 开盘成交
FULL_BK_FIELDS = [
    "BK5", "BK6", "BK7", "BK9", "BK10", "BK11", "BK12",
    "BK15", "BK16", "BK17", "BK18", "BK19",
]

FREE_SC_FIELDS = ["SC01", "SC02", "SC20", "SC25", "SC36"]
FULL_SC_FIELDS = [
    "SC01", "SC02", "SC03", "SC04", "SC11", "SC15", "SC16", "SC17",
    "SC20", "SC23", "SC25", "SC27", "SC28", "SC30", "SC31", "SC34",
    "SC38", "SC42",
]


def _resolve_field_set(env_key: str, free: list, full: list) -> list:
    """根据 env 选择字段集合：``TDX_SYNC_<KIND>_FIELDS`` 优先，
    否则 ``TDX_SYNC_FREE_TIER=1``（默认）= 免费集；``=0`` = 完整集。"""
    explicit = os.getenv(env_key, "").strip()
    if explicit:
        return [t.strip() for t in explicit.split(",") if t.strip()]
    if os.getenv("TDX_SYNC_FREE_TIER", "1") in ("0", "false", "no"):
        return list(full)
    return list(free)


DEFAULT_GP_FIELDS = _resolve_field_set("TDX_SYNC_GP_FIELDS", FREE_GP_FIELDS, FULL_GP_FIELDS)
DEFAULT_BK_FIELDS = _resolve_field_set("TDX_SYNC_BK_FIELDS", FREE_BK_FIELDS, FULL_BK_FIELDS)
DEFAULT_SC_FIELDS = _resolve_field_set("TDX_SYNC_SC_FIELDS", FREE_SC_FIELDS, FULL_SC_FIELDS)

# 项目内常用的 FN 字段（参照 TDX_DATA_SOURCE_MIGRATION_PLAN.md 附录 A）
DEFAULT_FN_FIELDS = [
    "FN1",    # 基本每股收益
    "FN4",    # 每股净资产
    "FN6",    # 净资产收益率
    "FN40",   # 资产总计
    "FN63",   # 负债合计
    "FN72",   # 所有者权益合计
    "FN107",  # 经营现金流净额
    "FN159",  # 流动比率
    "FN160",  # 速动比率
    "FN183",  # 营收增长率
    "FN184",  # 净利润增长率
    "FN197",  # 净资产收益率
    "FN199",  # 销售净利率
    "FN202",  # 销售毛利率
    "FN206",  # 扣非净利润
    "FN210",  # 资产负债率
    "FN230",  # 营业收入
    "FN232",  # 归母净利润
    "FN238",  # 总股本
    "FN239",  # 流通A股
    "FN308",  # 近一年归母净利润
    "FN319",  # TTM 营业总收入
]

# GO 字段：完整 47 项（一次性单点查询，开销小）
DEFAULT_GO_FIELDS = [f"GO{i}" for i in range(1, 48)]


def _split_value_pairs(payload: Any) -> Tuple[Optional[float], Optional[float]]:
    """tqcenter 的 GP/BK/SC ``Value`` 通常是长度 2 的字符串列表。"""
    if isinstance(payload, list):
        if len(payload) == 0:
            return None, None
        a = payload[0] if len(payload) >= 1 else None
        b = payload[1] if len(payload) >= 2 else None
        return _to_float(a), _to_float(b)
    if isinstance(payload, dict):
        return _to_float(payload.get("Value")), None
    return _to_float(payload), None


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "" or val == "--":
        return None
    try:
        v = float(val)
        return v if v == v else None
    except (TypeError, ValueError):
        return None

from .tdx_sync_market import _MarketSyncMixin
from .tdx_sync_financial import _FinancialSyncMixin
from .tdx_sync_events import _EventsSyncMixin
from .tdx_sync_derived import _DerivedSyncMixin
from .tdx_sync_completeness import _CompletenessMixin


class TdxSyncService(
    _MarketSyncMixin,
    _FinancialSyncMixin,
    _EventsSyncMixin,
    _DerivedSyncMixin,
    _CompletenessMixin,
):
    """13 个 TDX sync 任务的协调器。

    设计原则：
    - 每个任务独立 try/except，单任务失败不影响其它
    - 任务返回 ``{"task": str, "ok": bool, "stats": dict, "error": Optional[str]}``
    - 支持选择性跳过（``TDX_SYNC_DISABLE=task1,task2``）
    """

    def __init__(self,
                 universe: Optional[List[str]] = None,
                 limit_consensus: int = 200,
                 limit_more_info: int = 500,
                 limit_gpjy: int = 200,
                 limit_financial: int = 50,
                 limit_kzz: int = 200,
                 limit_relation: int = 200):
        self.universe = list(universe or [])
        self.limit_consensus = limit_consensus
        self.limit_more_info = limit_more_info
        self.limit_gpjy = limit_gpjy
        self.limit_financial = limit_financial
        self.limit_kzz = limit_kzz
        self.limit_relation = limit_relation
        # ``sync_financial_pro`` 依赖通达信"专业财务数据"包（付费功能）。
        # 默认禁用以免每次同步都报错；用户购买并下载完毕后通过设置
        # ``TDX_SYNC_ENABLE_PRO_FIN=1`` 主动启用。
        default_disabled = set()
        if os.getenv("TDX_SYNC_ENABLE_PRO_FIN", "0") not in ("1", "true", "yes"):
            default_disabled.add("sync_financial_pro")
        env_disabled = {
            t.strip() for t in os.getenv("TDX_SYNC_DISABLE", "").split(",") if t.strip()
        }
        self.disabled = default_disabled | env_disabled

    # ------------------------------------------------------------------
    # 调度入口
    # ------------------------------------------------------------------

    async def run_all(self) -> Dict[str, Any]:
        from ..storage import get_db
        db = get_db()

        results: List[Dict[str, Any]] = []
        tasks = [
            ("sync_trading_dates", self._sync_trading_dates),
            ("sync_stock_basic", self._sync_stock_basic),
            ("sync_quote_snapshots", self._sync_quote_snapshots),
            ("sync_index_klines", self._sync_index_klines),
            ("sync_sector_basic", self._sync_sector_basic),
            ("sync_more_info", self._sync_more_info),
            ("sync_consensus", self._sync_consensus),
            ("sync_relation", self._sync_relation),
            ("sync_gpjy_daily", self._sync_gpjy_daily),
            ("sync_bkjy_daily", self._sync_bkjy_daily),
            ("sync_scjy_daily", self._sync_scjy_daily),
            ("sync_kzz_basic", self._sync_kzz_basic),
            ("sync_ipo_events", self._sync_ipo_events),
            ("sync_divid_events", self._sync_divid_events),
            ("sync_financial_pro", self._sync_financial_pro),
            ("sync_basic_financial", self._sync_basic_financial),
            ("sync_stock_fund_flow", self._sync_stock_fund_flow),
            ("sync_derived_factory_market_data", self._sync_derived_factory_market_data),
            ("sync_external_gap_data", self._sync_external_gap_data),
            ("record_tdx_data_completeness", self._record_tdx_data_completeness),
        ]
        for name, fn in tasks:
            if name in self.disabled:
                results.append({"task": name, "ok": True, "skipped": True})
                continue
            started = datetime.now()
            try:
                stats = await fn(db)
                results.append({
                    "task": name, "ok": True,
                    "stats": stats or {},
                    "elapsed_sec": round((datetime.now() - started).total_seconds(), 2),
                })
            except Exception as exc:
                logger.exception("[TdxSync] %s failed", name)
                results.append({
                    "task": name, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_sec": round((datetime.now() - started).total_seconds(), 2),
                })

        ok = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
        skipped = sum(1 for r in results if r.get("skipped"))
        failed = sum(1 for r in results if not r.get("ok"))
        return {
            "summary": {"ok": ok, "skipped": skipped, "failed": failed,
                         "total": len(tasks)},
            "tasks": results,
        }

    # ------------------------------------------------------------------
    # 1. trading_dates
    # ------------------------------------------------------------------


async def run_tdx_sync(universe: Optional[List[str]] = None) -> Dict[str, Any]:
    svc = TdxSyncService(universe=universe)
    return await svc.run_all()
