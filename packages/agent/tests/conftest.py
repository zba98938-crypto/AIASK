from __future__ import annotations

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
