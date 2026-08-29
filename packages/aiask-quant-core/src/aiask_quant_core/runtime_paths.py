"""Canonical runtime path resolution shared by AIASK packages."""

from __future__ import annotations

import logging
import os
from pathlib import Path


logger = logging.getLogger(__name__)


def runtime_home_with_source() -> tuple[Path, str]:
    """Return the absolute AIASK runtime home and its winning source."""

    candidates = (
        ("AIASK_RUNTIME_HOME", None),
        ("LOCALAPPDATA", "hermes-aiask"),
        ("XDG_STATE_HOME", "hermes-aiask"),
    )
    for name, suffix in candidates:
        raw = str(os.getenv(name) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if suffix:
            path /= suffix
        return path.resolve(), name
    return (Path.home() / ".aiask").resolve(), "HOME"


def runtime_home() -> Path:
    """Return the canonical absolute AIASK runtime home."""

    return runtime_home_with_source()[0]


def runtime_db_path(filename: str) -> Path:
    """Return a database path under the canonical runtime data directory."""

    return runtime_home() / "data" / "db" / filename


def resolve_sqlite_path_with_source(
    filename: str = "akshare_mcp.sqlite3",
) -> tuple[Path, str]:
    """Resolve the primary SQLite path and report the winning config source."""

    aiask_path = str(os.getenv("AIASK_SQLITE_PATH") or "").strip()
    akshare_path = str(os.getenv("AKSHARE_MCP_SQLITE_PATH") or "").strip()
    if aiask_path and akshare_path:
        left = Path(aiask_path).expanduser().resolve()
        right = Path(akshare_path).expanduser().resolve()
        if left != right:
            logger.warning(
                "AIASK_SQLITE_PATH and AKSHARE_MCP_SQLITE_PATH differ; "
                "using AIASK_SQLITE_PATH"
            )

    if aiask_path:
        return Path(aiask_path).expanduser().resolve(), "AIASK_SQLITE_PATH"
    if akshare_path:
        return Path(akshare_path).expanduser().resolve(), "AKSHARE_MCP_SQLITE_PATH"
    return runtime_db_path(filename), "AIASK_RUNTIME_HOME"


def resolve_sqlite_path(filename: str = "akshare_mcp.sqlite3") -> Path:
    """Resolve the primary SQLite path using the canonical precedence."""

    return resolve_sqlite_path_with_source(filename)[0]


def resolve_task_board_path_with_source() -> tuple[Path, str]:
    """Resolve the task-board path and report whether it was explicit."""

    raw = str(os.getenv("STRATEGY_FACTORY_TASK_BOARD_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve(), "STRATEGY_FACTORY_TASK_BOARD_PATH"
    return runtime_db_path("strategy_factory_task_board.sqlite3"), "AIASK_RUNTIME_HOME"


def resolve_task_board_path() -> Path:
    """Resolve the strategy-factory task board beside other runtime databases."""

    return resolve_task_board_path_with_source()[0]


def legacy_task_board_path() -> Path:
    """Return the pre-runtime-layout task-board path used by older releases."""

    return (Path.home() / ".aiask" / "strategy_factory_task_board.sqlite3").resolve()


__all__ = [
    "resolve_sqlite_path",
    "resolve_sqlite_path_with_source",
    "resolve_task_board_path",
    "resolve_task_board_path_with_source",
    "legacy_task_board_path",
    "runtime_db_path",
    "runtime_home",
    "runtime_home_with_source",
]
