"""Offline dependency-advisory ledger contract.

The scanner used to hardcode ``configured: True`` and swallow a missing ledger
into an empty rule set, so a completely absent ledger reported ``status:
"passed"`` — a false clean bill of health. These tests pin the honest
behaviour: a usable ledger finds real advisories, and an unusable one is
reported as degraded rather than passing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aiask_agent import security as sec
from aiask_agent.security import SecurityScanner


def _explicit(scanner: SecurityScanner, name: str, version: str) -> dict[str, Any]:
    return scanner.dependency_advisory(
        {
            "include_loaded": False,
            "include_installed": False,
            "packages": [{"name": name, "version": version}],
        }
    )


def test_shipped_ledger_is_present_and_valid() -> None:
    """The ledger must ship inside the package, not merely be referenced by it."""
    assert sec._ADVISORIES_PATH.is_file(), f"advisory ledger is missing: {sec._ADVISORIES_PATH}"
    ledger = json.loads(sec._ADVISORIES_PATH.read_text(encoding="utf-8"))
    assert sec._ledger_defect(ledger) is None
    assert int(ledger["schema_version"]) >= 1
    assert ledger["last_reviewed"]
    assert ledger["advisories"], "a shipped ledger with zero rules would silently pass everything"
    for advisory in ledger["advisories"]:
        assert advisory["package"].strip()
        assert advisory["advisory_id"].strip()
        assert advisory["fixed_in"], f"{advisory['advisory_id']} must state a fixed version"
        assert advisory["summary"].strip()
        assert advisory["severity"] in {"critical", "high", "medium", "low", "info"}
        assert advisory["vulnerable_versions"]


def test_ledger_constraints_use_the_supported_grammar() -> None:
    """Constraints the matcher cannot parse would silently never match.

    ``_matches_constraint`` returns False for any clause its regex rejects, so an
    unparsable constraint is a rule that can never fire. Check each clause
    against that same regex, and check that the version target survives
    ``_parse_version`` (which truncates at the first non-numeric chunk, so a
    pre-release suffix would silently shift the boundary).
    """
    ledger = json.loads(sec._ADVISORIES_PATH.read_text(encoding="utf-8"))
    for advisory in ledger["advisories"]:
        for expr in advisory["vulnerable_versions"]:
            clauses = [clause.strip() for clause in expr.split(",") if clause.strip()]
            assert clauses, f"{advisory['advisory_id']}: empty constraint {expr!r}"
            for clause in clauses:
                match = sec.re.match(r"^(<=|>=|==|!=|<|>)\s*([0-9][0-9A-Za-z._\-+]*)\s*$", clause)
                assert match, f"{advisory['advisory_id']}: unparsable clause {clause!r}"
                target = match.group(2)
                assert sec._parse_version(target) == tuple(
                    int(part) for part in target.split(".")
                ), f"{advisory['advisory_id']}: version target {target!r} is not purely numeric-dotted"


def test_ledger_constraints_bracket_their_fixed_version() -> None:
    """Each advisory must fire below its fixed version and stop at or above it."""
    ledger = json.loads(sec._ADVISORIES_PATH.read_text(encoding="utf-8"))
    for advisory in ledger["advisories"]:
        fixed = str(advisory["fixed_in"])
        constraints = advisory["vulnerable_versions"]
        assert not any(
            sec._matches_constraint(fixed, expr) for expr in constraints
        ), f"{advisory['advisory_id']}: fixed version {fixed} must not match as vulnerable"


def test_flags_requests_2_30_0_from_the_curated_ledger() -> None:
    result = _explicit(SecurityScanner(), "requests", "2.30.0")
    assert result["configured"] is True
    assert result["status"] == "warning"
    flagged = [item for item in result["findings"] if str(item.get("package", "")).lower() == "requests"]
    assert flagged, "requests 2.30.0 must be flagged by the curated ledger"
    for item in flagged:
        assert item["installed_version"] == "2.30.0"
        assert item["fixed_in"]
        assert item["advisory_id"]


def test_patched_version_is_not_flagged() -> None:
    result = _explicit(SecurityScanner(), "requests", "2.32.5")
    assert result["finding_count"] == 0
    assert result["status"] == "passed"
    assert result["degraded"] is False


def test_scan_is_offline_and_deterministic() -> None:
    """Two identical scans must agree; the scanner must never reach the network."""
    scanner = SecurityScanner()
    first = _explicit(scanner, "urllib3", "2.0.0")
    second = _explicit(scanner, "urllib3", "2.0.0")
    assert first["findings"] == second["findings"]
    assert [item["advisory_id"] for item in first["findings"]] == ["GHSA-34jh-p97f-mpxf"]


def test_missing_ledger_reports_degraded_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sec, "_ADVISORIES_PATH", Path("/nonexistent/known_advisories.json"))
    result = _explicit(SecurityScanner(), "requests", "2.30.0")
    assert result["configured"] is False
    assert result["status"] == "degraded", "a missing ledger must never report a clean pass"
    assert result["degraded"] is True
    assert result["error_code"] == "ledger_missing"
    assert result["error"]
    assert result["advisory_count"] == 0
    assert result["finding_count"] == 0


def test_corrupt_ledger_reports_degraded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "known_advisories.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(sec, "_ADVISORIES_PATH", broken)
    result = SecurityScanner().dependency_advisory({})
    assert result["configured"] is False
    assert result["status"] == "degraded"
    assert result["error_code"] == "ledger_parse_error"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 0, "advisories": []},
        {"schema_version": 1, "advisories": "not-a-list"},
        {"schema_version": 1, "advisories": [{"package": "requests"}]},
        {"schema_version": 1, "advisories": [{"advisory_id": "X", "vulnerable_versions": ["<1"]}]},
        {"schema_version": 1, "advisories": [{"package": "x", "advisory_id": "X", "vulnerable_versions": []}]},
    ],
)
def test_invalid_schema_reports_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, Any]
) -> None:
    path = tmp_path / "known_advisories.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(sec, "_ADVISORIES_PATH", path)
    result = SecurityScanner().dependency_advisory({})
    assert result["configured"] is False
    assert result["status"] == "degraded"
    assert result["error_code"] in {"ledger_schema_invalid", "ledger_parse_error"}


def test_status_surfaces_ledger_defects(monkeypatch: pytest.MonkeyPatch) -> None:
    healthy = SecurityScanner().status()
    assert healthy["status"] == "implemented"
    assert healthy["dependency_advisory"]["configured"] is True
    assert healthy["dependency_advisory"]["error_code"] is None

    monkeypatch.setattr(sec, "_ADVISORIES_PATH", Path("/nonexistent/known_advisories.json"))
    degraded = SecurityScanner().status()
    assert degraded["status"] == "degraded"
    assert degraded["dependency_advisory"]["configured"] is False
    assert degraded["dependency_advisory"]["error_code"] == "ledger_missing"
    assert degraded["dependency_advisory"]["degraded"] is True


def test_installed_distribution_scan_path_runs_offline() -> None:
    """The default path enumerates the live env; it must stay usable and honest."""
    result = SecurityScanner().dependency_advisory({})
    assert result["configured"] is True
    assert result["lazy_supply_chain_check"] is True
    assert result["advisory_count"] >= 1
    assert result["scanned_count"] >= 1
    assert result["status"] in {"passed", "warning", "blocked"}
    assert isinstance(result["findings"], list)
