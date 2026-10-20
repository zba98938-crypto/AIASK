"""Behavioral test: `factory evidence` resolves the authoritative runtime DB.

Previously the default was the legacy shell at
~/.aiask/akshare_mcp.sqlite3 (0 rows). It must now default to
runtime_paths.resolve_sqlite_path() and expose `sqlite_path_source` in the
envelope so a reader can tell which config source won.
"""

from __future__ import annotations

import json

from aiask_agent.factory_cli import cmd_evidence


class _Args:
    def __init__(self, sqlite_path: str = "", legacy_db: bool = False):
        self.sqlite_path = sqlite_path
        self.legacy_db = legacy_db


def test_evidence_default_resolves_to_runtime_db(tmp_path, monkeypatch):
    """No flags, no env vars -> resolver path under ~/.aiask/data/db."""
    monkeypatch.delenv("AIASK_SQLITE_PATH", raising=False)
    monkeypatch.delenv("AKSHARE_MCP_SQLITE_PATH", raising=False)
    # Force the runtime home to a sandbox so the test doesn't touch the user dir.
    monkeypatch.setenv("AIASK_RUNTIME_HOME", str(tmp_path))
    from pathlib import Path
    Path(tmp_path, "data", "db").mkdir(parents=True)
    db = Path(tmp_path, "data", "db", "akshare_mcp.sqlite3")
    db.touch()

    envelope = cmd_evidence(_Args())
    payload = json.loads(envelope.render() if hasattr(envelope, "render") else json.dumps(envelope.data))
    path_source = envelope.data.get("sqlite_path_source") or (payload.get("sqlite_path_source"))
    path_str = envelope.data.get("sqlite_path") or payload.get("sqlite_path")
    assert path_str == str(db), f"expected runtime DB {db}, got {path_str}"
    assert "runtime_paths" in str(path_source), f"source was {path_source}"


def test_evidence_explicit_arg_wins_over_resolver(monkeypatch):
    """--sqlite-path always wins; the envelope labels it as a cli_arg."""
    monkeypatch.delenv("AIASK_SQLITE_PATH", raising=False)
    from pathlib import Path
    target = Path("/tmp/whatever.sqlite3")
    envelope = cmd_evidence(_Args(sqlite_path=str(target)))
    path_source = envelope.data.get("sqlite_path_source")
    assert path_source == "cli_arg"
    assert envelope.data.get("sqlite_path") == str(target)


def test_evidence_env_var_wins_over_resolver(tmp_path, monkeypatch):
    monkeypatch.delenv("AKSHARE_MCP_SQLITE_PATH", raising=False)
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "custom.sqlite3"))
    (tmp_path / "custom.sqlite3").touch()
    envelope = cmd_evidence(_Args())
    assert envelope.data.get("sqlite_path_source") == "AIASK_SQLITE_PATH"


def test_evidence_legacy_flag_points_at_shell():
    """--legacy-db explicitly reads the 0-row historical shell."""
    from pathlib import Path

    class _LegacyArgs:
        sqlite_path = ""
        legacy_db = True

    envelope = cmd_evidence(_LegacyArgs())
    assert envelope.data.get("sqlite_path_source") == "legacy_db_flag"
    assert envelope.data.get("sqlite_path") == str(Path.home() / ".aiask" / "akshare_mcp.sqlite3")
