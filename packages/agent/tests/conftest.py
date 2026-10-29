from __future__ import annotations

import importlib.util as _hermetic_util
import sys as _hermetic_sys
from pathlib import Path as _HermeticPath


def _install_hermetic_runtime() -> None:
    """Isolate the session from ~/.aiask, ~/.aiask-agent and the repo .env (see hermetic.py)."""
    name = "_aiask_test_hermetic"
    module = _hermetic_sys.modules.get(name)
    if module is None:
        path = _HermeticPath(__file__).resolve().parents[2] / "aiask-quant-core/src/aiask_quant_core/testing/hermetic.py"
        spec = _hermetic_util.spec_from_file_location(name, path)
        module = _hermetic_util.module_from_spec(spec)
        _hermetic_sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            _hermetic_sys.modules.pop(name, None)
            raise
    module.install()


_install_hermetic_runtime()

import os

import pytest


os.environ["AIASK_AGENT_LOAD_PROJECT_ENV"] = "0"
MODE_ENV_KEYS = (
    "AIASK_AGENT_TOOLSET",
    "AIASK_AGENT_ENABLE_GENERAL_TOOLS",
    "AIASK_AGENT_ENABLE_HERMES_FULL",
    "AIASK_AGENT_CONTROL_TOKEN",
    "AIASK_LOCAL_CONTROL_TOKEN",
)
for key in MODE_ENV_KEYS:
    os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _isolate_agent_mode_env() -> None:
    os.environ["AIASK_AGENT_LOAD_PROJECT_ENV"] = "0"
    for key in MODE_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    os.environ["AIASK_AGENT_LOAD_PROJECT_ENV"] = "0"
    for key in MODE_ENV_KEYS:
        os.environ.pop(key, None)


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--run-live-hermes",
        action="store_true",
        default=False,
        help="Run optional live AIASK-native Hermes smoke tests when credentials are configured.",
    )
    parser.addoption(
        "--run-live-ai",
        action="store_true",
        default=False,
        help="Run optional live AIASK model smoke tests when credentials are configured.",
    )
