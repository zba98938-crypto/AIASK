"""Launch AIASK MCP modules through the configured AIASK interpreter.

This wrapper is stdlib-only. A host can call it with any Python, and it re-execs
the AIASK interpreter before importing heavy finance deps. Resolution order is
``AIASK_VENV_PYTHON`` -> an isolated venv under the runtime home -> the conda
env named by ``AIASK_CONDA_ENV`` (default ``aiask``). See
``_integration_paths.resolve_python_interpreter``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from _integration_paths import (
    default_sqlite_path,
    ensure_runtime_dirs,
    resolve_python_interpreter,
    runtime_home,
)


MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 14)


def _integration_root() -> Path:
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _current_python_supported() -> bool:
    return MIN_PYTHON <= sys.version_info[:2] < MAX_PYTHON


def _python_label() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _require_supported_python() -> None:
    if _current_python_supported():
        return
    raise SystemExit(
        "AIASK MCP requires Python >=3.12,<3.14. "
        f"Current interpreter is Python {_python_label()} at {sys.executable}. "
        "Set AIASK_VENV_PYTHON to the AIASK interpreter, or create the conda "
        "env named by AIASK_CONDA_ENV."
    )


def _allow_current_python() -> bool:
    return _truthy(os.getenv("AIASK_ALLOW_CURRENT_PYTHON"))


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: run_mcp_stdio.py <module> [args...]")

    root = _integration_root()
    module = sys.argv[1]
    module_args = sys.argv[2:]

    os.environ.setdefault("AIASK_PROJECT_ROOT", str(root))
    os.environ.setdefault("AIASK_RUNTIME_HOME", str(runtime_home()))
    ensure_runtime_dirs()
    sqlite_path = default_sqlite_path()
    os.environ.setdefault("AIASK_SQLITE_PATH", str(sqlite_path))
    os.environ.setdefault("AKSHARE_MCP_SQLITE_PATH", os.environ["AIASK_SQLITE_PATH"])

    venv_python, source = resolve_python_interpreter()
    # Do not resolve either path: venv entrypoints commonly symlink to the base
    # interpreter, but invoking the symlink is what activates pyvenv.cfg and the
    # isolated site-packages. Comparing resolved targets silently skips re-exec.
    current = Path(os.path.abspath(sys.executable))
    expected = Path(os.path.abspath(str(venv_python)))
    if venv_python.exists() and current != expected:
        os.execv(str(venv_python), [str(venv_python), "-m", module, *module_args])
    if not venv_python.exists() and not _allow_current_python():
        raise SystemExit(
            f"{source} not found: {venv_python}. "
            "Set AIASK_VENV_PYTHON, create the conda env named by "
            "AIASK_CONDA_ENV, or create the isolated runtime venv. "
            "For local development only, set AIASK_ALLOW_CURRENT_PYTHON=1 with Python >=3.12,<3.14."
        )
    _require_supported_python()

    # Explicit development mode: run in the current supported interpreter with
    # source paths added. Heavy dependencies must already be installed.
    for rel in (
        "packages/aiask-quant-core/src",
        "packages/strategy-factory/src",
        "packages/akshare-mcp/src",
        "packages/finance-mcp-servers/src",
        "packages/agent/src",
    ):
        path = str(root / rel)
        if path not in sys.path:
            sys.path.insert(0, path)

    sys.argv = [module, *module_args]
    import runpy

    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
