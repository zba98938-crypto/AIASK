"""P1-1 gate: endpoint drift must stay fully explained by the allowlist.

The source of truth is the CURRENT HTTP surface, not a committed snapshot:
the gate loads ``reports/code-graph/live-routes.json`` (exported from the
live FastAPI app via ``scripts/code_graph/export_live_routes.py``) and
fails when a live application path lacks a valid allowlist reason, or when
a static ``/v1/desktop/*`` path has lost its covering catch-all route.
Comparing two static committed files against each other made the gate
inert; ``--live-routes`` makes it a real drift gate.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "code_graph" / "check_endpoint_drift.py"
EXPORTER = REPO_ROOT / "scripts" / "code_graph" / "export_live_routes.py"
ALLOWLIST = REPO_ROOT / "reports" / "code-graph" / "endpoint-allowlist.json"
LIVE_ROUTES = REPO_ROOT / "reports" / "code-graph" / "live-routes.json"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_endpoint_drift", CHECKER)
    assert spec and spec.loader, "could not load endpoint drift checker"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_live_routes() -> bool:
    """Export the live route set when the committed export is missing."""
    if LIVE_ROUTES.exists() and EXPORTER.exists():
        return True
    if not EXPORTER.exists():
        return False
    try:
        subprocess.run(
            [sys.executable, str(EXPORTER), "--out", str(LIVE_ROUTES)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception:
        return False
    return LIVE_ROUTES.exists()


@pytest.mark.skipif(not CHECKER.exists(), reason="drift checker missing")
def test_endpoint_drift_is_fully_explained() -> None:
    checker = _load_checker()
    if not _ensure_live_routes():
        pytest.skip("live route export unavailable")
    rc = checker.main([
        "--live-routes", str(LIVE_ROUTES),
        "--allowlist", str(ALLOWLIST),
    ])
    assert rc == 0, "live endpoint drift gate failed; run check_endpoint_drift.py --live-routes for details"


@pytest.mark.skipif(not ALLOWLIST.exists(), reason="allowlist missing")
def test_allowlist_reasons_are_valid() -> None:
    checker = _load_checker()
    allow = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    for path, reason in allow.get("server_only", {}).items():
        assert reason in checker.VALID_SERVER_REASONS, f"bad reason {reason!r} for {path}"
    for path, reason in allow.get("desktop_only", {}).items():
        assert reason in checker.VALID_DESKTOP_REASONS, f"bad reason {reason!r} for {path}"
