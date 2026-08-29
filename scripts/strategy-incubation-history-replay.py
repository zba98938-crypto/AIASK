#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    return [value]


def _strings(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _has_sample_gap(payload: dict[str, Any]) -> bool:
    if bool(payload.get("has_sample_gap")):
        return True
    if "sample_gap" in _strings(payload.get("gap_categories")):
        return True
    for detail in _as_list(payload.get("blocker_details")):
        if isinstance(detail, dict) and str(detail.get("category") or "").strip() == "sample_gap":
            return True
    return False


def _load_strategy_ids_from_acceptance_report(report_path: Path, *, sample_gap_only: bool = False) -> list[str]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    strategy_ids: list[str] = []
    seen: set[str] = set()
    for item in _as_list(report.get("strategy_results")):
        if not isinstance(item, dict):
            continue
        strategy_id = str(item.get("strategy_id") or "").strip()
        if not strategy_id or strategy_id in seen:
            continue
        if sample_gap_only and not _has_sample_gap(item):
            continue
        seen.add(strategy_id)
        strategy_ids.append(strategy_id)
    return strategy_ids


def _summarize_acceptance(payload: dict[str, Any]) -> dict[str, Any]:
    acceptance_matrix = dict(payload.get("acceptance_matrix") or {})
    trade_audit_summary = dict(payload.get("trade_audit_summary") or {})
    blockers = _strings(payload.get("blockers"))
    gap_categories = _strings(payload.get("gap_categories"))
    gate_status = (
        str(payload.get("execution_audit_gate_status") or "").strip()
        or str(trade_audit_summary.get("execution_audit_gate_status") or "").strip()
        or None
    )
    overall_ready = bool(acceptance_matrix.get("overall_ready")) if "overall_ready" in acceptance_matrix else str(payload.get("status") or "") == "ready"
    return {
        "overall_ready": overall_ready,
        "has_sample_gap": _has_sample_gap({**payload, "gap_categories": gap_categories}),
        "execution_audit_gate_status": gate_status,
        "realized_trade_count": int(trade_audit_summary.get("realized_trade_count") or 0),
        "blockers": blockers,
        "gap_categories": gap_categories,
    }


def _summarize_replay_result(replay_result: dict[str, Any]) -> dict[str, Any]:
    items = [item for item in _as_list(replay_result.get("items")) if isinstance(item, dict)]
    acceptance_by_strategy: dict[str, dict[str, Any]] = {}
    error_strategy_ids: list[str] = []
    for item in items:
        strategy_id = str(item.get("strategy_id") or "").strip()
        if item.get("error") and strategy_id:
            error_strategy_ids.append(strategy_id)
        acceptance = item.get("acceptance")
        if strategy_id and isinstance(acceptance, dict):
            acceptance_by_strategy[strategy_id] = _summarize_acceptance(acceptance)
    return {
        "count": int(replay_result.get("count") or 0),
        "replayed_days": int(replay_result.get("replayed_days") or 0),
        "non_empty_days": int(replay_result.get("non_empty_days") or 0),
        "orders_created": int(replay_result.get("orders_created") or 0),
        "orders_filled": int(replay_result.get("orders_filled") or 0),
        "rejected_orders": int(replay_result.get("rejected_orders") or 0),
        "metrics_recorded": int(replay_result.get("metrics_recorded") or 0),
        "acceptance_status_counts": dict(replay_result.get("acceptance_status_counts") or {}),
        "execution_audit_gate_status_counts": dict(
            replay_result.get("execution_audit_gate_status_counts") or {}
        ),
        "execution_hard_gate_passed_count": int(
            replay_result.get("execution_hard_gate_passed_count") or 0
        ),
        "acceptance_overall_ready_count": int(
            replay_result.get("acceptance_overall_ready_count") or 0
        ),
        "acceptance_sample_gap_count": int(replay_result.get("acceptance_sample_gap_count") or 0),
        "acceptance_realized_trade_count_total": int(
            replay_result.get("acceptance_realized_trade_count_total") or 0
        ),
        "error_strategy_ids": error_strategy_ids,
        "acceptance_by_strategy": acceptance_by_strategy,
    }


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _resolve_default_source_db(repo: Path, explicit: str | None = None) -> Path:
    raw = (
        explicit
        or os.getenv("AKSHARE_MCP_SQLITE_PATH")
        or os.getenv("AIASK_SQLITE_PATH")
        or str(repo / "data" / "db" / "akshare_mcp.sqlite3")
    )
    return Path(raw).expanduser().resolve()


def _copy_sqlite_database(source_db: Path, shadow_db: Path, *, overwrite: bool = False) -> dict[str, Any]:
    source = source_db.expanduser().resolve()
    target = shadow_db.expanduser().resolve()
    if source == target:
        raise ValueError("source database and shadow database must be different")
    if not source.exists():
        raise FileNotFoundError(f"source database not found: {source}")
    if target.exists() and not overwrite:
        return {
            "status": "exists",
            "source_db": str(source),
            "shadow_db": str(target),
            "copied": False,
            "size_bytes": target.stat().st_size,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(str(target), timeout=60)) as dst:
            src.backup(dst)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
    return {
        "status": "copied",
        "source_db": str(source),
        "shadow_db": str(target),
        "copied": True,
        "size_bytes": target.stat().st_size,
    }


def _bootstrap_runtime() -> Path:
    repo = Path(__file__).resolve().parents[1]
    for rel in (
        ("packages", "aiask-quant-core", "src"),
        ("packages", "strategy-factory", "src"),
        ("packages", "akshare-mcp", "src"),
    ):
        path = repo.joinpath(*rel)
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from strategy_factory.runtime_bootstrap import ensure_factory_runtime

    ensure_factory_runtime(
        project_root=repo,
        script_path=Path(__file__).resolve(),
        argv=sys.argv[1:],
        editable_packages=(
            "packages/strategy-factory",
            "packages/aiask-quant-core",
            "packages/akshare-mcp",
        ),
    )
    return repo


async def _execute_history_replay(
    strategy_ids: list[str],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    include_market_days: bool = True,
    max_dates: int = 1500,
    force_close_open_positions: bool = False,
    run_acceptance: bool = True,
    source_db: str | None = None,
    shadow_db: str | None = None,
    overwrite_shadow_db: bool = False,
) -> dict[str, Any]:
    repo = _bootstrap_runtime()
    from akshare_mcp.env_loader import load_mcp_env
    from akshare_mcp.services.incubation import get_strategy_incubation_service
    from akshare_mcp.storage import close_db, get_db

    load_mcp_env(override=False)
    database_mode = "runtime"
    shadow_copy: dict[str, Any] | None = None
    if shadow_db:
        source_path = _resolve_default_source_db(repo, source_db)
        shadow_path = Path(shadow_db).expanduser().resolve()
        shadow_copy = _copy_sqlite_database(
            source_path,
            shadow_path,
            overwrite=bool(overwrite_shadow_db),
        )
        os.environ["AKSHARE_MCP_SQLITE_PATH"] = str(shadow_path)
        os.environ["AIASK_SQLITE_PATH"] = str(shadow_path)
        database_mode = "shadow"

    db = get_db()
    try:
        strategies = []
        missing_strategy_ids: list[str] = []
        for strategy_id in strategy_ids:
            strategy = await db.get_strategy(strategy_id)
            if strategy:
                strategies.append(strategy)
            else:
                missing_strategy_ids.append(strategy_id)
        service = get_strategy_incubation_service()
        replay_result = await service.replay_strategies_history(
            db,
            strategies,
            start_date=start_date,
            end_date=end_date,
            include_market_days=include_market_days,
            max_dates=max(1, int(max_dates or 1500)),
            force_close_open_positions=force_close_open_positions,
            run_acceptance=run_acceptance,
        )
        return {
            "strategy_ids": strategy_ids,
            "missing_strategy_ids": missing_strategy_ids,
            "database_mode": database_mode,
            "shadow_copy": shadow_copy,
            "replay_result": replay_result,
            "replay_summary": _summarize_replay_result(replay_result),
        }
    finally:
        await close_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Select sample-gap strategy ids from an execution audit acceptance report, "
            "or execute paper/history replay for those strategies."
        )
    )
    parser.add_argument("report", type=Path, nargs="?")
    parser.add_argument("--strategy-id", action="append", default=[], help="Strategy id to replay; can be repeated.")
    parser.add_argument("--sample-gap-only", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Run incubation history replay instead of only printing ids.")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--max-dates", type=int, default=1500)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--include-market-days", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force-close-open-positions", action="store_true")
    parser.add_argument("--run-acceptance", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--source-db", help="Source SQLite database to copy when --shadow-db is used.")
    parser.add_argument("--shadow-db", help="Copy the source DB here and execute replay against this shadow DB.")
    parser.add_argument("--overwrite-shadow-db", action="store_true")
    args = parser.parse_args(argv)

    strategy_ids = list(dict.fromkeys(_strings(args.strategy_id)))
    if args.report is not None:
        strategy_ids.extend(
            strategy_id
            for strategy_id in _load_strategy_ids_from_acceptance_report(
                args.report,
                sample_gap_only=args.sample_gap_only,
            )
            if strategy_id not in strategy_ids
        )
    strategy_ids = strategy_ids[: max(1, int(args.limit or 200))]
    if not strategy_ids and args.report is None:
        parser.error("provide a report path or at least one --strategy-id")

    if args.execute:
        result = asyncio.run(
            _execute_history_replay(
                strategy_ids,
                start_date=_parse_date(args.start_date),
                end_date=_parse_date(args.end_date),
                include_market_days=bool(args.include_market_days),
                max_dates=max(1, int(args.max_dates or 1500)),
                force_close_open_positions=bool(args.force_close_open_positions),
                run_acceptance=bool(args.run_acceptance),
                source_db=args.source_db,
                shadow_db=args.shadow_db,
                overwrite_shadow_db=bool(args.overwrite_shadow_db),
            )
        )
        if isinstance(result.get("replay_result"), dict) and "replay_summary" not in result:
            result["replay_summary"] = _summarize_replay_result(result["replay_result"])
    else:
        result = {"strategy_ids": strategy_ids}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
