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

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _register_factory_schema_provider():
    """A fresh test database must include the factory-domain tables."""
    from strategy_factory.infrastructure.factory_schema import register_factory_schema

    register_factory_schema()
    yield


# Deployment switches that change incubation behaviour. A test that needs one
# sets it with monkeypatch; nothing inherited from a shell or a loaded .env may
# decide a test's outcome. Three tests failed only in root runs because a
# module-level .env load had turned these on.
_DEPLOYMENT_SWITCHES = (
    "STRATEGY_FACTORY_BULK_LLM_ENABLED",
    "STRATEGY_FACTORY_BULK_OPTIMIZER_ENABLED",
    "STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED",
    "STRATEGY_FACTORY_HORIZON_SCORING_ENABLED",
    "STRATEGY_FACTORY_CALIBRATION_ENABLED",
    "STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED",
    "STRATEGY_FACTORY_PREDICTION_HITRATE_GATE_ENABLED",
    "STRATEGY_FACTORY_SELECTIVE_GATE_THRESHOLD",
)


@pytest.fixture(autouse=True)
def _clear_deployment_switches(monkeypatch):
    for name in _DEPLOYMENT_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    yield

