"""Isolate the session from ~/.aiask, ~/.aiask-agent and the repo .env.

This package has its own pytest configuration, so the repository-root
conftest is not loaded for package-local runs. See
``aiask_quant_core/testing/hermetic.py``.
"""

import importlib.util as _hermetic_util
import sys as _hermetic_sys
from pathlib import Path as _HermeticPath


def _install_hermetic_runtime() -> None:
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
