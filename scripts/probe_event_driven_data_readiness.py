"""Probe whether tdx_relation / block_stocks / market_blocks / stocks / tdx_stock_extra
are populated in the production SQLite, so that we can decide whether
PR-B2 / PR-H of the event-driven plan is unblocked.

Read-only. Uses raw sqlite3 to avoid side effects.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from _integration_paths import default_sqlite_path


DB_PATH = default_sqlite_path()


def _row_to_dict(row):
    return dict(row) if row else None


def _safe_query(conn, sql, params=()):
    try:
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    except sqlite3.OperationalError as exc:
        return [{"__error__": str(exc)}]


def main() -> int:
    if not DB_PATH.exists():
        print(json.dumps({"error": f"db not found: {DB_PATH}"}, ensure_ascii=False, indent=2))
        return 1

    print(f"db: {DB_PATH}")
    print(f"size_bytes: {DB_PATH.stat().st_size}")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = {}

        # Table presence
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        report["table_presence"] = {
            name: (name in tables)
            for name in [
                "stocks",
                "market_blocks",
                "block_stocks",
                "tdx_relation",
                "tdx_stock_extra",
                "tdx_data_completeness",
                "strategy_factory_theme_nodes",
                "strategy_factory_theme_edges",
                "strategy_factory_event_injections",
                "strategy_factory_event_task_lineage",
                "strategy_factory_theme_exposure",
                "strategy_domain_events",
            ]
        }

        # Row counts and last update
        def count_and_last(table, ts_col="updated_at"):
            if table not in tables:
                return {"present": False}
            cnt = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            try:
                last = conn.execute(
                    f"SELECT max({ts_col}) AS t FROM {table}"
                ).fetchone()["t"]
            except sqlite3.OperationalError:
                last = None
            return {"present": True, "rows": cnt, "last_updated_at": last}

        report["stocks"] = count_and_last("stocks")
        report["market_blocks"] = count_and_last("market_blocks")
        report["block_stocks"] = count_and_last("block_stocks")
        report["tdx_relation"] = count_and_last("tdx_relation")
        report["tdx_stock_extra"] = count_and_last("tdx_stock_extra")

        # Distribution of block_type in tdx_relation
        if "tdx_relation" in tables:
            rows = _safe_query(
                conn,
                "SELECT block_type, count(*) AS n, count(DISTINCT block_code) AS bcodes "
                "FROM tdx_relation GROUP BY block_type ORDER BY n DESC",
            )
            report["tdx_relation_by_block_type"] = rows

            # Sample concept blocks
            concept_sample = _safe_query(
                conn,
                "SELECT block_code, block_name, count(*) AS members "
                "FROM tdx_relation WHERE block_type='概念' "
                "GROUP BY block_code, block_name ORDER BY members DESC LIMIT 10",
            )
            report["tdx_relation_concept_top10"] = concept_sample

            # Distinct stock coverage by tdx_relation
            cov = conn.execute(
                "SELECT count(DISTINCT code) AS covered FROM tdx_relation"
            ).fetchone()
            report["tdx_relation_covered_stocks"] = cov["covered"] if cov else None

        # market_blocks block_type breakdown (likely all 'tdx')
        if "market_blocks" in tables:
            rows = _safe_query(
                conn,
                "SELECT block_type, count(*) AS n FROM market_blocks "
                "GROUP BY block_type ORDER BY n DESC",
            )
            report["market_blocks_by_block_type"] = rows

        # block_stocks coverage
        if "block_stocks" in tables:
            cov = conn.execute(
                "SELECT count(DISTINCT block_code) AS bcodes, "
                "count(DISTINCT stock_code) AS scodes FROM block_stocks"
            ).fetchone()
            report["block_stocks_coverage"] = dict(cov) if cov else None

        # data_completeness for the two sync tasks
        if "tdx_data_completeness" in tables:
            rows = _safe_query(
                conn,
                "SELECT data_key, status, as_of_date, row_count, updated_at "
                "FROM tdx_data_completeness "
                "WHERE data_key IN ("
                "'sync_relation','sync_sector_basic','sync_stock_basic',"
                "'sync_more_info','tdx_relation','market_blocks','block_stocks'"
                ") ORDER BY data_key",
            )
            report["tdx_data_completeness_sync_keys"] = rows
            # all rows for visibility
            all_rows = _safe_query(
                conn,
                "SELECT data_key, status, as_of_date, row_count, updated_at "
                "FROM tdx_data_completeness ORDER BY data_key",
            )
            report["tdx_data_completeness_all"] = all_rows

        # stocks.industry / tdx_industry coverage (used as fallback in §6 Phase 6)
        if "stocks" in tables:
            cov = conn.execute(
                "SELECT "
                "count(*) AS total, "
                "sum(CASE WHEN industry IS NOT NULL AND industry <> '' THEN 1 ELSE 0 END) AS has_industry, "
                "sum(CASE WHEN tdx_industry IS NOT NULL AND tdx_industry <> '' THEN 1 ELSE 0 END) AS has_tdx_industry, "
                "sum(CASE WHEN tdx_region IS NOT NULL AND tdx_region <> '' THEN 1 ELSE 0 END) AS has_tdx_region "
                "FROM stocks"
            ).fetchone()
            report["stocks_field_coverage"] = dict(cov) if cov else None

        # event-driven schema readiness
        for t in [
            "strategy_factory_theme_nodes",
            "strategy_factory_theme_edges",
            "strategy_factory_event_injections",
            "strategy_factory_event_task_lineage",
            "strategy_factory_theme_exposure",
            "strategy_domain_events",
        ]:
            report[t] = count_and_last(t)

        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
