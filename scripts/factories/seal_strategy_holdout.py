#!/usr/bin/env python3
"""Explicit prospective holdout freeze, or strictly read-only pending status.

An archive reference is the complete evaluated reference returned by the
candidate recorder (not the gzip document itself or a bounded run preview).
This command never runs factories, fetches data, evaluates returns or grants
admission. Use a fresh interpreter so source hashes describe this deployment.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="Write immutable future-window pending seals")
    freeze.add_argument("--db", type=Path, required=True)
    freeze.add_argument("--archive-reference", type=Path, required=True, help="JSON file containing the complete evaluated archive reference")
    freeze.add_argument("--selection-data-end", required=True, help="Verified or conservative upper bound for selection market data, YYYY-MM-DD")
    selection = freeze.add_mutually_exclusive_group(required=True)
    selection.add_argument("--candidate-id", action="append")
    selection.add_argument("--all-candidates", action="store_true")
    freeze.add_argument("--min-sessions", type=int, default=20)
    freeze.add_argument("--max-candidates", type=int, default=100)
    status = commands.add_parser("status", help="Read existing seals without creating a database or schema")
    status.add_argument("--db", type=Path, required=True)
    status.add_argument("--include-snapshots", action="store_true")
    args = parser.parse_args(argv)
    from strategy_factory.application.oos_freeze import freeze_candidates_from_archive, pending_holdout_status

    try:
        if args.command == "freeze":
            result = freeze_candidates_from_archive(
                args.db, json.loads(args.archive_reference.read_text(encoding="utf-8")),
                selection_data_end=args.selection_data_end, candidate_ids=args.candidate_id,
                min_sessions=args.min_sessions, max_candidates=args.max_candidates,
            )
        else:
            result = pending_holdout_status(args.db, include_snapshots=args.include_snapshots)
    except (ValueError, TypeError, OSError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0 if result["status"] != "unavailable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
