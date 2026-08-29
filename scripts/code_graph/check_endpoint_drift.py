#!/usr/bin/env python
"""P1-1 fix: endpoint drift gate.

The code-graph endpoint map flags every HTTP endpoint as one of:
  - matched      : present in both the Agent server and the Desktop client.
  - server_only  : a server route with no Desktop literal pointing at it.
  - desktop_only : a Desktop string with no matching server route.

Raw server_only / desktop_only counts are NOT bugs by themselves: most
server_only entries are parametrized sub-resources that Desktop reaches by
building dynamic URLs under an already-matched base path, and most
desktop_only entries are documentation labels or mock literals. This gate
loads ``reports/code-graph/endpoint-allowlist.json`` (which classifies each
known entry with a reason) and fails ONLY when the endpoint map contains a
server_only / desktop_only entry that the allowlist does not explain. That
turns "42 server-only / 13 desktop-only" from an unexplained number into an
explicit, reviewed contract, and makes any *new* drift a hard CI failure.

Usage:
    python scripts/code_graph/check_endpoint_drift.py
    python scripts/code_graph/check_endpoint_drift.py --endpoint-map <path> --allowlist <path>

Exit code 0 = all drift explained; 1 = unexplained drift (or missing inputs).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINT_MAP = (
    REPO_ROOT / "reports" / "code-graph" / "full-2026-05-29" / "curated" / "endpoint-map.json"
)
DEFAULT_ALLOWLIST = REPO_ROOT / "reports" / "code-graph" / "endpoint-allowlist.json"

VALID_SERVER_REASONS = {"parametrized_subresource", "server_capability"}
VALID_DESKTOP_REASONS = {"doc_label", "mock_or_prefix"}


def _classify(endpoint_map: dict) -> tuple[list[str], list[str]]:
    """Return (server_only_paths, desktop_only_paths) from a raw endpoint map."""
    server_only: list[str] = []
    desktop_only: list[str] = []
    for ep in endpoint_map.get("endpoints", []):
        path = ep.get("path", "")
        has_server = bool(ep.get("server"))
        has_desktop = bool(ep.get("desktop"))
        if has_server and not has_desktop:
            server_only.append(path)
        elif has_desktop and not has_server:
            desktop_only.append(path)
    return server_only, desktop_only


def _check_side(
    observed: list[str],
    allow: dict[str, str],
    valid_reasons: set[str],
    side: str,
) -> list[str]:
    """Return a list of human-readable problems for one side of the map."""
    problems: list[str] = []
    for path in observed:
        reason = allow.get(path)
        if reason is None:
            problems.append(f"[{side}] UNEXPLAINED drift: {path!r} is not in the allowlist")
        elif reason not in valid_reasons:
            problems.append(
                f"[{side}] invalid reason {reason!r} for {path!r} "
                f"(allowed: {sorted(valid_reasons)})"
            )
    # Stale allowlist entries (in allowlist but no longer drifting) are a
    # warning, not a failure: the endpoint may have become matched.
    observed_set = set(observed)
    for path in allow:
        if path not in observed_set:
            problems.append(f"[{side}] STALE allowlist entry (no longer drifting): {path!r} -- safe to remove")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIASK endpoint drift gate (P1-1)")
    parser.add_argument("--endpoint-map", type=Path, default=DEFAULT_ENDPOINT_MAP)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--warn-stale",
        action="store_true",
        help="Treat stale allowlist entries as warnings (default: they fail the gate).",
    )
    args = parser.parse_args(argv)

    if not args.endpoint_map.exists():
        print(f"ERROR: endpoint map not found: {args.endpoint_map}", file=sys.stderr)
        return 1
    if not args.allowlist.exists():
        print(f"ERROR: allowlist not found: {args.allowlist}", file=sys.stderr)
        return 1

    endpoint_map = json.loads(args.endpoint_map.read_text(encoding="utf-8"))
    allowlist = json.loads(args.allowlist.read_text(encoding="utf-8"))

    server_only, desktop_only = _classify(endpoint_map)
    allow_server = {k: v for k, v in allowlist.get("server_only", {}).items()}
    allow_desktop = {k: v for k, v in allowlist.get("desktop_only", {}).items()}

    problems: list[str] = []
    problems += _check_side(server_only, allow_server, VALID_SERVER_REASONS, "server_only")
    problems += _check_side(desktop_only, allow_desktop, VALID_DESKTOP_REASONS, "desktop_only")

    stale = [p for p in problems if "STALE" in p]
    hard = [p for p in problems if "STALE" not in p]

    print(
        f"endpoint drift gate: {len(server_only)} server-only, "
        f"{len(desktop_only)} desktop-only; "
        f"{len(hard)} unexplained, {len(stale)} stale allowlist entries"
    )

    failures = hard if args.warn_stale else problems
    if failures:
        print("\nDRIFT GATE FAILURES:")
        for line in failures:
            print(f"  - {line}")
        if args.warn_stale and stale:
            print("\nWARNINGS (stale allowlist entries):")
            for line in stale:
                print(f"  - {line}")
        return 1

    print("OK: all endpoint drift is explained by the allowlist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
