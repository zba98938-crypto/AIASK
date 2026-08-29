"""Adapter from Strategy Factory execution-universe contract to SignalTracker dicts."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


async def load_executable_strategies_with_fallback(
    db: Any,
    *,
    limit: int = 500,
    use_contract: bool = True,
) -> list[dict[str, Any]]:
    """Load executable strategies as plain dicts for SignalTracker phases.

    Contract mode is fail-closed: an empty or failed contract query returns an
    empty universe. The wider legacy query is available only when explicitly
    requested with ``use_contract=False``.
    """
    if use_contract:
        try:
            # Public surface only; internal subpackages are package-private.
            from strategy_factory.api.contracts import (
                ExecutionUniverseContract,
                ExecutionUniverseQuery,
            )

            contract = ExecutionUniverseContract()
            query = ExecutionUniverseQuery(limit=max(1, int(limit or 500)))
            items = await contract.list_executable_strategies(db, query)
            strategies: list[dict[str, Any]] = []
            for item in list(items or []):
                payload = item if isinstance(item, dict) else None
                get_value = payload.get if payload is not None else lambda key, default=None: getattr(item, key, default)
                strategy_id = str(
                    get_value("strategy_id")
                    or get_value("id")
                    or ""
                ).strip()
                if not strategy_id:
                    continue
                incubation_stage = get_value("incubation_stage") or get_value("incubating")
                execution_mode = "diagnostic" if incubation_stage == "diagnostic" else "paper"
                strategies.append(
                    {
                        "id": strategy_id,
                        "strategy_id": strategy_id,
                        "name": get_value("strategy_name") or get_value("name"),
                        "status": get_value("physical_status") or get_value("status") or "submitted",
                        "incubating": incubation_stage,
                        "account_id": get_value("account_id"),
                        "execution_mode": str(
                            getattr(get_value("execution_mode"), "value", get_value("execution_mode"))
                            or execution_mode
                        ),
                        "runtime_control_enabled": bool(get_value("runtime_control_enabled")),
                        "runtime_control_reason": get_value("runtime_control_reason"),
                        "strategy_type": get_value("strategy_type"),
                        "params": get_value("params") or {},
                    }
                )
            return strategies[: max(1, int(limit or 500))]
        except Exception:
            logger.exception("execution-universe contract failed; returning an empty universe")
            return []

    # Explicit compatibility mode: submitted/incubating strategies with optional account binding.
    rows: list[Any] = []
    try:
        if hasattr(db, "acquire"):
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        s.id,
                        s.name,
                        s.status,
                        s.incubating,
                        s.strategy_type,
                        s.params,
                        sia.account_id
                    FROM strategies s
                    LEFT JOIN strategy_incubation_accounts sia
                        ON sia.strategy_id = s.id AND sia.status = 'active'
                    WHERE s.status IN ('submitted', 'incubating', 'listed')
                    ORDER BY s.updated_at DESC
                    LIMIT $1
                    """,
                    max(1, int(limit or 500)),
                )
        elif hasattr(db, "list_strategies"):
            for status in ("submitted", "incubating", "listed"):
                batch = await db.list_strategies(status, limit=max(1, int(limit or 500)))
                rows.extend(list(batch or []))
    except Exception:
        rows = []

    out: list[dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "keys"):
            item = dict(row)
        elif isinstance(row, dict):
            item = dict(row)
        else:
            continue
        strategy_id = str(item.get("id") or item.get("strategy_id") or "").strip()
        if not strategy_id:
            continue
        out.append(
            {
                "id": strategy_id,
                "strategy_id": strategy_id,
                "name": item.get("name"),
                "status": item.get("status") or "submitted",
                "incubating": item.get("incubating"),
                "account_id": item.get("account_id"),
                "execution_mode": "paper",
                "strategy_type": item.get("strategy_type"),
                "params": item.get("params") or {},
            }
        )
    return out[: max(1, int(limit or 500))]


__all__ = ["load_executable_strategies_with_fallback"]
