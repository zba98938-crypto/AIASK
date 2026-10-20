"""The deep-analysis package must define the names it uses, with nothing shadowing it.

There used to be two implementations under one import name:

    services/stock_deep_analysis.py     (50 KB module, dead — Python resolves the package)
    services/stock_deep_analysis/       (package, live)

The dead module was the one a reader is likely to open, and it differed from the
live package (it imported ``normalize_code``, the package had to as well). It has
been deleted; the assertions below guard against a bare module reappearing under
the same name and against the live package losing ``normalize_code``, which is
what turned a blank/unmatched query into an unbound name:

    resolved_code = str(target.get("code") or normalize_code(code))   # NameError

That aborted before the structured ``target_resolution: blocked`` result could be
returned. Empty input is not exotic — it is what an unmatched stock name produces.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from akshare_mcp.services import stock_deep_analysis as pkg


def test_the_live_package_is_resolved_and_not_shadowed_by_a_module() -> None:
    shadowing_module = Path(pkg.__file__).resolve().parent.parent / "stock_deep_analysis.py"
    assert not shadowing_module.exists(), (
        "a bare .py module next to the package would shadow it again; the dead "
        f"copy was deleted on purpose: {shadowing_module}"
    )
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
