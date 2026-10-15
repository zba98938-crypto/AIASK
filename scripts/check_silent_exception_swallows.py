#!/usr/bin/env python3
"""Report exception handlers that discard the error without recording it.

Why this exists
---------------
``CLAUDE.md`` rules that a degraded path must let "could not be measured" be
told apart from "measured zero", and names ``except Exception: continue`` as the
pattern that breaks it. Counting them once is not a gate; the count grows back.
This script makes the count repeatable so a regression is visible.

It reports, it does not block. Most of these sites are legitimate best-effort
cleanup, optional imports, or "optional field absent" — a hard threshold would
be a number nobody could defend. The signal is the trend, and the ``--risky``
view, which narrows to the paths where a swallowed error can change a judgment:
risk, gates, scoring, valuation and quality reporting.

Expect false positives, and do not "fix" them:

* A helper that returns ``None`` for unparseable input, such as
  ``_quality_reporting_policy._first_float`` or ``promotion_gate._finite_float``,
  is the honest contract, not a swallow. Callers deny on ``None``.
* A helper that returns ``0.0`` on the *conservative* side of a check — for
  example ``safe_metric_value`` feeding ``has_degenerate_validation_statistics``,
  where a missing metric makes the report look degenerate and therefore rejected
  — errs toward refusing. Read the direction before changing it.

Usage::

    python scripts/check_silent_exception_swallows.py
    python scripts/check_silent_exception_swallows.py --risky
    python scripts/check_silent_exception_swallows.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Path fragments where a discarded error can turn "unmeasured" into a number
#: that passes a check.
RISKY_MARKERS = (
    "risk_model",
    "quality_gate",
    "quality_report",
    "promotion_gate",
    "decision_rule_gate",
    "submission_gate",
    "validation",
    "valuation",
    "scoring",
    "stress",
)

EMPTY_RETURNS = (None, False, 0, 0.0, "")


def _swallow_kind(handler: ast.ExceptHandler) -> str | None:
    """Classify a handler that discards its error, or return ``None``."""

    body = handler.body
    if len(body) != 1:
        return None
    statement = body[0]
    if isinstance(statement, ast.Pass):
        return "pass"
    if isinstance(statement, ast.Continue):
        return "continue"
    if isinstance(statement, ast.Return):
        value = statement.value
        if value is None:
            return "return_empty"
        if isinstance(value, ast.Constant) and value.value in EMPTY_RETURNS:
            return "return_empty"
    return None


def scan(root: Path) -> list[dict]:
    findings: list[dict] = []
    for path in sorted(root.rglob("*.py")):
        parts = path.parts
        if ".venv" in parts or "__pycache__" in parts or "tests" in parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            kind = _swallow_kind(node)
            if kind is None:
                continue
            findings.append(
                {
                    "path": str(path.relative_to(root)),
                    "line": node.lineno,
                    "handler": ast.unparse(node.type) if node.type else "bare",
                    "kind": kind,
                    "risky": any(marker in str(path) for marker in RISKY_MARKERS),
                }
            )
    return findings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--risky", action="store_true", help="only judgment-changing paths")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--root", default="", help="scan root (default: repository)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser().resolve() if args.root else REPO_ROOT
    findings = scan(root)
    shown = [item for item in findings if item["risky"]] if args.risky else findings

    if args.json:
        print(json.dumps({"total": len(findings), "shown": shown}, indent=2))
        return 0

    by_package: dict[str, int] = {}
    for item in findings:
        parts = Path(item["path"]).parts
        package = parts[1] if len(parts) > 1 and parts[0] == "packages" else parts[0]
        by_package[package] = by_package.get(package, 0) + 1

    print(f"scan root            : {root}")
    print(f"discarding handlers  : {len(findings)}")
    print(f"on judgment paths    : {sum(1 for item in findings if item['risky'])}")
    print()
    print("by package")
    for package, count in sorted(by_package.items(), key=lambda pair: -pair[1]):
        print(f"  {count:6d}  {package}")

    if args.risky:
        print()
        print("judgment-changing paths")
        for item in shown:
            print(f"  {item['path']}:{item['line']}  {item['handler']} -> {item['kind']}")
    else:
        print()
        print("narrow to the ones that can change a judgment with --risky")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
