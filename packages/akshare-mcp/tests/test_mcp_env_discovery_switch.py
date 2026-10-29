"""``AIASK_LOAD_PROJECT_ENV=0`` must stop ``load_mcp_env`` from reading any .env.

``akshare_mcp/server.py`` calls ``load_mcp_env()`` at import time. It found the
repo .env through ``__file__`` regardless of cwd, so importing the server in a
test injected the real LLM key and six STRATEGY_FACTORY_*_ENABLED switches
into the whole pytest process.

The project root is simulated by pointing the module's ``__file__`` into a
temporary tree, so the real discovery code runs against fake files and the
operator's .env is never in reach.
"""

from __future__ import annotations

import os
import sys

from akshare_mcp import env_loader

PROBE = "AIASK_TEST_ENV_DISCOVERY_PROBE"
MARKER = "aiask_hermetic_test_session"


def _absent(monkeypatch, *names: str) -> None:
    # setenv first so monkeypatch restores "absent" even if the code under test writes it.
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def _fake_tree(tmp_path, monkeypatch):
    """A cwd and a project root, each with its own .env, and the module looking at that root."""
    root = tmp_path / "repo"
    module = root / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "env_loader.py"
    module.parent.mkdir(parents=True)
    (root / ".env").write_text(f"{PROBE}=from-root\nSTRATEGY_LLM_MODEL=from-root\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(f"{PROBE}=from-cwd\nSTRATEGY_LLM_MODEL=from-cwd\n", encoding="utf-8")
    monkeypatch.setattr(env_loader, "__file__", str(module))
    monkeypatch.chdir(cwd)
    _absent(monkeypatch, "AKSHARE_MCP_ENV", PROBE, "STRATEGY_LLM_MODEL")
    return root, cwd


def test_switch_off_ignores_cwd_and_project_root(tmp_path, monkeypatch) -> None:
    _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    before = dict(os.environ)
    assert env_loader.get_mcp_env_candidates() == []
    assert env_loader.load_mcp_env() is None
    assert env_loader.load_mcp_llm_env() is None
    assert dict(os.environ) == before


def test_switch_off_also_ignores_a_file_named_by_akshare_mcp_env(tmp_path, monkeypatch) -> None:
    root, _ = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AKSHARE_MCP_ENV", str(root / ".env"))
    before = dict(os.environ)
    assert env_loader.load_mcp_env() is None
    assert dict(os.environ) == before


def test_switch_off_still_loads_an_explicit_path_argument(tmp_path, monkeypatch) -> None:
    root, _ = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    assert env_loader.load_mcp_env(explicit_path=str(root / ".env")) == root / ".env"
    assert os.environ[PROBE] == "from-root"


def test_healthy_control_without_switch_or_marker_finds_the_fake_files(tmp_path, monkeypatch) -> None:
    # Proves the two tests above are not vacuous: the same tree is discovered
    # once nothing turns discovery off.
    root, cwd = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.delitem(sys.modules, MARKER, raising=False)
    found = [p.resolve() for p in env_loader.get_mcp_env_candidates()]
    assert found == [(cwd / ".env").resolve(), (root / ".env").resolve()]
    assert env_loader.load_mcp_env() == cwd / ".env"
    assert os.environ[PROBE] == "from-cwd"


def test_marker_alone_keeps_guessing_off_when_environ_is_cleared(tmp_path, monkeypatch) -> None:
    # Tests that patch.dict(os.environ, clear=True) remove the switch; the
    # sys.modules marker must still stop guessing.
    _fake_tree(tmp_path, monkeypatch)
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    assert MARKER in sys.modules
    assert env_loader.implicit_env_discovery_disabled() is True
    assert env_loader.get_mcp_env_candidates() == []


def test_hermetic_scrub_covers_every_llm_key_family() -> None:
    # A key family added here but not to the hermetic scrub would be inherited
    # from the operator's shell into every test session.
    from aiask_quant_core.testing import hermetic

    names = sorted(env_loader.LLM_ENV_KEYS) + [prefix + "PROBE" for prefix in env_loader.LLM_ENV_PREFIXES]
    assert [name for name in names if not hermetic.scrubbed(name)] == []


def test_marker_does_not_block_a_file_named_by_akshare_mcp_env(tmp_path, monkeypatch) -> None:
    named = tmp_path / "named.env"
    named.write_text(f"{PROBE}=named\n", encoding="utf-8")
    _fake_tree(tmp_path, monkeypatch)
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.setenv("AKSHARE_MCP_ENV", str(named))
    assert env_loader.get_mcp_env_candidates() == [named]
    assert env_loader.load_mcp_env() == named
    assert os.environ[PROBE] == "named"
