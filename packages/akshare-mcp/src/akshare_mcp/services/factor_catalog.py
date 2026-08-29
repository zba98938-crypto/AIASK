"""统一因子字典/货架：整合 SUPPORTED_FACTORS 与 DSL 白名单。

P1-3 FACTOR-SUPERMARKET-GAP: 显式化因子定义、公式、方向、类别、IC 历史指针等元数据，
让 AI/Router 规范"进货"。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, TypedDict

from ..tools.quant_definitions import SUPPORTED_FACTORS
from .factor_candidate_compiler import (
    SUPPORTED_FACTOR_FIELDS,
    SUPPORTED_FACTOR_FUNCTIONS,
)


class FactorMetadata(TypedDict, total=False):
    """因子元数据标准结构。"""

    name: str
    category: str
    description: str
    requires_financials: bool
    sub_factors: List[str]
    aliases: List[str]
    formula: Optional[str]
    direction: str  # "positive" | "negative" | "neutral"
    version: str
    update_time: str
    ic_history_available: bool
    latest_ic: Optional[Dict[str, Any]]
    dsl_fields: List[str]
    dsl_functions: List[str]


# ── Factor direction heuristics ──────────────────────────────────

_POSITIVE_DIRECTION_FACTORS = {
    "momentum",
    "trend",
    "quality",
    "growth",
    "mom_1d",
    "mom_5d",
    "mom_10d",
    "mom_60d",
    "rsi_14",
    "rsi_6",
    "macd_signal",
    "macd_histogram",
    "roe_ttm",
    "roa_ttm",
    "gross_margin",
    "net_margin",
    "revenue_growth_yoy",
    "dividend_yield",
    "obv_slope",
    "sentiment_score",
    "capital_flow",
    "north_flow",
    "institutional_flow",
    "event_intensity",
}

_NEGATIVE_DIRECTION_FACTORS = {
    "reversal",
    "volatility",
    "value",
    "vol_5d",
    "vol_10d",
    "vol_60d",
    "atr_14",
    "atr_20",
    "bollinger_width",
    "downside_vol",
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "debt_to_equity",
}

_NEUTRAL_DIRECTION_FACTORS = {
    "size",
    "volume_ratio",
    "turnover_5d",
    "turnover_20d",
    "vwap_deviation",
}


def _infer_direction(factor_name: str, category: str) -> str:
    """推断因子方向（高值 = 好 / 坏 / 中性）。"""
    if factor_name in _POSITIVE_DIRECTION_FACTORS:
        return "positive"
    if factor_name in _NEGATIVE_DIRECTION_FACTORS:
        return "negative"
    if factor_name in _NEUTRAL_DIRECTION_FACTORS:
        return "neutral"

    # Category fallback
    if category in {"technical", "volume"}:
        if "momentum" in factor_name or "return" in factor_name:
            return "positive"
        if "vol" in factor_name or "atr" in factor_name:
            return "negative"
    if category == "fundamental":
        if "growth" in factor_name or "margin" in factor_name:
            return "positive"
        if "pe" in factor_name or "pb" in factor_name or "ps" in factor_name:
            return "negative"

    return "neutral"


def _build_formula_hint(factor_name: str, sub_factors: List[str]) -> Optional[str]:
    """为常见因子生成公式提示。"""
    formula_map = {
        "momentum": "close.pct_change(20)",
        "mom_1d": "close.pct_change(1)",
        "mom_5d": "close.pct_change(5)",
        "mom_10d": "close.pct_change(10)",
        "mom_60d": "close.pct_change(60)",
        "rsi_14": "RSI(close, 14)",
        "rsi_6": "RSI(close, 6)",
        "macd_signal": "MACD(close).signal",
        "macd_histogram": "MACD(close).histogram",
        "volatility": "returns_1d.rolling(20).std() * sqrt(252)",
        "vol_5d": "returns_1d.rolling(5).std() * sqrt(252)",
        "vol_10d": "returns_1d.rolling(10).std() * sqrt(252)",
        "vol_60d": "returns_1d.rolling(60).std() * sqrt(252)",
        "atr_14": "ATR(high, low, close, 14)",
        "atr_20": "ATR(high, low, close, 20)",
        "volume_ratio": "volume.rolling(5).mean() / volume.rolling(20).mean()",
        "pe_ttm": "price / eps_ttm",
        "pb_mrq": "price / bps_mrq",
        "ps_ttm": "price / revenue_per_share_ttm",
        "roe_ttm": "net_profit_ttm / equity",
        "roa_ttm": "net_profit_ttm / total_assets",
    }
    return formula_map.get(factor_name)


# ── Unified Factor Catalog ──────────────────────────────────────

async def build_factor_catalog(
    *,
    include_dsl_metadata: bool = True,
    version: str = "1.0.0",
    db: Any | None = None,
) -> Dict[str, FactorMetadata]:
    """构建统一因子字典。

    Args:
        include_dsl_metadata: 是否附加 DSL 白名单字段/函数信息
        version: 因子库版本号

    Returns:
        因子名 -> FactorMetadata 字典
    """
    catalog: Dict[str, FactorMetadata] = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    for factor_name, meta in SUPPORTED_FACTORS.items():
        category = meta.get("category", "unknown")
        description = meta.get("description", "")
        requires_financials = meta.get("requires_financials", False)
        sub_factors = meta.get("sub_factors", [])
        aliases = meta.get("aliases", [])

        direction = _infer_direction(factor_name, category)
        formula = _build_formula_hint(factor_name, sub_factors)

        latest_ic = await _load_latest_ic_summary(factor_name, db=db)
        ic_available = latest_ic is not None

        entry: FactorMetadata = {
            "name": factor_name,
            "category": category,
            "description": description,
            "requires_financials": requires_financials,
            "sub_factors": sub_factors,
            "aliases": aliases,
            "formula": formula,
            "direction": direction,
            "version": version,
            "update_time": now,
            "ic_history_available": ic_available,
            "latest_ic": latest_ic,
        }

        if include_dsl_metadata:
            entry["dsl_fields"] = list(SUPPORTED_FACTOR_FIELDS)
            entry["dsl_functions"] = list(SUPPORTED_FACTOR_FUNCTIONS)

        catalog[factor_name] = entry

    return catalog

def _normalize_ic_row(row: Mapping[str, Any] | None) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    payload = dict(row)
    factor_name = payload.get("factor_name") or payload.get("factor")
    ic_date = payload.get("ic_date") or payload.get("date")
    return {
        "factor_name": factor_name,
        "period": str(payload.get("period") or ""),
        "ic_date": str(ic_date or ""),
        "ic_value": payload.get("ic_value"),
        "rank_ic": payload.get("rank_ic"),
        "stock_count": payload.get("stock_count"),
    }


async def _load_latest_ic_summary(factor_name: str, *, db: Any | None = None) -> Optional[Dict[str, Any]]:
    """检查因子是否有 IC 历史记录。"""
    try:
        if db is None:
            from ..storage import get_db
            db = get_db()
        if hasattr(db, "get_factor_ic_history"):
            rows = await db.get_factor_ic_history(factor_name, "20", 1)
            if rows:
                return _normalize_ic_row(rows[0])
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM factor_ic_history
                WHERE factor_name = $1
                ORDER BY ic_date DESC
                LIMIT 1
                """,
                factor_name,
            )
            return _normalize_ic_row(row)
    except Exception:
        return None


