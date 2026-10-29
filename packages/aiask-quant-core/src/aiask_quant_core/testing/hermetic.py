"""Keep every pytest run away from the operator's real runtime state.

Why this exists
---------------
A plain ``python -m pytest`` from the repository root used to write into the
authoritative runtime store.  Path resolvers fall back to ``~/.aiask`` when
``AIASK_SQLITE_PATH`` is unset, and only one fixture in one package redirected
it.  On 2026-10-29 such a run closed 9 real paper positions, wrote sell fills,
rewrote 274 closure snapshots with a wall-clock ``as_of_date`` and inserted
test fixtures into ``strategy_artifacts``.  The same run imported
``akshare_mcp.server``, whose module-level ``load_mcp_env()`` found the repo
``.env`` through ``__file__`` and injected the real LLM key and six
``STRATEGY_FACTORY_*_ENABLED`` switches into the whole process, which is why
three strategy-factory tests failed only in root runs.

What ``install()`` does
-----------------------
It must run before any test module is imported, so every ``conftest.py`` calls
it at import time (they load this file by path, so no package needs another
package on its ``pythonpath``).

* ``HOME``, ``AIASK_RUNTIME_HOME`` and ``AIASK_AGENT_HOME`` point into a fresh
  temporary directory.  ``PYTHONUSERBASE`` keeps the real user base so child
  interpreters still find user-site packages.
* Implicit ``.env`` discovery is switched off with ``AIASK_LOAD_PROJECT_ENV=0``
  and with a ``sys.modules`` marker that survives ``patch.dict(os.environ,
  clear=True)``.
* Inherited path overrides, every key named in the repo ``.env`` and the
  provider/feature-switch families are removed from the process environment.
* ``scripts_local_bootstrap`` is dropped from ``PYTHONPATH`` so child processes
  are not redirected to the sandbox snapshot database.
* ``sqlite3.connect`` refuses any database under the real ``~/.aiask`` or
  ``~/.aiask-agent``, read-only opens included: a unit test must not depend on
  the live store.

Limits.  The HOME redirect is the main defence; the connect guard only raises
the alarm.  It cannot see ``from sqlite3 import connect`` bound before
``install()``.  A test that runs ``patch.dict(os.environ, clear=True)`` drops
``HOME`` for that block, so ``Path.home()`` falls back to the passwd entry; the
guard covers the passwd home's runtime roots for that reason, but files other
than databases written there are not intercepted.  The end-to-end check
therefore fingerprints every file under the real runtime roots, not only the
databases.

The module uses only the standard library and stays importable on Python 3.10
(``finance-mcp-servers`` declares ``>=3.10``).
"""

from __future__ import annotations

import atexit
import os
import sqlite3
import sys
import tempfile
import traceback
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

SENTINEL_ENV = "AIASK_TEST_HERMETIC_HOME"
REAL_HOME_ENV = "AIASK_TEST_REAL_HOME"
KEEP_HOME_ENV = "AIASK_TEST_KEEP_HOME"
DISCOVERY_SWITCH_ENV = "AIASK_LOAD_PROJECT_ENV"
AGENT_DISCOVERY_SWITCH_ENV = "AIASK_AGENT_LOAD_PROJECT_ENV"
# Loaders check for this key in ``sys.modules``; unlike an environment variable
# it survives tests that clear ``os.environ``.
DISCOVERY_MARKER_MODULE = "aiask_hermetic_test_session"
TEMP_PREFIX = "aiask-test-home-"
PROTECTED_DIR_NAMES = (".aiask", ".aiask-agent")

PATH_OVERRIDE_KEYS = (
    "AIASK_SQLITE_PATH",
    "AKSHARE_MCP_SQLITE_PATH",
    "STRATEGY_FACTORY_TASK_BOARD_PATH",
    "AIASK_AGENT_STATE_DB",
    "AIASK_AGENT_INTENT_DB",
    "AIASK_AGENT_QUANT_RESEARCH_DB",
    "AKSHARE_MCP_ENV",
    "STRATEGY_FACTORY_ENV_FILE",
    "AIASK_ENV_FILE",
    "AIASK_AGENT_ENV_FILE",
    "LOCALAPPDATA",
    "XDG_STATE_HOME",
)

