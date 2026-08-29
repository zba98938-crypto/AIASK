"""Shared AIASK storage primitives.

This package owns the canonical local SQLite implementation used by Strategy
Factory and MCP hosts. Host packages may re-export these symbols for backwards
compatibility, but the database implementation lives here.
"""

from .sqlite import (
    SQLiteAdapter,
    await_with_db_cleanup,
    close_db,
    drain_cleanup_callbacks,
    get_db,
    run_with_db_cleanup,
)

__all__ = [
    "SQLiteAdapter",
    "get_db",
    "close_db",
    "drain_cleanup_callbacks",
    "await_with_db_cleanup",
    "run_with_db_cleanup",
]
