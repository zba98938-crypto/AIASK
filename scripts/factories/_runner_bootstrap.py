"""Shared, provider-neutral bootstrap for the factory runners.

Every runner used to hardcode ``packages/akshare-mcp/src`` on ``sys.path`` and
call ``akshare_mcp.env_loader.load_mcp_env()`` directly. That made the runners
structurally dependent on one data provider even though the runtime contract they
drive is provider-neutral — the last place the extraction plan's "root runners
must not go through AKShare" condition was still open.

This module inverts that: it injects whichever packages are actually present and
loads env through whichever provider is installed, reporting what it found. A
runner works with a data provider, with only the standalone set, and in a
standalone repository where the provider was never installed.

Nothing here fabricates capability. If no provider is available the runner still
starts; the runtime contract then reports an honestly empty cycle.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


#: Packages injected onto ``sys.path`` when running from a source tree, in
#: dependency order. Optional ones are skipped silently when absent.
_CORE_PACKAGES: tuple[str, ...] = (
    "aiask-quant-core",
    "strategy-factory",
)

#: Data providers, tried in order. The first present one supplies env loading.
_PROVIDER_PACKAGES: tuple[tuple[str, str], ...] = (
    ("akshare-mcp", "akshare_mcp"),
    ("data-mcp", "aiask_data_mcp"),
)


def project_root(script_path: Path | str) -> Path:
    """The AIASK root. ``AIASK_PROJECT_ROOT`` wins when set."""
    explicit = str(os.getenv("AIASK_PROJECT_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(script_path).resolve().parents[2]


def bootstrap_paths(root: Path) -> dict[str, Any]:
    """Inject present package sources onto ``sys.path``.

    Returns what was injected and what was skipped, so a runner can report its
    own composition rather than assuming it.
    """
    injected: list[str] = []
    skipped: list[str] = []

    candidates = list(_CORE_PACKAGES) + [name for name, _ in _PROVIDER_PACKAGES]
    for package in candidates:
        source = root / "packages" / package / "src"
        if not source.is_dir():
            skipped.append(package)
            continue
        path = str(source)
        if path not in sys.path:
            sys.path.insert(0, path)
        injected.append(package)

    scripts_dir = root / "scripts"
    if scripts_dir.is_dir() and str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    return {"injected": injected, "skipped": skipped}


def load_provider_env(*, override: bool = False) -> dict[str, Any]:
    """Load environment through whichever data provider is installed.

    Tries each known provider in order. Absence is not an error: a standalone
    installation has no provider, and the runner must still start.
    """
    import importlib

    for distribution, module_name in _PROVIDER_PACKAGES:
        try:
            module = importlib.import_module(f"{module_name}.env_loader")
        except ImportError as _swallowed:
            logger.warning("scripts/factories/_runner_bootstrap.py:90: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        loader = getattr(module, "load_mcp_env", None) or getattr(module, "load_env", None)
        if not callable(loader):
            continue
        try:
            loader(override=override)
        except Exception as exc:  # noqa: BLE001 - env loading must not abort a run
            logger.debug("env load via %s failed: %s", module_name, exc)
            continue
        return {"loaded": True, "provider": distribution, "module": module_name}

    return {
        "loaded": False,
        "provider": None,
        "reason": "no_data_provider_installed",
    }


def resolve_sqlite_path(root: Path) -> str:
    """Pick the SQLite path, preferring an explicit environment setting."""
    for key in ("AIASK_SQLITE_PATH", "AKSHARE_MCP_SQLITE_PATH"):
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    try:
        sys.path.insert(0, str(root / "scripts"))
        from _integration_paths import default_sqlite_path  # type: ignore[import-not-found]

        return str(default_sqlite_path())
    except Exception:  # noqa: BLE001 - fall back to the documented default
        return str(Path.home() / ".aiask" / "akshare_mcp.sqlite3")


def bootstrap_runner(script_path: Path | str, *, load_env: bool = True) -> dict[str, Any]:
    """One call a runner makes before touching the runtime contract.

    Returns a composition report: which packages were injected, which provider
    supplied env, and the resolved database path.
    """
    root = project_root(script_path)
    paths = bootstrap_paths(root)
    env = load_provider_env() if load_env else {"loaded": False, "provider": None}

    sqlite_path = resolve_sqlite_path(root)
    os.environ.setdefault("AIASK_SQLITE_PATH", sqlite_path)
    os.environ.setdefault("AKSHARE_MCP_SQLITE_PATH", sqlite_path)

    report = {
        "project_root": str(root),
        "paths": paths,
        "env": env,
        "sqlite_path": sqlite_path,
        "data_provider": env.get("provider"),
    }
    if not env.get("loaded"):
        logger.info(
            "factory runner: no data provider installed; "
            "the runtime contract will report an empty cycle"
        )
    return report


__all__ = [
    "bootstrap_paths",
    "bootstrap_runner",
    "load_provider_env",
    "project_root",
    "resolve_sqlite_path",
]
