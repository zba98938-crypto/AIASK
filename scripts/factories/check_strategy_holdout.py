#!/usr/bin/env python3
"""Read-only persisted-seal maturity audit, with optional explicit OOS replay.

Writes immutable evidence files only; never updates the database or admission.
Use a fresh interpreter with the source version frozen in the seal.
"""
from __future__ import annotations

import argparse
import asyncio
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--seal-id", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args(argv)
    from strategy_factory.application.oos_maturity import evaluate_sealed_holdout

    result = asyncio.run(evaluate_sealed_holdout(args.db, args.seal_id, args.evidence_dir, replay=args.replay))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if result["status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
