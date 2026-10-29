"""``AIASK_LOAD_PROJECT_ENV=0`` must stop ``load_strategy_llm_env`` from reading any .env.

The project root is simulated by pointing the module's ``__file__`` into a
temporary tree that has ``packages/`` and a ``.env``, which is what the loader
looks for when it walks up.
"""

from __future__ import annotations

import os
import sys

from strategy_factory.infrastructure import env_loader

MARKER = "aiask_hermetic_test_session"


def _absent(monkeypatch, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def _fake_tree(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    module = root / "packages" / "strategy-factory" / "src" / "strategy_factory" / "infrastructure" / "env_loader.py"
    module.parent.mkdir(parents=True)
    (root / ".env").write_text("STRATEGY_LLM_MODEL=from-root\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("STRATEGY_LLM_MODEL=from-cwd\n", encoding="utf-8")
    monkeypatch.setattr(env_loader, "__file__", str(module))
    monkeypatch.chdir(cwd)
    _absent(monkeypatch, "STRATEGY_FACTORY_ENV_FILE", "AIASK_ENV_FILE", "STRATEGY_LLM_MODEL")
    return root, cwd


def test_switch_off_ignores_cwd_and_project_root(tmp_path, monkeypatch) -> None:
    _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    before = dict(os.environ)
    assert env_loader._project_root_candidates() == []
    assert env_loader.load_strategy_llm_env() is None
    assert dict(os.environ) == before


def test_switch_off_also_ignores_files_named_by_env_vars(tmp_path, monkeypatch) -> None:
    root, _ = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    for name in ("STRATEGY_FACTORY_ENV_FILE", "AIASK_ENV_FILE"):
        monkeypatch.setenv(name, str(root / ".env"))
        before = dict(os.environ)
        assert env_loader.load_strategy_llm_env() is None, name
        assert dict(os.environ) == before
        monkeypatch.delenv(name)


def test_switch_off_still_loads_an_explicit_path_argument(tmp_path, monkeypatch) -> None:
    root, _ = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.setenv("AIASK_LOAD_PROJECT_ENV", "0")
    assert env_loader.load_strategy_llm_env(explicit_path=str(root / ".env")) == root / ".env"
    assert os.environ["STRATEGY_LLM_MODEL"] == "from-root"


def test_healthy_control_without_switch_or_marker_finds_the_fake_root(tmp_path, monkeypatch) -> None:
    root, cwd = _fake_tree(tmp_path, monkeypatch)
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.delitem(sys.modules, MARKER, raising=False)
    found = [p.resolve() for p in env_loader._project_root_candidates()]
    assert found == [(root / ".env").resolve(), (cwd / ".env").resolve()]
    assert env_loader.load_strategy_llm_env() == root / ".env"
    assert os.environ["STRATEGY_LLM_MODEL"] == "from-root"


def test_hermetic_scrub_covers_every_llm_key_family() -> None:
    from aiask_quant_core.testing import hermetic

    names = sorted(env_loader.LLM_ENV_KEYS) + [prefix + "PROBE" for prefix in env_loader.LLM_ENV_PREFIXES]
    assert [name for name in names if not hermetic.scrubbed(name)] == []


def test_marker_does_not_block_a_named_file(tmp_path, monkeypatch) -> None:
    named = tmp_path / "named.env"
    named.write_text("STRATEGY_LLM_MODEL=named-model\n", encoding="utf-8")
    _fake_tree(tmp_path, monkeypatch)
    monkeypatch.delenv("AIASK_LOAD_PROJECT_ENV", raising=False)
    monkeypatch.setenv("STRATEGY_FACTORY_ENV_FILE", str(named))
    assert env_loader._project_root_candidates() == [named]
    assert env_loader.load_strategy_llm_env() == named
    assert os.environ["STRATEGY_LLM_MODEL"] == "named-model"