# The feature-switch families plus every LLM key family the three env loaders
# know (akshare_mcp/strategy_factory ``LLM_ENV_PREFIXES``, aiask_agent
# ``LLM_ENV_KEYS``/``LLM_ENV_PREFIXES``). Each loader's tests assert their table
# is covered, so a prefix added there cannot silently leak into test sessions.
SCRUB_PREFIXES = (
    "STRATEGY_FACTORY_",
    "INCUBATION_FACTORY_",
    "STRATEGY_LLM_",
    "STRATEGY_EMBEDDING_",
    "STRATEGY_PIPELINE_STAGE_",
    "FACTOR_LLM_",
    "FINANCIAL_SEMANTIC_",
    "OPENAI_",
    "ANTHROPIC_",
    "DEEPSEEK_",
    "DASHSCOPE_",
    "MOONSHOT_",
    "QWEN_",
    "ZHIPU_",
    "AIASK_AGENT_MODEL",
    "AIASK_AGENT_IMAGE_",
    "AIASK_AGENT_VISION_",
    "AIASK_AGENT_TTS_",
    "AIASK_AGENT_PROMPT_CACHE_",
    "AIASK_AGENT_CONTEXT_MAX_TOKENS",
    "AIASK_AGENT_MAX_TOKENS",
    "AIASK_MOA_",
    "AIASK_VIDEO_",
    "AIASK_VOICE_",
)

# Opt-in switches that tests read on purpose; never scrubbed.
KEEP_PREFIXES = ("AIASK_RUN_", "AIASK_TEST_", "AIASK_RL_LIVE_ENVIRONMENT")

_OWNER_PID: Optional[int] = None


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _repo_root() -> Optional[Path]:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "packages").is_dir() and (candidate / "pytest.ini").is_file():
            return candidate
    return None


def _dotenv_key_names(path: Path) -> List[str]:
    """Return the key names in a dotenv file. Values are discarded, never returned or stored."""
    names: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return names
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name = line.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def scrubbed(name: str, dotenv_names: Tuple[str, ...] = ()) -> bool:
    """Whether ``install()`` removes an inherited variable of this name."""
    if name.startswith(KEEP_PREFIXES):
        return False
    return name in PATH_OVERRIDE_KEYS or name in dotenv_names or name.startswith(SCRUB_PREFIXES)


def _scrub_environment() -> List[str]:
    removed: List[str] = []
    dotenv_names: set = set()
    roots = [_repo_root(), Path.cwd()]
    for root in roots:
        if root is not None:
            dotenv_names.update(_dotenv_key_names(root / ".env"))
    frozen = tuple(dotenv_names)
    for name in list(os.environ):
        if scrubbed(name, frozen):
            os.environ.pop(name, None)
            removed.append(name)
    return sorted(removed)


