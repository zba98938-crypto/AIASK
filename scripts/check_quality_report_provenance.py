#!/usr/bin/env python3
"""Report which strategy quality reports predate the measurement fixes.

Why this exists
---------------
The 179 reports already in the runtime database were produced by code that has
since been fixed, and their numbers are still shaped like measurements:

* ``stress_loss_percent = -20.0`` in every one of them, because the stress path
  defaulted a missing beta to 1.0. The admission gate compares against -25.0, so
  that check could never fire on them.
* ``var_percent = 0.0`` in eight, because the caller read a missing key as zero.
  That value passes ``var_percent_max = 4.5`` without anything being computed.
* ``n_folds = 0`` in eighteen, i.e. a walk-forward that ran no folds and stored
  all-zero statistics as a result.

Fixing the code does not repair those rows. The ``measurement_contract`` column
marks them (``pre_fix_unverified`` by default) so a consumer can tell which it
is holding. This script shows how many of each kind exist.

Named ``check_*`` on purpose: pytest must not collect it, because it needs a
populated runtime database.

Usage::

    python scripts/check_quality_report_provenance.py
    python scripts/check_quality_report_provenance.py --db /path/to.sqlite3

Exit status is 0 when every report carries the current contract, 1 when
unverified reports remain (so it can gate a pipeline).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

SCRIPT = Path(__file__).resolve()
REPO_ROOT = SCRIPT.parents[1]

DEFAULT_DB = Path.home() / ".aiask" / "data" / "db" / "akshare_mcp.sqlite3"
CURRENT_CONTRACT = "2026-10-v1"
UNVERIFIED = "pre_fix_unverified"


def _resolve_db(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    import os

    for key in ("AIASK_SQLITE_PATH", "AKSHARE_MCP_SQLITE_PATH"):
        raw = str(os.getenv(key) or "").strip()
        if raw:
            return Path(raw).expanduser()
    return DEFAULT_DB


def _load_json(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="SQLite path (default: runtime default)")
    args = parser.parse_args()

    db_path = _resolve_db(args.db)
    if not db_path.is_file():
        print(f"database not found: {db_path}")
        return 1

    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_quality_reports)")}
        if not columns:
            print("strategy_quality_reports does not exist in this database")
            return 1
        has_contract = "measurement_contract" in columns

        select = "strategy_id, passed, validation_report, risk_report"
        if has_contract:
            select = "measurement_contract, " + select
        rows = list(conn.execute(f"SELECT {select} FROM strategy_quality_reports"))
    finally:
        conn.close()

    total = len(rows)
    if not has_contract:
        print(f"database: {db_path}")
        print(f"  {total} report(s); the measurement_contract column is absent,")
        print("  so none of them can be distinguished from a current measurement.")
        return 1

    contracts = Counter(str(row["measurement_contract"]) for row in rows)
    unverified = contracts.get(UNVERIFIED, 0)

    constant_stress: list[str] = []
    zero_var: list[str] = []
    no_folds: list[str] = []
    for row in rows:
        risk = _load_json(row["risk_report"])
        validation = _load_json(row["validation_report"])
        if risk.get("stress_loss_percent") == -20.0:
            constant_stress.append(str(row["strategy_id"]))
        if risk.get("var_percent") == 0.0:
            zero_var.append(str(row["strategy_id"]))
        walk_forward = validation.get("walk_forward") or {}
        if int(walk_forward.get("n_folds") or 0) <= 0:
            no_folds.append(str(row["strategy_id"]))

    print(f"database: {db_path}")
    print(f"  reports: {total}")
    print("  by measurement_contract:")
    for name, count in contracts.most_common():
        print(f"    {name:24s} {count}")
    print("  reports carrying the signatures of the pre-fix code:")
    print(f"    stress_loss_percent == -20.0 : {len(constant_stress)}")
    print(f"    var_percent == 0.0           : {len(zero_var)}")
    print(f"    walk_forward n_folds == 0    : {len(no_folds)}")

    if unverified:
        print()
        print(f"RESULT: {unverified} report(s) are stamped {UNVERIFIED}.")
        print("Their metrics were produced by code that no longer exists and must")
        print("not be read as measurements comparable with current output.")
        print("Re-computation would need the market data backfilled first; these")
        print("rows stop at 2026-07-23.")
        return 1

    if contracts.get(CURRENT_CONTRACT, 0) == total:
        print()
        print(f"RESULT: all reports carry the current contract ({CURRENT_CONTRACT}).")
        return 0

    legacy = total - contracts.get(CURRENT_CONTRACT, 0)
    print()
    print(f"RESULT: {legacy} report(s) carry a contract other than {CURRENT_CONTRACT}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
