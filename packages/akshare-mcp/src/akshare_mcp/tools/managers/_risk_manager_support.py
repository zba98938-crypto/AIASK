"""Shared data-loading helpers for risk_manager actions."""

from __future__ import annotations

from typing import Any

from ...utils import normalize_code


def _extract_holding_code(holding: dict[str, Any]) -> str:
    return normalize_code(
        str(
            holding.get("code")
            or holding.get("stock_code")
            or holding.get("symbol")
            or ""
        )
    )


def _extract_holding_shares(holding: dict[str, Any]) -> float:
    raw = holding.get("shares")
    if raw is None:
        raw = holding.get("quantity")
    if raw is None:
        raw = holding.get("qty")
    return float(raw or 0)


async def _load_portfolio_holdings(conn: Any, portfolio_id: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT * FROM holdings WHERE portfolio_id = $1", portfolio_id)
    holdings: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        code = _extract_holding_code(item)
        shares = _extract_holding_shares(item)
        if not code or shares <= 0:
            continue
        holdings.append({**item, "code": code, "shares": shares})
    return holdings


async def _get_klines_with_fallback(db: Any, code: str, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        klines = await db.get_klines(code, limit=limit)
        if klines:
            return klines, ["db.get_klines"]
    except Exception:
        pass

    try:
        from ..market import get_kline

        result = await get_kline(code, "daily", limit)
        if result.get("success") and isinstance(result.get("data"), list):
            return result["data"], ["tools.market.get_kline"]
    except Exception:
        pass
    return [], []


async def _get_stock_info_with_fallback(db: Any, code: str) -> tuple[dict[str, Any], list[str]]:
    try:
        payload = await db.get_stock_info(code)
        if isinstance(payload, dict):
            return payload, ["db.get_stock_info"]
    except Exception:
        pass
    return {}, []


async def _get_financials_with_fallback(db: Any, code: str) -> tuple[list[dict[str, Any]] | dict[str, Any], list[str]]:
    try:
        payload = await db.get_financials(code, limit=1)
        if isinstance(payload, (list, dict)):
            return payload, ["db.get_financials"]
    except Exception:
        pass
    return [], []


def _dedupe_chain(values: list[str]) -> list[str]:
    chain: list[str] = []
    seen: set[str] = set()
    for value in values:
        label = str(value or "").strip()
        if not label or label in seen:
            continue
        chain.append(label)
        seen.add(label)
    return chain


__all__ = [
    "_dedupe_chain",
    "_extract_holding_code",
    "_extract_holding_shares",
    "_get_financials_with_fallback",
    "_get_klines_with_fallback",
    "_get_stock_info_with_fallback",
    "_load_portfolio_holdings",
]
