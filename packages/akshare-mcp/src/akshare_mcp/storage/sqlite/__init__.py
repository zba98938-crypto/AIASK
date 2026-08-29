"""Compatibility shell for the canonical shared SQLite storage package."""

from __future__ import annotations

import importlib
import pkgutil
import sys
from types import ModuleType

import aiask_quant_core.storage.sqlite as _shared_sqlite
from aiask_quant_core.storage.sqlite import (
    SQLiteAdapter,
    await_with_db_cleanup,
    close_db,
    drain_cleanup_callbacks,
    get_db,
    run_with_db_cleanup,
)


def _alias_module(shared_name: str, module: ModuleType) -> None:
    legacy_name = shared_name.replace(
        "aiask_quant_core.storage.sqlite",
        __name__,
        1,
    )
    sys.modules.setdefault(legacy_name, module)


def _install_legacy_module_aliases() -> None:
    _alias_module(_shared_sqlite.__name__, _shared_sqlite)
    shared_path = getattr(_shared_sqlite, "__path__", None)
    if not shared_path:
        return
    prefix = f"{_shared_sqlite.__name__}."
    for module_info in pkgutil.walk_packages(shared_path, prefix):
        module = importlib.import_module(module_info.name)
        _alias_module(module_info.name, module)


_install_legacy_module_aliases()

__all__ = [
    "SQLiteAdapter",
    "get_db",
    "close_db",
    "drain_cleanup_callbacks",
    "await_with_db_cleanup",
    "run_with_db_cleanup",
]
