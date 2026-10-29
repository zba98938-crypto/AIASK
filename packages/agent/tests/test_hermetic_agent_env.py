"""``AIASK_LOAD_PROJECT_ENV=0`` must stop the agent from reading or writing any .env.

``update_project_env_values`` writes a file. With nothing named and guessing
off it used to fall back to ``<project root>/.env`` / ``<cwd>/.env`` — the
operator's real file when tests run from the repository root.

The project root is simulated by pointing the module's ``__file__`` into a
temporary tree with ``AGENT.md`` and ``packages/``, which is what
``discover_project_root`` looks for.
"""

from __future__ import annotations

import os
import sys

import pytest

from aiask_agent import env_config
from aiask_agent.paths import aiask_agent_home

PROBE = "AIASK_TEST_ENV_DISCOVERY_PROBE"
MARKER = "aiask_hermetic_test_session"


def _absent(monkeypatch, *names: str) -> None:
    # setenv first so monkeypatch restores "absent" even if the code under test writes it.
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def _fake_tree(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    module = root / "packages" / "agent" / "src" / "aiask_agent" / "env_config.py"
    module.parent.mkdir(parents=True)
    (root / "AGENT.md").write_text("fake project root\n", encoding="utf-8")
    (root / ".env").write_text(f"{PROBE}=from-root\nAIASK_AGENT_MODEL=from-root\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(f"{PROBE}=from-cwd\nAIASK_AGENT_MODEL=from-cwd\n", encoding="utf-8")
    monkeypatch.setattr(env_config, "__file__", str(module))
    monkeypatch.setattr(env_config, "_LOADED_ENV_PATH", env_config._LOADED_ENV_PATH)
    monkeypatch.setattr(env_config, "_LOADED_ENV_SOURCE", env_config._LOADED_ENV_SOURCE)
    monkeypatch.chdir(cwd)
    _absent(
        monkeypatch,
        "AIASK_AGENT_ENV_FILE",
        "AIASK_ENV_FILE",
        "AIASK_AGENT_DISABLE_PROJECT_ENV",
        PROBE,
        "AIASK_AGENT_MODEL",
    )
    return root, cwd


def test_agent_home_is_inside_the_test_home() -> None:
    home = os.path.realpath(os.environ["AIASK_TEST_HERMETIC_HOME"])
    assert os.path.realpath(str(aiask_agent_home())).startswith(home + os.sep)


def test_switch_off_ignores_cwd_and_project_root(tmp_path, monkeypatch) -> None:
    _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    # The agent-level opt-in does not override the shared switch.
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "1")
    before = dict(os.environ)
    assert env_config.project_env_candidates() == []
    assert env_config.load_project_env() is None
    assert env_config.load_project_env(force=True) is None
    assert dict(os.environ) == before


def test_switch_off_also_ignores_files_named_by_env_vars(tmp_path, monkeypatch) -> None:
    root, _ = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "1")
    for name in ("AIASK_AGENT_ENV_FILE", "AIASK_ENV_FILE"):
        monkeypatch.setenv(name, str(root / ".env"))
        before = dict(os.environ)
        assert env_config.load_project_env(force=True) is None, name
        assert dict(os.environ) == before
        monkeypatch.delenv(name)


def test_switch_off_still_loads_an_explicit_path_argument(tmp_path, monkeypatch) -> None:
    root, _ = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    loaded = env_config.load_project_env(explicit_path=str(root / ".env"))
    assert loaded is not None and loaded.resolve() == (root / ".env").resolve()
    assert os.environ[PROBE] == "from-root"


def test_a_write_with_the_switch_off_is_refused_and_touches_nothing(tmp_path, monkeypatch) -> None:
    root, cwd = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    monkeypatch.setenv("AIASK_AGENT_ENV_FILE", str(root / ".env"))
    before = {path: path.read_bytes() for path in (root / ".env", cwd / ".env")}
    with pytest.raises(ValueError, match="AIASK_LOAD_PROJECT_ENV"):
        env_config.update_project_env_values({"AIASK_AGENT_MODEL": "x"})
    assert {path: path.read_bytes() for path in before} == before
    assert "AIASK_AGENT_MODEL" not in os.environ


def test_healthy_control_without_switch_or_marker_finds_the_fake_root(tmp_path, monkeypatch) -> None:
    # Proves the tests above are not vacuous: the same tree is discovered once
    # nothing turns discovery off.
    root, cwd = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.delitem(sys.modules, MARKER, raising=False)
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "1")
    found = [(path.resolve(), source) for path, source in env_config.project_env_candidates()]
    assert found == [((root / ".env").resolve(), "project_root"), ((cwd / ".env").resolve(), "cwd")]
    loaded = env_config.load_project_env()
    assert loaded is not None and loaded.resolve() == (root / ".env").resolve()
    assert os.environ[PROBE] == "from-root"


def test_marker_does_not_block_a_named_file(tmp_path, monkeypatch) -> None:
    named = tmp_path / "named.env"
    named.write_text("AIASK_AGENT_MODEL=named-model\n", encoding="utf-8")
    _fake_tree(tmp_path, monkeypatch)
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.setenv("AIASK_AGENT_LOAD_PROJECT_ENV", "1")
    monkeypatch.setenv("AIASK_AGENT_ENV_FILE", str(named))
    assert [path for path, _ in env_config.project_env_candidates()] == [named]
    assert env_config.load_project_env() == named
    assert os.environ["AIASK_AGENT_MODEL"] == "named-model"


def test_hermetic_scrub_covers_every_agent_llm_key_family() -> None:
    # A key family added to env_config but not to the hermetic scrub would be
    # inherited from the operator's shell into every test session.
    from aiask_quant_core.testing import hermetic

    names = sorted(env_config.LLM_ENV_KEYS) + [prefix + "PROBE" for prefix in env_config.LLM_ENV_PREFIXES]
    assert [name for name in names if not hermetic.scrubbed(name)] == []
