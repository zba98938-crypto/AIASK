"""The test session must not be able to reach the operator's runtime state.

On 2026-10-29 a plain root ``pytest`` closed 9 real paper positions and rewrote
274 closure snapshots in ``~/.aiask/data/db/akshare_mcp.sqlite3`` because path
resolvers fell back to ``~/.aiask``. These tests pin the isolation that the
conftests install, and the guard that refuses the real database even when a
test bypasses the resolvers.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from aiask_quant_core.runtime_paths import (
    resolve_sqlite_path,
    resolve_task_board_path,
    runtime_home,
)
from aiask_quant_core.testing import hermetic


def _inside(path: Path, root: str) -> bool:
    resolved = os.path.realpath(str(path))
    return resolved == root or resolved.startswith(root + os.sep)


@pytest.fixture
def isolation() -> dict:
    state = hermetic.state()
    assert state["installed"], "conftest did not install the hermetic runtime"
    yield state
    hermetic.clear_refusals()


def test_runtime_paths_resolve_inside_the_test_home(isolation) -> None:
    home = os.path.realpath(isolation["home"])
    assert home != os.path.realpath(isolation["real_home"])
    assert os.path.realpath(str(Path.home())) == home
    for path in (runtime_home(), resolve_sqlite_path(), resolve_task_board_path()):
        assert _inside(path, home), f"{path} escapes the test home {home}"


@pytest.mark.parametrize("subdir", [".aiask/data/db/akshare_mcp.sqlite3", ".aiask-agent/state.sqlite3"])
def test_the_real_runtime_database_cannot_be_opened(isolation, subdir) -> None:
    target = os.path.join(isolation["real_home"], subdir)
    with pytest.raises(RuntimeError, match="hermetic"):
        sqlite3.connect(target)
    with pytest.raises(RuntimeError, match="hermetic"):
        sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    with pytest.raises(RuntimeError, match="hermetic"):
        sqlite3.connect(Path(target))
    # SQLite builds with SQLITE_USE_URI read a file: name as a URI even without uri=True.
    with pytest.raises(RuntimeError, match="hermetic"):
        sqlite3.connect(f"file:{target}?mode=ro")
    with pytest.raises(RuntimeError, match="hermetic"):
        sqlite3.connect(database=target.encode())


def test_dropping_home_does_not_reach_the_passwd_home(isolation, monkeypatch) -> None:
    # patch.dict(os.environ, clear=True) drops HOME; Path.home() then reads the
    # passwd entry, which is the operator's real home even when the session
    # started with HOME pointed elsewhere.
    monkeypatch.delenv("HOME")
    target = Path.home() / ".aiask" / "data" / "db" / "akshare_mcp.sqlite3"
    try:
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    except RuntimeError as exc:
        assert "hermetic" in str(exc)
    else:
        con.close()
        pytest.fail(f"the guard let a test open {target}")


def test_protected_roots_cover_the_inherited_and_the_passwd_home() -> None:
    def roots(*bases: str) -> tuple:
        return tuple(os.path.realpath(os.path.join(b, n)) for b in bases for n in (".aiask", ".aiask-agent"))

    assert hermetic.protected_roots("/decoy/home", "/Users/someone") == roots("/decoy/home", "/Users/someone")
    assert hermetic.protected_roots("/same", "/same") == roots("/same")
    assert hermetic.protected_roots("/only", None) == roots("/only")


def test_a_temporary_database_still_opens(isolation, tmp_path) -> None:
    # Healthy control: the guard must not get in the way of real test databases.
    con = sqlite3.connect(tmp_path / "ok.sqlite3")
    con.execute("create table t (x)")
    con.close()
    sqlite3.connect(":memory:").close()
    sqlite3.connect("file:mem?mode=memory", uri=True).close()


def test_implicit_env_discovery_is_switched_off(isolation) -> None:
    assert os.environ.get("AIASK_LOAD_PROJECT_ENV") == "0"
    assert isolation["discovery_marker"] is True


def test_scrub_removes_inherited_switches_and_overrides(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_LLM_API_KEY", "sk-should-not-survive")
    monkeypatch.setenv("AIASK_SQLITE_PATH", "/somewhere/real.sqlite3")
    monkeypatch.setenv("AIASK_AGENT_MODEL", "operator-model")
    monkeypatch.setenv("AIASK_RUN_CLEAN_INSTALL_E2E", "1")
    snapshot = dict(os.environ)
    try:
        removed = hermetic._scrub_environment()
        remaining = dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
    assert "STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED" in removed
    assert "STRATEGY_LLM_API_KEY" in removed
    assert "AIASK_SQLITE_PATH" in removed
    assert "AIASK_AGENT_MODEL" in removed
    assert "STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED" not in remaining
    # Opt-in test switches are kept.
    assert remaining.get("AIASK_RUN_CLEAN_INSTALL_E2E") == "1"


@pytest.mark.parametrize(
    "database, uri, expected_suffix",
    [
        ("/x/y.sqlite3", False, "/x/y.sqlite3"),
        ("file:/x/y.sqlite3?mode=ro", True, "/x/y.sqlite3"),
        ("file:///x/y%20z.sqlite3", True, "/x/y z.sqlite3"),
        ("file://localhost/x/y.sqlite3", True, "/x/y.sqlite3"),
        (":memory:", False, None),
        ("file:mem?mode=memory", True, None),
    ],
)
def test_target_path_resolution(database, uri, expected_suffix) -> None:
    got = hermetic._target_path(database, uri)
    if expected_suffix is None:
        assert got is None
    else:
        assert got is not None and got.endswith(expected_suffix)
