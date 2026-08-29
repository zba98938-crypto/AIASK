#!/usr/bin/env python3
"""核心市场数据补数脚本 (warmup task: ``core_market``).

Strategy Factory 的启动预热阶段会调用 ``await module._main(args)`` 来执行
此脚本，用于补齐 *指数 K 线 / 北向资金 / 融资融券* 等核心市场表。补数路径：

    1. 通过 ``TdxSyncService`` 跑 ``sync_index_klines`` 与
       ``sync_derived_factory_market_data``（从 TDX 原始表派生 north/margin）。
    2. 跑 ``sync_external_gap_data`` 调用 ``ExternalGapSyncService``，在
       ``ENABLE_EXTERNAL_GAP_FILL=1`` 的情况下用 akshare 免费接口补齐。
    3. 跑 ``record_tdx_data_completeness`` 把当前覆盖度回写到
       ``tdx_data_completeness``。

所有路径都对失败做 try/except，并以非零 ``exit_code`` 表示失败、便于
``sync_tasks`` 表中正确记录任务状态。

也可独立运行：

    python scripts/audit_sync_core_market_data.py

环境变量：
    AKSHARE_MCP_SQLITE_PATH   - SQLite 路径
    TDX_LOCAL_ONLY=1          - 仅使用本地 TDX
    TDX_SYNC_DISABLE=...      - 跳过任务（调度器路径已默认只跑核心市场任务）
    ENABLE_EXTERNAL_GAP_FILL  - 是否启用 akshare 免费接口补齐外部缺口
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _configure_stdio_utf8() -> None:
    """Wrap stdout/stderr with UTF-8 encoding for Windows GBK consoles.

    Only safe to call when running as a CLI; pytest captures stdout, and
    wrapping it during import would break pytest's capfd/capsys. Therefore
    this is invoked from ``main()`` rather than at module import time.
    """
    if sys.platform != "win32":
        return
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass


REPO = Path(__file__).resolve().parents[1]
for src in (
    REPO / "packages" / "akshare-mcp" / "src",
    REPO / "packages" / "strategy-factory" / "src",
    REPO / "packages" / "aiask-quant-core" / "src",
):
    s = str(src)
    if src.exists() and s not in sys.path:
        sys.path.insert(0, s)

logger = logging.getLogger("audit_sync_core_market_data")

# 默认补数股票池：沪深龙头 + 主要指数代表
DEFAULT_STOCK_CODES: list[str] = [
    "600519",  # 贵州茅台
    "601318",  # 中国平安
    "600036",  # 招商银行
    "000001",  # 平安银行
    "000651",  # 格力电器
    "300750",  # 宁德时代
    "002594",  # 比亚迪
    "601012",  # 隆基绿能
]

# 仅运行核心市场相关任务，跳过股票级别的重型任务以保持启动预热<5min
CORE_MARKET_TDX_TASKS = {
    "sync_index_klines",
    "sync_derived_factory_market_data",
    "sync_external_gap_data",
    "record_tdx_data_completeness",
}


def _normalize_codes(raw: Any) -> list[str]:
    """Accept comma-separated string, list, or None and produce a clean list."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        items = str(raw).replace(";", ",").split(",")
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        code = str(item or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def _resolve_universe(args: argparse.Namespace) -> list[str]:
    raw = getattr(args, "stock_codes", None)
    codes = _normalize_codes(raw)
    return codes or list(DEFAULT_STOCK_CODES)


async def _run_tdx_core_tasks(universe: list[str], *, warmup_mode: bool) -> dict[str, Any]:
    """Run only the index/north/margin/external-gap subset of TdxSyncService.

    In ``warmup_mode`` (the default for the runtime warmup path) this function
    forces ``ENABLE_EXTERNAL_GAP_FILL`` and ``TDX_ENABLE_EXTERNAL_GAP_FILL``
    to be treated as false, so the warmup never depends on a third-party
    network endpoint that could blow the 45s timeout. Standalone CLI users
    pass ``--full`` to opt back into external gap fill.
    """
    # Disable everything except the core-market subset, layered on top of any
    # user-provided ``TDX_SYNC_DISABLE``.
    from akshare_mcp.services.tdx_sync_service import TdxSyncService

    all_known = {
        "sync_trading_dates", "sync_stock_basic", "sync_quote_snapshots",
        "sync_index_klines", "sync_sector_basic", "sync_more_info",
        "sync_consensus", "sync_relation", "sync_gpjy_daily",
        "sync_bkjy_daily", "sync_scjy_daily", "sync_kzz_basic",
        "sync_ipo_events", "sync_divid_events", "sync_financial_pro",
        "sync_basic_financial", "sync_stock_fund_flow",
        "sync_derived_factory_market_data", "sync_external_gap_data",
        "record_tdx_data_completeness",
    }
    user_disabled = {
        t.strip() for t in os.environ.get("TDX_SYNC_DISABLE", "").split(",") if t.strip()
    }
    extra_disabled = (all_known - CORE_MARKET_TDX_TASKS) | user_disabled

    # Save and override env vars
    saved_env: dict[str, str | None] = {}

    def _override(name: str, value: str) -> None:
        saved_env[name] = os.environ.get(name)
        os.environ[name] = value

    _override("TDX_SYNC_DISABLE", ",".join(sorted(extra_disabled)))
    if warmup_mode:
        # Hard-off the external gap fill switch in warmup so a slow akshare
        # endpoint can't overrun the warmup runner's 45s timeout. Both env
        # var aliases must be neutralised — `external_gap_fill_enabled()`
        # accepts either.
        _override("ENABLE_EXTERNAL_GAP_FILL", "0")
        _override("TDX_ENABLE_EXTERNAL_GAP_FILL", "0")

    try:
        svc = TdxSyncService(universe=universe)
        return await svc.run_all()
    finally:
        for name, prev in saved_env.items():
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev


def _format_error(*, task_type: str, reason: str, **context: Any) -> str:
    """Build a structured, grep-friendly one-line error message.

    Format::

        [audit_sync_<task_type>] reason=<reason> task_type=<type> key1=v1 key2=v2

    All contextual values are best-effort stringified and truncated; the
    intent is that ``sync_tasks.error_message`` always carries a stable
    prefix that operators can grep for.
    """
    pieces = [f"reason={reason}", f"task_type={task_type}"]
    for key, value in context.items():
        text = "" if value is None else str(value)
        pieces.append(f"{key}={text[:200]}")
    return f"[audit_sync_{task_type}] " + " ".join(pieces)


async def _table_counts(tables: list[str]) -> dict[str, int]:
    from akshare_mcp.storage import get_db

    db = get_db()
    out: dict[str, int] = {}
    async with db.acquire() as conn:
        for name in tables:
            try:
                cnt = await conn.fetchval(f"SELECT COUNT(*) FROM {name}")
                out[name] = int(cnt or 0)
            except Exception as exc:
                out[name] = -1
                logger.debug("count(%s) failed: %s", name, exc)
    return out


async def _main(args: argparse.Namespace) -> int:
    started = datetime.now()
    # Same convention as audit_sync_factor_context_data.py: callers from the
    # data_sync_manager runtime path don't pass ``full`` or ``warmup_mode``,
    # so we default to warmup-mode unless the standalone CLI opts into ``--full``.
    full_mode = bool(getattr(args, "full", False))
    warmup_mode = bool(getattr(args, "warmup_mode", not full_mode))
    args.warmup_mode = warmup_mode

    logger.info(
        "[audit_sync_core_market_data] start mode=%s years=%s calendar_year=%s north_days=%s margin_days=%s",
        "warmup" if warmup_mode else "full",
        getattr(args, "years", None),
        getattr(args, "calendar_year", None),
        getattr(args, "north_days", None),
        getattr(args, "margin_days", None),
    )

    universe = _resolve_universe(args)
    print(f"[core_market] universe={len(universe)} codes (sample={universe[:3]})")

    failures: list[str] = []

    try:
        run_result = await _run_tdx_core_tasks(universe, warmup_mode=warmup_mode)
    except Exception as exc:
        logger.exception("TdxSyncService failed")
        msg = _format_error(
            task_type="core_market",
            reason="tdx_sync_service_exception",
            exception_type=type(exc).__name__,
            detail=str(exc),
            mode="warmup" if warmup_mode else "full",
        )
        print(f"[core_market] {msg}")
        run_result = {"summary": {"ok": 0, "failed": 1, "skipped": 0, "total": 1},
                      "tasks": [{"task": "tdx_sync_service", "ok": False,
                                 "error": msg}]}
        failures.append(msg)

    summary = run_result.get("summary") or {}
    print(
        f"[core_market] tdx tasks summary: "
        f"ok={summary.get('ok')} skipped={summary.get('skipped')} "
        f"failed={summary.get('failed')} total={summary.get('total')}"
    )
    for item in run_result.get("tasks") or []:
        name = item.get("task")
        if item.get("skipped"):
            continue
        if not item.get("ok"):
            err_msg = _format_error(
                task_type="core_market",
                reason=f"tdx_subtask_failed:{name}",
                detail=str(item.get("error") or "unknown"),
                mode="warmup" if warmup_mode else "full",
            )
            failures.append(err_msg)
            print(f"  - FAIL {name}: {err_msg}")
        else:
            stats = item.get("stats") or {}
            elapsed = item.get("elapsed_sec", 0)
            print(f"  - ok   {name:<36} {elapsed:>6.2f}s {stats}")

    counts = await _table_counts([
        "kline_1d",
        "north_fund_flow",
        "margin_market_flow",
        "margin_detail",
        "stock_quotes",
        "tdx_data_completeness",
    ])
    print("[core_market] table counts:")
    for tbl, cnt in counts.items():
        print(f"  - {tbl:<28} {cnt:>10}")

    elapsed = (datetime.now() - started).total_seconds()
    print(f"[core_market] done in {elapsed:.1f}s, failures={len(failures)}")

    return 0 if not failures else 1


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit / refresh core market data (indices, north fund flow, margin)."
    )
    parser.add_argument("--years", type=int, default=1)
    parser.add_argument("--stock-codes", "--stock_codes", dest="stock_codes",
                        type=str, default="")
    parser.add_argument("--calendar-year", "--calendar_year", dest="calendar_year",
                        type=int, default=datetime.now().year)
    parser.add_argument("--north-days", "--north_days", dest="north_days",
                        type=int, default=365)
    parser.add_argument("--margin-days", "--margin_days", dest="margin_days",
                        type=int, default=90)
    parser.add_argument("--full", action="store_true",
                        help="Disable warmup-mode caps and re-enable "
                             "ENABLE_EXTERNAL_GAP_FILL if set in the "
                             "environment. Default is warmup-mode (no external "
                             "gap fill) so the script fits inside "
                             "DATA_SYNC_CORE_MARKET_TIMEOUT_SEC=45s.")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    # 修复(2026-05-25): 加载 .env 让 ENABLE_EXTERNAL_GAP_FILL 等开关在子进程生效
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(REPO / ".env", override=False)
    except ImportError:
        # 没装 python-dotenv 时手动解析
        env_path = REPO / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = _build_arg_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
