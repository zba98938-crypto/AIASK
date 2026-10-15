"""Path helpers for the copied AIASK integration inside Hermes.

The copied AIASK tree should not write runtime state into the source copy by
default. Scripts can import this helper to locate the integration root and the
external runtime home used for databases, logs, temporary files, and caches.
"""

from __future__ import annotations

import os
from pathlib import Path


#: Environment variable naming the conda environment that carries the AIASK
#: packages. AIASK is a conda project: the supported way to hold the runtime
#: dependencies is a conda env, not a hand-built virtualenv.
CONDA_ENV_ENV_VAR = "AIASK_CONDA_ENV"
DEFAULT_CONDA_ENV = "aiask"


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
    """Return the canonical isolated-venv entrypoint, without resolving symlinks.

    This is the *path that would be used if an isolated venv existed*. It is not
    an interpreter lookup: use :func:`resolve_python_interpreter` to find the
    interpreter AIASK should actually run under.
    """

    if os.name == "nt":
        return default_venv_dir() / "Scripts" / "python.exe"
    return default_venv_dir() / "bin" / "python"


def conda_env_name() -> str:
    """Return the name of the conda environment holding the AIASK packages."""

    return str(os.getenv(CONDA_ENV_ENV_VAR) or "").strip() or DEFAULT_CONDA_ENV


def _env_dirs(env_root: Path) -> tuple[str, str]:
    if os.name == "nt":
        return ("Scripts", "python.exe")
    return ("bin", "python")


def _conda_env_roots(name: str) -> list[Path]:
    """Return candidate directories for the conda env ``name``, best first.

    Four independent sources are consulted because a conda install can be
    activated, ad-hoc, or only known to conda's own registry. The order matters:
    the currently activated env is the strongest signal, then ``$CONDA_EXE``,
    then the registry, then the conventional install locations.
    """

    roots: list[Path] = []

    active = str(os.getenv("CONDA_PREFIX") or "").strip()
    if active:
        active_path = Path(active).expanduser()
        # Only trust the activated env when it *is* the AIASK env. Otherwise
        # `base` or an unrelated env would win purely for being active.
        if active_path.name == name:
            roots.append(active_path)

    conda_exe = str(os.getenv("CONDA_EXE") or "").strip()
    if conda_exe:
        roots.append(Path(conda_exe).expanduser().parent.parent / "envs" / name)

    registry = Path.home() / ".conda" / "environments.txt"
    if registry.is_file():
        try:
            for line in registry.read_text(encoding="utf-8", errors="replace").splitlines():
                candidate = line.strip()
                if candidate and Path(candidate).name == name:
                    roots.append(Path(candidate))
        except OSError:
            # An unreadable registry only costs us this one source; the
            # conventional locations below are still tried.
            pass

    for base in (
        "~/miniconda3",
        "~/anaconda3",
        "~/miniforge3",
        "/opt/miniconda3",
        "/opt/anaconda3",
        "/usr/local/miniconda3",
        "/usr/local/anaconda3",
    ):
        roots.append(Path(base).expanduser() / "envs" / name)

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def conda_env_python(name: str | None = None) -> Path | None:
    """Return the interpreter of the named conda env, or ``None`` when absent."""

    env_name = str(name or conda_env_name()).strip() or DEFAULT_CONDA_ENV
    for env_root in _conda_env_roots(env_name):
        candidate = env_root.joinpath(*_env_dirs(env_root))
        if candidate.exists():
            return candidate
    return None


def resolve_python_interpreter() -> tuple[Path, str]:
    """Return the AIASK interpreter and the rule that selected it.

    Precedence, highest first:

    1. ``AIASK_VENV_PYTHON`` — an explicit operator override always wins.
    2. ``<runtime_home>/venv`` — a real isolated venv, when one exists.
    3. the conda env named by ``AIASK_CONDA_ENV`` (default ``aiask``).
    4. the canonical runtime-home venv path, which may not exist. Callers report
       that as missing instead of quietly running under an arbitrary Python.
    """

    configured = str(os.getenv("AIASK_VENV_PYTHON") or "").strip()
    if configured:
        return Path(configured).expanduser(), "AIASK_VENV_PYTHON"

    isolated = default_venv_python()
    if isolated.exists():
        return isolated, "AIASK_RUNTIME_HOME venv"

    conda_python = conda_env_python()
    if conda_python is not None:
        return conda_python, f"conda:{conda_env_name()}"

    return isolated, "AIASK_RUNTIME_HOME venv"


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
