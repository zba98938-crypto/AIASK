"""P1-1 gate: endpoint drift must stay fully explained by the allowlist.

This wraps ``scripts/code_graph/check_endpoint_drift.py`` so the endpoint
contract is enforced as part of the agent test suite (and therefore CI).
The checker fails when the committed endpoint map contains a server-only or
desktop-only endpoint that the allowlist does not classify with a valid
reason. New, unexplained drift becomes a hard failure here.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "code_graph" / "check_endpoint_drift.py"
ALLOWLIST = REPO_ROOT / "reports" / "code-graph" / "endpoint-allowlist.json"
ENDPOINT_MAP = (
    REPO_ROOT / "reports" / "code-graph" / "full-2026-05-29" / "curated" / "endpoint-map.json"
)


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_endpoint_drift", CHECKER)
    assert spec and spec.loader, "could not load endpoint drift checker"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not CHECKER.exists(), reason="drift checker missing")
@pytest.mark.skipif(not ENDPOINT_MAP.exists(), reason="endpoint map not generated")
def test_endpoint_drift_is_fully_explained() -> None:
    checker = _load_checker()
    rc = checker.main(["--endpoint-map", str(ENDPOINT_MAP), "--allowlist", str(ALLOWLIST)])
    assert rc == 0, "endpoint drift gate failed; run check_endpoint_drift.py for details"


@pytest.mark.skipif(not ALLOWLIST.exists(), reason="allowlist missing")
def test_allowlist_reasons_are_valid() -> None:
    checker = _load_checker()
    allow = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    for path, reason in allow.get("server_only", {}).items():
        assert reason in checker.VALID_SERVER_REASONS, f"bad reason {reason!r} for {path}"
    for path, reason in allow.get("desktop_only", {}).items():
        assert reason in checker.VALID_DESKTOP_REASONS, f"bad reason {reason!r} for {path}"
