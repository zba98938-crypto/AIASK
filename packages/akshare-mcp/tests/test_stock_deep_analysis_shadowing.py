"""The live deep-analysis package must define the name it uses.

There are two implementations under one import name:

    services/stock_deep_analysis.py     (50 KB module)
    services/stock_deep_analysis/       (package)

Python resolves the package, so the module is dead — but it is the one that
readers are likely to open, and it contains ``from ..utils import normalize_code``
while the live package does not. With a blank or unmatched query,
``target["code"]`` is falsy and the live package evaluates an unbound name:

    resolved_code = str(target.get("code") or normalize_code(code))   # NameError

which aborts before the structured ``target_resolution: blocked`` result can be
returned. Empty input is not exotic — it is what an unmatched stock name
produces.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from akshare_mcp.services import stock_deep_analysis as pkg


def test_the_live_package_is_resolved_not_the_shadowed_module() -> None:
    resolved = Path(pkg.__file__).resolve()
    assert resolved.parent.name == "stock_deep_analysis"
    assert resolved.name == "__init__.py", (
        "a bare .py module would mean the shadowing reversed; the import layout "
        f"this test depends on changed: {resolved}"
    )


def test_the_live_package_defines_normalize_code() -> None:
    assert hasattr(pkg, "normalize_code"), (
        "the package calls normalize_code but must import it; the shadowed "
        "module importing it is not enough"
    )


def test_blank_code_does_not_raise_name_error() -> None:
    """Guards the unbound name specifically, not the whole downstream flow."""

    try:
        result = asyncio.run(pkg.run_stock_deep_analysis(code=""))
    except NameError as exc:  # pragma: no cover - only on regression
        pytest.fail(
            "an unmatched/blank target must produce a structured "
            f"'unresolved target' result, not an unbound name: {exc}"
        )
    except Exception:
        # A different downstream failure (database, artifact store) is a
        # separate concern. This test claims only that the NameError is gone.
        return
    assert isinstance(result, dict)
