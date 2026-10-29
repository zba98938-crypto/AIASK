"""Root conftest: isolate every test session from the operator's runtime state.

Packages with their own pytest configuration stop conftest discovery at the
package directory, so each ``packages/*/tests/conftest.py`` makes the same call.
``install()`` is idempotent. See ``aiask_quant_core/testing/hermetic.py``.
"""

import importlib.util as _hermetic_util
import sys as _hermetic_sys
from pathlib import Path as _HermeticPath


def _install_hermetic_runtime() -> None:
    name = "_aiask_test_hermetic"
    module = _hermetic_sys.modules.get(name)
    if module is None:
        path = _HermeticPath(__file__).resolve().parent / "packages/aiask-quant-core/src/aiask_quant_core/testing/hermetic.py"
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
