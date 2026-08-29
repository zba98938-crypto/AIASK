from __future__ import annotations

from pathlib import Path

from aiask_quant_core.runtime_paths import (
    resolve_sqlite_path_with_source,
    runtime_db_path,
)


def default_aiask_sqlite_path(filename: str = "akshare_mcp.sqlite3") -> Path:
    return runtime_db_path(filename)


def configured_sqlite_path(filename: str = "akshare_mcp.sqlite3") -> tuple[Path, list[str]]:
    path, source = resolve_sqlite_path_with_source(filename)
    return path, [source]
