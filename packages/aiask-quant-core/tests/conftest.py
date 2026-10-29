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

import pytest


@pytest.fixture(autouse=True)
def _register_strategy_schema_provider():
    """Supply the factory-domain schema owner when the package is installed.

    quant-core is a leaf package and does not import strategy-factory.  Its
    tests still create databases that include factory tables, so the owner is
    registered explicitly here, exactly as a host would do at startup.
    """
    try:
        from strategy_factory.infrastructure.factory_schema import (
            register_factory_schema,
        )
    except ImportError:
        yield
        return
    register_factory_schema()
    yield
