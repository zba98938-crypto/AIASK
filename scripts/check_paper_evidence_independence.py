#!/usr/bin/env python3
"""Read-only clustering diagnostic for paper-fill and position dependence."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path


def cluster_summary(rows: list[dict], *, key_fields: tuple[str, ...]) -> dict:
    clusters = Counter(
        tuple(str(row.get(field) or "") for field in key_fields)
        for row in rows
    )
    count = len(rows)
    max_cluster = max(clusters.values(), default=0)
    return {
        "raw_count": count,
        "independent_cluster_count": len(clusters),
        "max_cluster_count": max_cluster,
        "max_cluster_share": (max_cluster / count) if count else 0.0,
        "clusters": [
            {"key": list(key), "count": n}
            for key, n in clusters.most_common()
        ],
    }


def diagnose(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        trades = [dict(row) for row in con.execute(
            "SELECT stock_code, substr(trade_time,1,10) AS execution_date, trade_type AS direction, price, account_id FROM paper_trades"
        )]
        positions = [dict(row) for row in con.execute(
            "SELECT stock_code, substr(updated_at,1,10) AS execution_date, 'position' AS direction, cost_price AS price, account_id FROM paper_positions WHERE quantity > 0"
        )]
        trade_clusters = cluster_summary(
            trades,
            key_fields=("stock_code", "execution_date", "direction", "price"),
        )
        position_clusters = cluster_summary(
            positions,
            key_fields=("stock_code", "execution_date", "direction", "price"),
        )
        by_code = Counter(str(row.get("stock_code") or "") for row in positions)
        position_count = len(positions)
        code_counts = dict(by_code.most_common())
        return {
            "database": str(db_path), "read_only": True,
            "fills": trade_clusters, "open_positions": position_clusters,
            "single_code_position_share": {
                code: n / position_count for code, n in by_code.items()
            } if position_count else {},
            "open_position_count": position_count,
            "position_code_counts": code_counts,
            "note": "Clusters are diagnostic only; no readiness or promotion gate is changed.",
        }
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if not default:
        default = str(Path(os.getenv("AIASK_RUNTIME_HOME", Path.home() / ".aiask")).expanduser() / "data/db/akshare_mcp.sqlite3")
    parser.add_argument("--db", type=Path, default=Path(default))
    args = parser.parse_args()
    print(json.dumps(diagnose(args.db.expanduser().resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
