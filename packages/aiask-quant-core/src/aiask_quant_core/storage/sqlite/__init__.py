"""SQLite storage adapter package.

The public surface intentionally keeps the historical async methods used by the
MCP tools while the runtime backend is now a local SQLite database.
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import logging
import threading
import weakref
from typing import Awaitable, Optional, TypeVar

from .artifacts import ArtifactMixin
from .factor_storage import FactorStorageMixin
from .financials import FinancialsMixin
from .kline import KlineMixin
from .market_context import MarketContextMixin
from .market_temperature import MarketTemperatureStorageMixin
from .quotes import QuotesMixin
from .schema import SchemaBase
from .signal_tracking import SignalTrackingMixin
from .stock_info import StockInfoMixin
from .strategy import StrategyMixin
from .tdx_storage import TdxStorageMixin
from .vector_unified import VectorUnifiedMixin

logger = logging.getLogger(__name__)
T = TypeVar("T")


class SQLiteAdapter(
    KlineMixin,
    StockInfoMixin,
    FinancialsMixin,
    QuotesMixin,
    MarketContextMixin,
    MarketTemperatureStorageMixin,
    VectorUnifiedMixin,
    ArtifactMixin,
    StrategyMixin,
    FactorStorageMixin,
    SignalTrackingMixin,
    TdxStorageMixin,
    SchemaBase,
):
    """SQLite async adapter composed from the storage mixins."""


_db_instance: Optional[SQLiteAdapter] = None
_db_instances_by_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SQLiteAdapter] = weakref.WeakKeyDictionary()
_db_instances_by_thread: dict[int, SQLiteAdapter] = {}
_db_instance_lock = threading.Lock()
_shutdown_registered = False
_cleanup_callbacks_running = False


def _snapshot_instances_locked() -> list[SQLiteAdapter]:
    instances: list[SQLiteAdapter] = []
    seen: set[int] = set()
    for instance in list(_db_instances_by_loop.values()) + list(_db_instances_by_thread.values()) + [_db_instance]:
        if instance is None:
            continue
        marker = id(instance)
        if marker in seen:
            continue
        seen.add(marker)
        instances.append(instance)
    return instances


def _refresh_legacy_alias_locked() -> None:
    global _db_instance
    remaining = _snapshot_instances_locked()
    _db_instance = remaining[0] if remaining else None


async def _flush_cleanup_callbacks() -> None:
    global _cleanup_callbacks_running
    if _cleanup_callbacks_running:
        return
    _cleanup_callbacks_running = True
    await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    try:
        try:
            from aiask_quant_core.storage.runtime_hooks import iter_cleanup_callbacks
        except Exception as exc:
            logger.warning("[storage] cleanup hook registry unavailable: %s", exc)
            return
        for callback in iter_cleanup_callbacks():
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await asyncio.shield(result)
            except Exception as exc:
                logger.warning("[storage] cleanup callback failed: %s", exc)
    finally:
        _cleanup_callbacks_running = False


async def drain_cleanup_callbacks() -> None:
    await _flush_cleanup_callbacks()


async def _close_instance(instance: Optional[SQLiteAdapter]) -> None:
    if instance is not None:
        await asyncio.shield(instance.close())


async def _close_all_db_instances() -> None:
    global _db_instance, _db_instances_by_loop, _db_instances_by_thread
    with _db_instance_lock:
        instances = _snapshot_instances_locked()
        _db_instance = None
        _db_instances_by_loop = weakref.WeakKeyDictionary()
        _db_instances_by_thread = {}
    first_error: Exception | None = None
    for instance in instances:
        try:
            await _close_instance(instance)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _safe_shutdown_db_atexit() -> None:
    with _db_instance_lock:
        instances = _snapshot_instances_locked()
    if not instances:
        return
    try:
        asyncio.run(_close_all_db_instances())
    except Exception as exc:
        logger.warning("[storage] SQLite shutdown failed: %s", exc)


def _ensure_shutdown_hook_registered() -> None:
    global _shutdown_registered
    if _shutdown_registered:
        return
    atexit.register(_safe_shutdown_db_atexit)
    _shutdown_registered = True


def get_db() -> SQLiteAdapter:
    """Return the SQLite adapter for the current event loop/thread."""

    global _db_instance
    _ensure_shutdown_hook_registered()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    with _db_instance_lock:
        thread_id = threading.get_ident()
        if current_loop is not None:
            instance = _db_instances_by_loop.get(current_loop)
            if instance is None:
                instance = SQLiteAdapter()
                _db_instances_by_loop[current_loop] = instance
        else:
            instance = _db_instances_by_thread.get(thread_id)
            if instance is None:
                instance = SQLiteAdapter()
                _db_instances_by_thread[thread_id] = instance
        _db_instance = instance
        return instance


async def close_db() -> None:
    """Close the SQLite adapter for the current event loop/thread."""

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    with _db_instance_lock:
        thread_id = threading.get_ident()
        instances: list[SQLiteAdapter] = []
        if current_loop is not None:
            instance = _db_instances_by_loop.pop(current_loop, None)
            if instance is not None:
                instances.append(instance)
        else:
            instance = _db_instances_by_thread.pop(thread_id, None)
            if instance is not None:
                instances.append(instance)
        _refresh_legacy_alias_locked()

    for instance in instances:
        await _close_instance(instance)


async def await_with_db_cleanup(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    finally:
        try:
            await asyncio.shield(close_db())
        except Exception as exc:
            logger.warning("[storage] SQLite shutdown failed during cleanup: %s", exc)
        try:
            await asyncio.shield(_flush_cleanup_callbacks())
        except Exception as exc:
            logger.warning("[storage] cleanup callback drain failed: %s", exc)


def run_with_db_cleanup(awaitable: Awaitable[T]) -> T:
    return asyncio.run(await_with_db_cleanup(awaitable))


__all__ = [
    "SQLiteAdapter",
    "get_db",
    "close_db",
    "drain_cleanup_callbacks",
    "await_with_db_cleanup",
    "run_with_db_cleanup",
]
