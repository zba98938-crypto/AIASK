"""Path helpers for the copied AIASK integration inside Hermes.

The copied AIASK tree should not write runtime state into the source copy by
default. Scripts can import this helper to locate the integration root and the
external runtime home used for databases, logs, temporary files, and caches.
"""

from __future__ import annotations

import os
from pathlib import Path


def aiask_root() -> Path:
    """Return the copied AIASK integration root."""

    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # scripts/_integration_paths.py -> <aiask root>. This tree is standalone;
    # it used to be nested at <repo>/integrations/aiask.
    return Path(__file__).resolve().parents[1]


def runtime_home() -> Path:
    """Return the external runtime home for this copied integration."""

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
        return path.resolve()
    return (Path.home() / ".aiask").resolve()


def default_venv_dir() -> Path:
    """Return the single canonical isolated-venv location."""

    return runtime_home() / "venv"


def default_venv_python() -> Path:
    """Return the canonical AIASK Python entrypoint without resolving symlinks."""

    if os.name == "nt":
        return default_venv_dir() / "Scripts" / "python.exe"
    return default_venv_dir() / "bin" / "python"


def default_sqlite_path() -> Path:
    """Return the default SQLite path for this copied integration."""

    override = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return runtime_home() / "data" / "db" / "akshare_mcp.sqlite3"


def default_task_board_path() -> Path:
    """Return the default Strategy Factory task board path."""

    override = os.getenv("STRATEGY_FACTORY_TASK_BOARD_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return runtime_home() / "data" / "db" / "strategy_factory_task_board.sqlite3"


def ensure_runtime_dirs() -> None:
    """Create external runtime directories used by copied scripts."""

    root = runtime_home()
    for rel in ("data/db", "logs", "tmp", "cache"):
        (root / rel).mkdir(parents=True, exist_ok=True)