# ── Catalog query helpers ──────────────────────────────────────

async def get_factors_by_category(category: str) -> List[str]:
    """按类别筛选因子。"""
    catalog = await build_factor_catalog(include_dsl_metadata=False)
    return [
        name
        for name, meta in catalog.items()
        if meta.get("category") == category
    ]


async def get_factors_requiring_financials() -> List[str]:
    """获取需要财务数据的因子列表。"""
    catalog = await build_factor_catalog(include_dsl_metadata=False)
    return [
        name
        for name, meta in catalog.items()
        if meta.get("requires_financials", False)
    ]


async def get_factor_metadata(factor_name: str) -> Optional[FactorMetadata]:
    """获取单个因子的完整元数据。"""
    catalog = await build_factor_catalog(include_dsl_metadata=True)
    return catalog.get(factor_name)


async def list_all_factor_names() -> List[str]:
    """列出所有因子名称（含别名）。"""
    catalog = await build_factor_catalog(include_dsl_metadata=False)
    names = set(catalog.keys())

    for meta in catalog.values():
        names.update(meta.get("aliases", []))

    return sorted(names)


async def resolve_factor_alias(alias: str) -> Optional[str]:
    """别名 -> 规范名称解析。"""
    catalog = await build_factor_catalog(include_dsl_metadata=False)

    # Direct match
    if alias in catalog:
        return alias

    # Alias lookup
    for canonical_name, meta in catalog.items():
        if alias in meta.get("aliases", []):
            return canonical_name

    return None


# ── DSL whitelist accessors ──────────────────────────────────────

def get_dsl_fields() -> List[str]:
    """获取 DSL 白名单字段列表。"""
    return sorted(SUPPORTED_FACTOR_FIELDS)


def get_dsl_functions() -> List[str]:
    """获取 DSL 白名单函数列表。"""
    return sorted(SUPPORTED_FACTOR_FUNCTIONS)


def get_dsl_summary() -> Dict[str, Any]:
    """获取 DSL 白名单摘要。"""
    return {
        "fields": get_dsl_fields(),
        "field_count": len(SUPPORTED_FACTOR_FIELDS),
        "functions": get_dsl_functions(),
        "function_count": len(SUPPORTED_FACTOR_FUNCTIONS),
        "version": "1.0.0",
    }


# ── Catalog statistics ──────────────────────────────────────────

async def get_catalog_stats() -> Dict[str, Any]:
    """获取因子库统计信息。"""
    catalog = await build_factor_catalog(include_dsl_metadata=False)

    category_counts: Dict[str, int] = {}
    direction_counts: Dict[str, int] = {}
    financials_required = 0

    for meta in catalog.values():
        cat = meta.get("category", "unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1

        direction = meta.get("direction", "neutral")
        direction_counts[direction] = direction_counts.get(direction, 0) + 1

        if meta.get("requires_financials", False):
            financials_required += 1

    return {
        "total_factors": len(catalog),
        "category_distribution": category_counts,
        "direction_distribution": direction_counts,
        "financials_required_count": financials_required,
        "dsl_field_count": len(SUPPORTED_FACTOR_FIELDS),
        "dsl_function_count": len(SUPPORTED_FACTOR_FUNCTIONS),
    }


# ── Environment toggle (可选) ──────────────────────────────────

def factor_catalog_enabled() -> bool:
    """检查因子字典功能是否启用。"""
    raw = os.getenv("STRATEGY_FACTORY_FACTOR_CATALOG_ENABLED")
    return raw is not None and str(raw).strip().lower() in {"1", "true", "yes", "on"}
