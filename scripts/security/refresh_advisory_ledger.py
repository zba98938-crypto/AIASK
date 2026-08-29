#!/usr/bin/env python
"""Refresh the offline advisory ledger from OSV.

The agent's dependency scanner reads a committed ledger and never touches the
network, so ordinary test runs and PRs stay fully offline and deterministic.
This script is the *only* thing that talks to OSV, and it is meant to run on a
schedule (see .github/workflows/advisory-refresh.yml) to open a PR when upstream
advisory data has moved.

What it does:
  - re-fetches every advisory already tracked in the ledger, by id
  - rewrites vulnerable_versions / fixed_in / summary / aliases from OSV
  - leaves the set of tracked advisories alone (adding new packages is a
    human decision, not something a cron job should do silently)

Usage:
    python scripts/security/refresh_advisory_ledger.py --check
    python scripts/security/refresh_advisory_ledger.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "packages" / "agent" / "src" / "aiask_agent" / "data" / "known_advisories.json"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/{advisory_id}"

# Only these severities are emitted; anything else is a data error worth failing on.
VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


def _fetch(advisory_id: str, *, timeout: float) -> dict[str, Any]:
    url = OSV_VULN_URL.format(advisory_id=advisory_id)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read().decode("utf-8"))


def _pypi_ranges(record: dict[str, Any], package: str) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for affected in record.get("affected") or []:
        pkg = affected.get("package") or {}
        if str(pkg.get("ecosystem") or "").lower() != "pypi":
            continue
        if str(pkg.get("name") or "").lower() != package.lower():
            continue
        ranges.extend(affected.get("ranges") or [])
    return ranges


def _numeric_dotted(version: str) -> bool:
    """The matcher truncates at the first non-numeric chunk, so reject anything else."""
    parts = str(version).split(".")
    return bool(parts) and all(part.isdigit() for part in parts)


def _constraints_from_ranges(ranges: list[dict[str, Any]]) -> tuple[list[str], str | None]:
    """Translate OSV introduced/fixed events into the scanner's constraint grammar."""
    constraints: list[str] = []
    fixed_versions: list[str] = []
    for entry in ranges:
        if str(entry.get("type") or "").upper() != "ECOSYSTEM":
            continue
        introduced: str | None = None
        for event in entry.get("events") or []:
            if "introduced" in event:
                raw = str(event["introduced"])
                introduced = None if raw in {"0", ""} else raw
            elif "fixed" in event:
                fixed = str(event["fixed"])
                for version in filter(None, (introduced, fixed)):
                    if not _numeric_dotted(version):
                        raise ValueError(f"non-numeric version boundary from OSV: {version!r}")
                clause = f"<{fixed}" if introduced is None else f">={introduced},<{fixed}"
                constraints.append(clause)
                fixed_versions.append(fixed)
                introduced = None
    highest_fixed = max(fixed_versions, key=lambda v: tuple(int(p) for p in v.split("."))) if fixed_versions else None
    return constraints, highest_fixed


def refresh(ledger: dict[str, Any], *, timeout: float) -> tuple[dict[str, Any], list[str]]:
    """Return (updated_ledger, change_descriptions)."""
    updated = json.loads(json.dumps(ledger))
    changes: list[str] = []
    for advisory in updated.get("advisories") or []:
        advisory_id = str(advisory.get("advisory_id") or "").strip()
        package = str(advisory.get("package") or "").strip()
        if not advisory_id or not package:
            raise ValueError(f"ledger entry missing advisory_id/package: {advisory!r}")
        record = _fetch(advisory_id, timeout=timeout)
        ranges = _pypi_ranges(record, package)
        if not ranges:
            raise ValueError(f"{advisory_id}: OSV reports no PyPI ranges for {package}")
        constraints, fixed_in = _constraints_from_ranges(ranges)
        if not constraints or not fixed_in:
            raise ValueError(f"{advisory_id}: could not derive constraints from OSV ranges")

        for field, new_value in (
            ("vulnerable_versions", constraints),
            ("fixed_in", fixed_in),
            ("aliases", sorted(record.get("aliases") or [])),
            ("summary", str(record.get("summary") or advisory.get("summary") or "").strip()),
        ):
            old_value = advisory.get(field)
            if old_value != new_value:
                changes.append(f"{advisory_id}.{field}: {old_value!r} -> {new_value!r}")
                advisory[field] = new_value

        if advisory.get("severity") not in VALID_SEVERITIES:
            raise ValueError(f"{advisory_id}: invalid severity {advisory.get('severity')!r}")
    return updated, changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report drift, do not write")
    mode.add_argument("--write", action="store_true", help="rewrite the ledger in place")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read ledger {LEDGER_PATH}: {exc}", file=sys.stderr)
        return 2

    try:
        updated, changes = refresh(ledger, timeout=args.timeout)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"OSV request failed: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"advisory data rejected: {exc}", file=sys.stderr)
        return 2

    if not changes:
        print("ledger is up to date with OSV")
        return 0

    print("advisory drift detected:")
    for change in changes:
        print(f"  {change}")

    if args.write:
        updated["last_reviewed"] = __import__("datetime").date.today().isoformat()
        LEDGER_PATH.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {LEDGER_PATH}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