def _scrub_pythonpath() -> None:
    raw = os.environ.get("PYTHONPATH")
    if not raw:
        return
    kept = [part for part in raw.split(os.pathsep) if part and "scripts_local_bootstrap" not in part]
    if kept:
        os.environ["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        os.environ.pop("PYTHONPATH", None)


def _database_text(database: Any) -> str:
    if hasattr(database, "__fspath__"):
        database = os.fspath(database)
    if isinstance(database, bytes):
        return database.decode("utf-8", "replace")
    return str(database)


def _target_path(database: Any, uri: bool) -> Optional[str]:
    """Resolve what file a ``sqlite3.connect`` call would open, or None for in-memory."""
    database = _database_text(database)
    if database in {"", ":memory:"}:
        return None
    if uri and database.startswith("file:"):
        rest = database[len("file:"):]
        rest, _, query = rest.partition("?")
        rest = rest.partition("#")[0]
        if rest.startswith("//"):
            authority, _, tail = rest[2:].partition("/")
            if authority not in {"", "localhost"}:
                return None
            rest = "/" + tail
        rest = unquote(rest)
        if not rest or rest == ":memory:" or "mode=memory" in query:
            return None
        return _realpath(rest)
    return _realpath(database)


def _candidate_targets(database: Any, uri: bool) -> List[Optional[str]]:
    """Every file a connect call could open. A ``file:`` name is checked both
    ways: SQLite builds with ``SQLITE_USE_URI`` parse URIs even when ``uri=False``."""
    text = _database_text(database)
    targets = [_target_path(text, uri)]
    if not uri and text.startswith("file:"):
        targets.append(_target_path(text, True))
    return targets


def _is_protected(target: Optional[str], roots: Tuple[str, ...]) -> bool:
    if target is None:
        return False
    return any(target == root or target.startswith(root + os.sep) for root in roots)


def _install_sqlite_guard(roots: Tuple[str, ...]) -> None:
    current = sqlite3.connect
    if getattr(current, "_aiask_hermetic_guard", False):
        current._aiask_protected_roots = roots  # type: ignore[attr-defined]
        return
    original = current
    refusals: List[Dict[str, str]] = []

    def guarded_connect(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        # connect(database, timeout, detect_types, isolation_level,
        #         check_same_thread, factory, cached_statements, uri)
        uri = bool(kwargs.get("uri", args[6] if len(args) > 6 else False))
        roots_now = guarded_connect._aiask_protected_roots  # type: ignore[attr-defined]
        for target in _candidate_targets(database, uri):
            if _is_protected(target, roots_now):
                stack = "".join(traceback.format_stack(limit=12)[:-1])
                refusals.append({"target": str(target), "stack": stack})
                raise RuntimeError(
                    "hermetic test session refused to open the real runtime database "
                    f"{target}; use tmp_path or the redirected runtime home"
                )
        return original(database, *args, **kwargs)

    guarded_connect._aiask_hermetic_guard = True  # type: ignore[attr-defined]
    guarded_connect._aiask_protected_roots = roots  # type: ignore[attr-defined]
    guarded_connect._aiask_refusals = refusals  # type: ignore[attr-defined]
    guarded_connect._aiask_original = original  # type: ignore[attr-defined]
    sqlite3.connect = guarded_connect  # type: ignore[assignment]
    try:
        import sqlite3.dbapi2 as dbapi2

        dbapi2.connect = guarded_connect  # type: ignore[assignment]
    except ImportError:  # pragma: no cover - dbapi2 always exists in CPython
        pass


def refusals() -> List[Dict[str, str]]:
    """Every connect call the guard refused in this process (target and stack)."""
    return list(getattr(sqlite3.connect, "_aiask_refusals", []))


def clear_refusals() -> None:
    """Forget refusals a test provoked on purpose, so the exit report shows only real leaks."""
    store = getattr(sqlite3.connect, "_aiask_refusals", None)
    if isinstance(store, list):
        store.clear()


def _report_refusals() -> None:
    seen = refusals()
    if not seen:
        return
    sys.stderr.write(
        f"\n[aiask hermetic] refused {len(seen)} attempt(s) to open the real runtime database:\n"
    )
    for item in seen[:5]:
        sys.stderr.write(f"  {item['target']}\n{item['stack']}\n")


def _cleanup(path: str) -> None:
    if os.getpid() != _OWNER_PID or _truthy(os.environ.get(KEEP_HOME_ENV)):
        return
    resolved = _realpath(path)
    temp_root = _realpath(tempfile.gettempdir())
    if not os.path.basename(resolved).startswith(TEMP_PREFIX):
        return
    if not resolved.startswith(temp_root + os.sep):
        return
    import shutil

    shutil.rmtree(resolved, ignore_errors=True)


def _adoptable(path: Optional[str]) -> bool:
    if not path or not os.path.isdir(path):
        return False
    resolved = _realpath(path)
    return os.path.basename(resolved).startswith(TEMP_PREFIX) and resolved.startswith(
        _realpath(tempfile.gettempdir()) + os.sep
    )


def _passwd_home() -> Optional[str]:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):  # pragma: no cover - non-POSIX
        return None


def _guess_real_home() -> str:
    raw = os.environ.get("HOME")
    if raw:
        return raw
    return _passwd_home() or os.path.expanduser("~")


def protected_roots(real_home: str, passwd_home: Optional[str]) -> Tuple[str, ...]:
    """Runtime roots the guard refuses: under the inherited HOME and under the passwd home.

    They differ when the session started with HOME pointed elsewhere (the decoy
    check does this). The passwd home still matters: a test that runs
    ``patch.dict(os.environ, clear=True)`` drops HOME, and ``Path.home()`` then
    falls back to the passwd entry.
    """
    roots: List[str] = []
    for base in (real_home, passwd_home):
        if not base:
            continue
        for name in PROTECTED_DIR_NAMES:
            root = _realpath(os.path.join(base, name))
            if root not in roots:
                roots.append(root)
    return tuple(roots)


def install() -> Dict[str, Any]:
    """Isolate this interpreter. Idempotent; child processes adopt the parent's home."""
    global _OWNER_PID
    existing = os.environ.get(SENTINEL_ENV)
    created_here = False
    if _adoptable(existing):
        home = _realpath(str(existing))
        real_home = os.environ.get(REAL_HOME_ENV) or _guess_real_home()
        _scrub_pythonpath()
    else:
        real_home = _guess_real_home()
        os.environ.setdefault("PYTHONUSERBASE", _real_userbase(real_home))
        _scrub_environment()
        _scrub_pythonpath()
        home = _realpath(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        created_here = True
        _OWNER_PID = os.getpid()
    os.environ[REAL_HOME_ENV] = real_home
    os.environ[SENTINEL_ENV] = home
    os.environ["HOME"] = home
    os.environ["AIASK_RUNTIME_HOME"] = os.path.join(home, ".aiask")
    os.environ["AIASK_AGENT_HOME"] = os.path.join(home, ".aiask-agent")
    os.environ[DISCOVERY_SWITCH_ENV] = "0"
    os.environ[AGENT_DISCOVERY_SWITCH_ENV] = "0"
    sys.modules.setdefault(DISCOVERY_MARKER_MODULE, types.ModuleType(DISCOVERY_MARKER_MODULE))
    _install_sqlite_guard(protected_roots(real_home, _passwd_home()))
    if created_here:
        atexit.register(_cleanup, home)
        atexit.register(_report_refusals)
    return state()


def _real_userbase(real_home: str) -> str:
    try:
        import site

        base = site.getuserbase()
        if base:
            return base
    except Exception:  # pragma: no cover - site is always importable
        pass
    return os.path.join(real_home, ".local")


def state() -> Dict[str, Any]:
    """What the current process is isolated to. Read from the environment so every module instance agrees."""
    home = os.environ.get(SENTINEL_ENV)
    real_home = os.environ.get(REAL_HOME_ENV)
    return {
        "installed": bool(home) and getattr(sqlite3.connect, "_aiask_hermetic_guard", False),
        "home": home,
        "real_home": real_home,
        "protected_roots": list(getattr(sqlite3.connect, "_aiask_protected_roots", ())),
        "discovery_marker": DISCOVERY_MARKER_MODULE in sys.modules,
    }


__all__ = [
    "DISCOVERY_MARKER_MODULE",
    "DISCOVERY_SWITCH_ENV",
    "SCRUB_PREFIXES",
    "clear_refusals",
    "install",
    "protected_roots",
    "refusals",
    "scrubbed",
    "state",
]
