#!/usr/bin/env python3
"""Run Strategy Factory from the maintained package runtime.

This script is intentionally thin: process lifecycle belongs here, while the
factory implementation stays in ``packages/strategy-factory``.  It is safe by
default for trading operations: live/broker writes are disabled in the child
environment unless a caller has explicitly configured otherwise outside this
runner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SCRIPTS_DIR = SCRIPT_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import default_sqlite_path, ensure_runtime_dirs, runtime_home

DEFAULT_DB_PATH = default_sqlite_path()
DEFAULT_INTERVAL_SEC = 300


def _configure_import_paths() -> None:
    """Provider-neutral path setup.

    Injects whichever packages are present instead of naming a data provider, so
    this runner works with a provider, with only the standalone provider set, and
    in a standalone repository.
    """
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from _runner_bootstrap import bootstrap_paths  # noqa: PLC0415

    bootstrap_paths(PROJECT_ROOT)


def _configure_runtime_env() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    canonical_runtime_home = runtime_home()
    os.environ["AIASK_RUNTIME_HOME"] = str(canonical_runtime_home)
    ensure_runtime_dirs()
    if "AIASK_SQLITE_PATH" not in os.environ and "AKSHARE_MCP_SQLITE_PATH" not in os.environ:
        default_db_path = default_sqlite_path()
        os.environ["AIASK_SQLITE_PATH"] = str(default_db_path)
        os.environ["AKSHARE_MCP_SQLITE_PATH"] = str(default_db_path)
    os.environ["LIVE_TRADING_ENABLED"] = "0"
    os.environ["LIVE_TRADING_ALLOW_WRITE"] = "0"
    os.environ["BROKER_ALLOW_WRITE"] = "0"
    os.environ["LIVE_TRADING_READ_ONLY"] = "1"
    os.environ["BROKER_READ_ONLY"] = "1"


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_default(value: Any) -> str:
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _summary_error(data: dict[str, Any]) -> str:
    summary = data.get("summary")
    if isinstance(summary, dict):
        return str(summary.get("error") or "")
    return ""


def _normalize_cycle_result(result: Any, *, elapsed_seconds: float | None = None) -> dict[str, Any]:
    data = dict(result) if isinstance(result, dict) else {"status": "unknown", "result": result}
    if elapsed_seconds is not None:
        data["elapsed_seconds"] = elapsed_seconds
    status = str(data.get("status") or "unknown").lower()
    degraded = status.startswith("partial")
    if degraded:
        data["degraded"] = True
    product_success = status == "success"
    error = str(data.get("error") or _summary_error(data) or "")
    payload = {
        "success": product_success,
        "product_success": product_success,
        "process_healthy": True,
        "status": status,
        "data": data,
    }
    if error and not product_success:
        payload["error"] = error
    return payload


def _resolve_runner_interval_sec(value: int | None = None, *, now: datetime | None = None) -> int:
    if value is not None:
        return max(1, int(value))
    raw = os.getenv("STRATEGY_FACTORY_RUNNER_INTERVAL_SEC")
    if not raw:
        current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        local = current.astimezone(ZoneInfo("Asia/Shanghai"))
        in_market_hours = local.weekday() < 5 and (
            (local.hour == 9 and local.minute >= 30)
            or 10 <= local.hour < 15
            or (local.hour == 15 and local.minute == 0)
        )
        raw = os.getenv(
            "STRATEGY_FACTORY_MARKET_HOURS_INTERVAL_SEC"
            if in_market_hours
            else "STRATEGY_FACTORY_OFF_HOURS_INTERVAL_SEC"
        )
    try:
        return max(1, int(str(raw or DEFAULT_INTERVAL_SEC).strip()))
    except Exception:
        return DEFAULT_INTERVAL_SEC


def _resolve_db_path() -> Path:
    raw = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH") or str(DEFAULT_DB_PATH)
    return Path(raw).expanduser().resolve()


def _load_default_universe(limit: int | None) -> list[str]:
    db_path = _resolve_db_path()
    if not db_path.exists():
        return []
    max_rows = max(1, min(int(limit or 500), 5000))
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    try:
        rows = conn.execute(
            """
            SELECT stock_code
            FROM stocks
            WHERE COALESCE(stock_code, '') <> ''
            ORDER BY stock_code
            LIMIT ?
            """,
            (max_rows,),
        ).fetchall()
        return [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]
    finally:
        conn.close()


def _apply_dispatch_env(args: argparse.Namespace) -> None:
    if args.parallel_full_cycles:
        os.environ["STRATEGY_FACTORY_ALLOW_PARALLEL_FULL_CYCLES"] = "1"
    if args.dispatch_concurrency is not None:
        os.environ["STRATEGY_FACTORY_MAX_CONCURRENT_DISPATCHES"] = str(args.dispatch_concurrency)
    if args.execution_mode:
        os.environ["STRATEGY_FACTORY_EXECUTION_MODE"] = str(args.execution_mode)


def _load_strategy_factory_runtime_kwargs(db: Any | None = None) -> dict[str, Any]:
    _configure_import_paths()
    _configure_runtime_env()
    from strategy_factory.api.runtime import build_scheduler_runtime_kwargs
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services

    ensure_default_runtime_services()
    return build_scheduler_runtime_kwargs(db=db)


def _build_scheduler():
    _configure_import_paths()
    _configure_runtime_env()
    from strategy_factory.api import get_strategy_factory_scheduler
    from strategy_factory.api.runtime import build_scheduler_runtime_kwargs
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services

    ensure_default_runtime_services()
    return get_strategy_factory_scheduler(**build_scheduler_runtime_kwargs())


class _RunnerFacade:
    _load_strategy_factory_runtime_kwargs = staticmethod(_load_strategy_factory_runtime_kwargs)


_runner = _RunnerFacade()


class StrategyFactoryRunner:
    def __init__(
        self,
        *,
        interval_sec: int | None = None,
        execution_mode: str | None = None,
        target_codes: list[str] | None = None,
        dispatch_run: bool = False,
        run_once: bool = False,
        dispatch_run_mode: bool | None = None,
        dispatch_concurrency_limit: int | None = None,
        dispatch_shard_size: int | None = None,
        dispatch_default_universe: bool = False,
        dispatch_default_universe_limit: int | None = None,
    ) -> None:
        self.interval_sec = _resolve_runner_interval_sec(interval_sec)
        self.execution_mode = execution_mode
        self.target_codes = list(target_codes or [])
        self.dispatch_run = bool(dispatch_run if dispatch_run_mode is None else dispatch_run_mode)
        self.run_once_mode = bool(run_once)
        self.dispatch_concurrency_limit = dispatch_concurrency_limit
        self.dispatch_shard_size = max(1, int(dispatch_shard_size or 0)) if dispatch_shard_size else None
        self.dispatch_default_universe = bool(dispatch_default_universe)
        self.dispatch_default_universe_limit = dispatch_default_universe_limit
        self._run_count = 0
        self._success_count = 0
        self._partial_count = 0
        self._failure_count = 0
        self._active_dispatch: dict[str, Any] | None = None

    async def _execute_cycle(self) -> dict[str, Any]:
        runtime_kwargs = _load_strategy_factory_runtime_kwargs()
        scheduler = _build_scheduler()
        if self.dispatch_run:
            return await self._execute_dispatch_run_batch(scheduler, runtime_kwargs)
        await self._claim_queued_dispatch(runtime_kwargs)
        return await scheduler.run_once(
            execution_mode=self.execution_mode,
            target_codes=self.target_codes,
        )

    async def run_once(self) -> dict[str, Any]:
        started = datetime.now().astimezone()
        result = await self._execute_cycle()
        return _normalize_cycle_result(
            result,
            elapsed_seconds=(datetime.now().astimezone() - started).total_seconds(),
        )

    async def _claim_queued_dispatch(self, runtime_kwargs: dict[str, Any]) -> dict[str, Any] | None:
        db_provider = runtime_kwargs.get("db_provider")
        if not callable(db_provider):
            return None
        db = db_provider()
        list_dispatches = getattr(db, "list_strategy_factory_dispatches", None)
        update_dispatch = getattr(db, "update_strategy_factory_dispatch", None)
        if not callable(list_dispatches) or not callable(update_dispatch):
            return None
        rows = await list_dispatches(status="queued", limit=1)
        if not rows:
            return None
        dispatch = dict(rows[0])
        metadata = dict(dispatch.get("metadata") or {})
        metadata["runner"] = "standalone"
        claimed = await update_dispatch(
            dispatch_id=str(dispatch.get("dispatch_id")),
            status="running",
            metadata=metadata,
        )
        claimed = dict(claimed or {})
        claimed.setdefault("metadata", metadata)
        self._active_dispatch = {**claimed, "_db": db}
        return claimed

    async def _mark_active_dispatch_failed(self, error: BaseException) -> None:
        active = self._active_dispatch
        if not active:
            return
        db = active.get("_db")
        update_dispatch = getattr(db, "update_strategy_factory_dispatch", None)
        if callable(update_dispatch):
            metadata = dict(active.get("metadata") or {})
            metadata["cancelled"] = isinstance(error, asyncio.CancelledError)
            metadata["error_type"] = error.__class__.__name__
            await update_dispatch(
                dispatch_id=str(active.get("dispatch_id")),
                status="failed",
                error="cancelled" if isinstance(error, asyncio.CancelledError) else str(error),
                metadata=metadata,
            )
        self._active_dispatch = None

    async def _execute_dispatch_run_batch(self, scheduler: Any, runtime_kwargs: dict[str, Any]) -> dict[str, Any]:
        codes = list(self.target_codes)
        default_universe = False
        if self.dispatch_default_universe and not codes:
            db_provider = runtime_kwargs.get("db_provider")
            db = db_provider() if callable(db_provider) else None
            list_universe = getattr(db, "list_stock_universe", None)
            if callable(list_universe):
                rows = await list_universe(limit=int(self.dispatch_default_universe_limit or 500), offset=0)
                codes = [
                    str(row.get("code") or row.get("stock_code") or "").split(".", 1)[0]
                    for row in rows
                    if str(row.get("code") or row.get("stock_code") or "").strip()
                ]
                default_universe = True
        shard_size = self.dispatch_shard_size or max(1, len(codes) or 1)
        chunks = [codes[index : index + shard_size] for index in range(0, len(codes), shard_size)] or [[]]
        results = []
        for chunk in chunks:
            results.append(
                await scheduler.dispatch_run(
                    None,
                    execution_mode=self.execution_mode,
                    target_codes=chunk,
                )
            )
        failed = [item for item in results if str(item.get("status") or "").lower() not in {"success", "queued"}]
        return {
            "status": "failed" if failed else "success",
            "summary": {
                "dispatch_count": len(results),
                "default_universe_dispatch": default_universe,
                "default_universe_code_count": len(codes) if default_universe else 0,
            },
            "dispatches": results,
        }

    async def run(self) -> None:
        while True:
            try:
                result = await self.run_once()
            except asyncio.CancelledError as exc:
                await self._mark_active_dispatch_failed(exc)
                self._run_count += 1
                self._failure_count += 1
                return
            except Exception as exc:
                await self._mark_active_dispatch_failed(exc)
                self._run_count += 1
                self._failure_count += 1
                if self.run_once_mode:
                    return
            else:
                self._run_count += 1
                status = str(result.get("status") or "").lower()
                if result.get("success"):
                    self._success_count += 1
                elif status.startswith("partial"):
                    self._partial_count += 1
                else:
                    self._failure_count += 1
            if self.run_once_mode:
                return
            await asyncio.sleep(self.interval_sec)

    async def run_forever(self) -> None:
        while True:
            try:
                result = await self.run_once()
                print(json.dumps(result, ensure_ascii=False, default=_json_default), flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "error": str(exc),
                            "error_type": exc.__class__.__name__,
                            "ts": _timestamp(),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            await asyncio.sleep(self.interval_sec)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AIASK Strategy Factory.")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration without running a cycle.")
    parser.add_argument("--json", action="store_true", help="Print run result as JSON.")
    parser.add_argument("--interval", type=int, default=None, help="Loop interval seconds when not using --once.")
    parser.add_argument("--codes", nargs="*", default=None, help="Optional target stock codes.")
    parser.add_argument("--execution-mode", default=None, help="Strategy Factory execution mode override.")
    parser.add_argument("--dispatch-run", action="store_true", help="Queue via scheduler.dispatch_run instead of direct run_once.")
    parser.add_argument("--parallel-full-cycles", action="store_true", help="Allow bounded parallel full-cycle dispatches.")
    parser.add_argument("--dispatch-concurrency", type=int, default=None, help="Dispatch concurrency limit.")
    parser.add_argument("--dispatch-shard-size", type=int, default=None, help="Accepted for supervisor compatibility.")
    parser.add_argument("--dispatch-default-universe", action="store_true", help="Load target codes from stocks table.")
    parser.add_argument("--dispatch-default-universe-limit", type=int, default=None, help="Max default-universe codes.")
    return parser.parse_args(argv)


async def _run_async(args: argparse.Namespace) -> int:
    _configure_runtime_env()
    _apply_dispatch_env(args)
    target_codes = list(args.codes or [])
    if args.dispatch_default_universe and not target_codes:
        target_codes = _load_default_universe(args.dispatch_default_universe_limit)
    if args.dispatch_shard_size is not None:
        os.environ["STRATEGY_FACTORY_DISPATCH_SHARD_SIZE"] = str(args.dispatch_shard_size)

    if args.dry_run:
        payload = {
            "status": "dry_run",
            "db_path": str(_resolve_db_path()),
            "db_exists": _resolve_db_path().exists(),
            "execution_mode": args.execution_mode or os.getenv("STRATEGY_FACTORY_EXECUTION_MODE"),
            "dispatch_run": bool(args.dispatch_run),
            "parallel_full_cycles": bool(args.parallel_full_cycles),
            "dispatch_concurrency": args.dispatch_concurrency,
            "target_code_count": len(target_codes),
            "interval_sec": _resolve_runner_interval_sec(args.interval),
            "live_trading_enabled": os.getenv("LIVE_TRADING_ENABLED"),
            "broker_allow_write": os.getenv("BROKER_ALLOW_WRITE"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return 0 if payload["db_exists"] else 1

    runner = StrategyFactoryRunner(
        interval_sec=args.interval,
        execution_mode=args.execution_mode,
        target_codes=target_codes,
        dispatch_run=args.dispatch_run,
    )
    if args.once:
        result = await runner.run_once()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), flush=True)
        else:
            print(
                f"{_timestamp()} StrategyFactory run_once status={result.get('status')} "
                f"run_id={result.get('run_id')}",
                flush=True,
            )
        status = str(result.get("status") or "").lower()
        return 0 if status == "success" else 1

    await runner.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_import_paths()
    args = parse_args(argv)
    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
