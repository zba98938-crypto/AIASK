from __future__ import annotations

import subprocess
import sys
import textwrap

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
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
