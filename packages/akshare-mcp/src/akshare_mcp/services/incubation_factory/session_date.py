"""Incubation-side name for :mod:`aiask_quant_core.market_session`.

The resolver lives in quant-core so strategy-factory (factory submit path)
and akshare-mcp (incubation, SignalTracker) share one implementation.
"""

from __future__ import annotations

from aiask_quant_core.market_session import (  # noqa: F401
    FULL_SESSION_MIN_CODES,
    SOURCE_PARTIAL,
    SOURCE_SYNCED,
    SOURCE_UNREADABLE,
    SOURCE_WALL_CLOCK,
    clear_cache,
    resolve_session_date,
    session_date_or_today,
)
