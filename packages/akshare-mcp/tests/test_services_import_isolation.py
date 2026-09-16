from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

def test_services_package_import_survives_optional_subservice_failure() -> None:
    # Import-failure simulation replaces an entire package graph.  Run it in
    # a child interpreter so parent-package attributes and sys.modules cannot
    # leak split module identities into later tests.
    script = textwrap.dedent(
        """
        import builtins
        import importlib

        real_import = builtins.__import__
        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            package = str((globals or {}).get('__package__') or '')
            if level == 1 and package == 'akshare_mcp.services' and name == 'factor_candidate_compiler':
                raise ImportError("No module named 'numba'")
            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = guarded_import
        services = importlib.import_module('akshare_mcp.services')
        stock_radar = importlib.import_module('akshare_mcp.services.stock_radar')
        assert stock_radar.__name__ == 'akshare_mcp.services.stock_radar'
        assert services.SUPPORTED_FACTOR_FIELDS == set()
        assert services.SUPPORTED_FACTOR_FUNCTIONS == set()
        try:
            services.compile_factor_candidate({})
        except RuntimeError as exc:
            assert "compile_factor_candidate unavailable: No module named 'numba'" in str(exc)
        else:
            raise AssertionError('optional compiler fallback did not raise')
        """
    )
    # pytest.ini's `pythonpath` only mutates the parent's sys.path; a child
    # started with -c inherits none of it. Forward the resolved import roots so
    # this test does not depend on which interpreter happens to run pytest.
    env = dict(os.environ)
    roots = [str(Path(__file__).resolve().parents[1] / "src")]
    roots.extend(entry for entry in sys.path if entry)
    existing = env.get("PYTHONPATH", "")
    if existing:
        roots.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(roots))
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
