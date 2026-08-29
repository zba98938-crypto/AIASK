"""SQLite persistence helpers for market temperature snapshots."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Optional


def _json_text(value: Any, default: Any) -> str:
    if value in (None, ""):
        value = default
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _coerce_date_key(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10]


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MarketTemperatureStorageMixin:
    """Durable local cache for daily market-temperature snapshots."""

    @staticmethod
    def _decode_market_temperature_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        snapshot = _decode_json(payload.get("snapshot_json"), {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        source_chain = _decode_json(payload.get("source_chain"), [])
        request = _decode_json(payload.get("request_json"), {})
        warnings = _decode_json(payload.get("warnings"), [])
        payload["snapshot"] = snapshot
        payload["source_chain"] = source_chain if isinstance(source_chain, list) else []
        payload["request"] = request if isinstance(request, dict) else {}
        payload["warnings"] = warnings if isinstance(warnings, list) else []
        payload.pop("snapshot_json", None)
        payload.pop("request_json", None)
        return payload

    async def save_market_temperature_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        request: dict[str, Any] | None = None,
        source_chain: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = dict(snapshot or {})
        as_of = _coerce_date_key(payload.get("as_of"))
        if not as_of:
            raise ValueError("market temperature snapshot as_of is required")
        contract_version = str(payload.get("contract_version") or "market_temperature.v1")
        market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
        quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
        industries = payload.get("industries") if isinstance(payload.get("industries"), list) else []
        warnings = quality.get("warnings") if isinstance(quality.get("warnings"), list) else []
        resolved_source_chain = source_chain or payload.get("source_chain") or []
        if resolved_source_chain:
            payload["source_chain"] = [str(item) for item in resolved_source_chain if str(item).strip()]
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO market_temperature_snapshots (
                    as_of,
                    contract_version,
                    market_temperature,
                    market_state,
                    stock_count,
                    industry_count,
                    quality_status,
                    warnings,
                    snapshot_json,
                    source_chain,
                    request_json,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, CURRENT_TIMESTAMP)
                ON CONFLICT (as_of) DO UPDATE SET
                    contract_version = EXCLUDED.contract_version,
                    market_temperature = EXCLUDED.market_temperature,
                    market_state = EXCLUDED.market_state,
                    stock_count = EXCLUDED.stock_count,
                    industry_count = EXCLUDED.industry_count,
                    quality_status = EXCLUDED.quality_status,
                    warnings = EXCLUDED.warnings,
                    snapshot_json = EXCLUDED.snapshot_json,
                    source_chain = EXCLUDED.source_chain,
                    request_json = EXCLUDED.request_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                as_of,
                contract_version,
                _safe_float(market.get("temperature")),
                str(market.get("state") or "unknown"),
                _safe_int(market.get("stock_count")),
                _safe_int(quality.get("industry_count"), len(industries)),
                str(quality.get("status") or "unknown"),
                _json_text(warnings, []),
                _json_text(payload, {}),
                _json_text(resolved_source_chain, []),
                _json_text(request or {}, {}),
            )
        cached = await self.get_market_temperature_snapshot_cache(as_of)
        if cached is None:
            raise RuntimeError("market temperature snapshot cache write did not round-trip")
        return cached

    async def get_market_temperature_snapshot_cache(self, as_of: Any = None) -> dict[str, Any] | None:
        as_of_key = _coerce_date_key(as_of)
        async with self.acquire() as conn:
            if as_of_key:
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM market_temperature_snapshots
                    WHERE as_of = $1
                    LIMIT 1
                    """,
                    as_of_key,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM market_temperature_snapshots
                    ORDER BY as_of DESC, updated_at DESC
                    LIMIT 1
                    """
                )
        return self._decode_market_temperature_snapshot_row(dict(row)) if row else None

    async def list_market_temperature_snapshot_cache(self, limit: int = 30) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 30), 365))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM market_temperature_snapshots
                ORDER BY as_of DESC, updated_at DESC
                LIMIT $1
                """,
                safe_limit,
            )
        return [self._decode_market_temperature_snapshot_row(dict(row)) for row in rows]
