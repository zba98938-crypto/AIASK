"""Compatibility exports for the shared AIASK storage layer."""

import logging

from aiask_quant_core.storage import (
    SQLiteAdapter,
    await_with_db_cleanup,
    close_db,
    drain_cleanup_callbacks,
    get_db,
    run_with_db_cleanup,
)

logger = logging.getLogger(__name__)

try:
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services

    ensure_default_runtime_services()
except Exception as exc:
    logger.debug("AKShare storage runtime hook registration skipped: %s", exc)

__all__ = [
    "SQLiteAdapter",
    "get_db",
    "close_db",
    "drain_cleanup_callbacks",
    "await_with_db_cleanup",
    "run_with_db_cleanup",
]
