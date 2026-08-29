#!/usr/bin/env python3
"""因子上下文补数脚本 (warmup task: ``factor_context``).

Strategy Factory 启动预热阶段会调用 ``await module._main(args)``，目标是
补齐 *新闻 / 公告 / 研报 / 个股资金流* 等因子上下文数据，供因子研究阶段使用。

数据落地：

    - market_documents / market_doc_chunks / vector_documents
      (通过 ``run_market_text_source_ingest``)
    - stock_fund_flow（通过 ``TdxSyncService.sync_stock_fund_flow``）

可独立运行：

    python scripts/audit_sync_factor_context_data.py --codes 600519,000001

调度器侧会传入：codes / scope_sources / active_pool_limit / task_run_limit /
news_days / notice_days / item_limit。
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

logger = logging.getLogger("audit_sync_factor_context_data")

# 默认 representative codes —— 与 core_market 保持一致
DEFAULT_REPRESENTATIVE_CODES: list[str] = [
    "600519", "601318", "600036", "000001", "000651",
    "300750", "002594", "601012",
]


def _normalize_codes(raw: Any) -> list[str]:
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


def _normalize_scope(raw: Any) -> set[str]:
    items = str(raw or "").replace(";", ",").split(",")
    return {item.strip().lower() for item in items if item.strip()}


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


async def _resolve_factory_target_codes(active_pool_limit: int) -> list[str]:
    """Pull recently-active strategy/factory targets from DB if available."""
    try:
        from akshare_mcp.storage import get_db
        db = get_db()
    except Exception:
        return []
    candidates: list[str] = []
    queries = [
        # Strategy factory most recent run targets
        (
            "SELECT DISTINCT json_extract(metadata,'$.target_codes') AS codes "
            "FROM strategy_factory_runs "
            "ORDER BY started_at DESC LIMIT ?",
            (max(1, int(active_pool_limit or 12)),),
        ),
    ]
    async with db.acquire() as conn:
        for sql, params in queries:
            try:
                rows = await conn.fetch(sql, *params)
            except Exception:
                continue
            for row in rows:
                value = row[0] if row else None
                for code in _normalize_codes(value):
                    candidates.append(code)
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for code in candidates:
        if code in seen:
            continue
        seen.add(code)
        deduped.append(code)
    return deduped[: max(1, int(active_pool_limit or 12))]


async def _resolve_codes(args: argparse.Namespace) -> list[str]:
    explicit = _normalize_codes(getattr(args, "codes", ""))
    scope = _normalize_scope(getattr(args, "scope_sources", "") or
                             "explicit,representative,active_pool,factory_targets")
    out: list[str] = []
    seen: set[str] = set()

    def _extend(items: list[str]) -> None:
        for code in items:
            if code in seen:
                continue
            seen.add(code)
            out.append(code)

    if "explicit" in scope and explicit:
        _extend(explicit)
    if "representative" in scope:
        _extend(DEFAULT_REPRESENTATIVE_CODES)
    if {"active_pool", "factory_targets"} & scope:
        try:
            factory_codes = await _resolve_factory_target_codes(
                int(getattr(args, "active_pool_limit", 12) or 12)
            )
            _extend(factory_codes)
        except Exception as exc:
            logger.warning("factory_targets resolution failed: %s", exc)

    if not out and explicit:
        _extend(explicit)
    if not out:
        _extend(DEFAULT_REPRESENTATIVE_CODES)
    return out


async def _ingest_market_text(args: argparse.Namespace, codes: list[str]) -> dict[str, Any]:
    """Run the public market text ingest pipeline (news / notice / research).

    The full pipeline calls Eastmoney via blocking ``requests.get`` and easily
    blocks the event loop for 60–90s — well beyond the warmup runner's 45s
    budget. In ``warmup_mode`` we therefore run a content-presence check only:
    we report current ``market_documents`` coverage and recommend the
    ``market_text_source_ingest`` scheduled task (or this script with
    ``--full``) for a real refresh.
    """
    from akshare_mcp.services.market_text_source_ingest import run_market_text_source_ingest
    from akshare_mcp.storage import get_db

    db = get_db()
    item_limit = int(getattr(args, "item_limit", 10) or 10)
    warmup_mode = bool(getattr(args, "warmup_mode", False))

    if warmup_mode:
        # Probe-only: surface current coverage without touching the network.
        async with db.acquire() as conn:
            news_docs = await conn.fetchval(
                "SELECT COUNT(*) FROM market_documents WHERE doc_type = 'news'"
            ) or 0
            notice_docs = await conn.fetchval(
                "SELECT COUNT(*) FROM market_documents WHERE doc_type = 'notice'"
            ) or 0
            research_docs = await conn.fetchval(
                "SELECT COUNT(*) FROM market_documents WHERE doc_type = 'research'"
            ) or 0
        return {
            "mode": "warmup_probe",
            "fetched": {},
            "saved": {},
            "snapshots": [],
            "errors": [],
            "totals": {
                "news_docs": int(news_docs),
                "notice_docs": int(notice_docs),
                "research_docs": int(research_docs),
            },
            "quality_flags": ["warmup_probe_only"],
            "note": "Use --full or schedule task_type=market_text_source_ingest for a real refresh.",
        }

    return await run_market_text_source_ingest(
        db,
        stock_codes=codes,
        doc_types=["news", "notice", "research"],
        news_limit=max(item_limit * 5, 30),
        notice_limit=max(item_limit * 8, 40),
        notice_days=int(getattr(args, "notice_days", 30) or 30),
        code_notice_limit=item_limit,
        code_notice_code_limit=max(len(codes), 10),
        research_code_limit=max(len(codes), 10),
        research_per_code=item_limit,
        chunk_size=1000,
        overlap=120,
        version="v1",
        embed=True,
        build_snapshot=True,
        activate_snapshot=True,
        allow_network=True,
        dry_run=False,
    )


async def _refresh_stock_fund_flow(codes: list[str]) -> dict[str, Any]:
    """Refresh stock_fund_flow via TdxSyncService when local TDX is available."""
    if not codes:
        return {"updated": 0, "skipped": "no_codes"}
    try:
        from akshare_mcp.services.tdx_sync_service import TdxSyncService
        from akshare_mcp.storage import get_db
    except Exception as exc:
        return {"updated": 0, "error": f"import_failed:{exc}"}
    svc = TdxSyncService(universe=codes)
    db = get_db()
    try:
        return await svc._sync_stock_fund_flow(db)
    except Exception as exc:
        logger.warning("sync_stock_fund_flow failed: %s", exc)
        return {"updated": 0, "error": f"{type(exc).__name__}: {exc}"}


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
    # The data_sync_manager runner constructs an argparse.Namespace without
    # ``full`` or ``warmup_mode`` and calls ``await module._main(args)``
    # directly, so by default we treat any invocation that didn't explicitly
    # opt into ``--full`` as a warmup-mode probe. Standalone CLI users can
    # still pass ``--full`` to run the full ingest pipeline.
    full_mode = bool(getattr(args, "full", False))
    warmup_mode = bool(getattr(args, "warmup_mode", not full_mode))
    args.warmup_mode = warmup_mode
    logger.info(
        "[audit_sync_factor_context_data] start mode=%s scope_sources=%s news_days=%s notice_days=%s item_limit=%s",
        "warmup" if warmup_mode else "full",
        getattr(args, "scope_sources", None),
        getattr(args, "news_days", None),
        getattr(args, "notice_days", None),
        getattr(args, "item_limit", None),
    )

    failures: list[str] = []

    codes = await _resolve_codes(args)
    print(f"[factor_context] resolved {len(codes)} codes (sample={codes[:3]})")

    # Hard ceiling on the network-bound ingest path. Warmup callers expect us
    # to finish well under 45s; a slow Eastmoney fetch shouldn't block the rest.
    ingest_budget_sec = float(
        os.getenv("AUDIT_SYNC_FACTOR_CONTEXT_INGEST_BUDGET_SEC",
                  "30" if warmup_mode else "1500")
        or (30.0 if warmup_mode else 1500.0)
    )

    # 1) market text ingest (news / notice / research)
    ingest_started = datetime.now()
    try:
        ingest_result = await asyncio.wait_for(
            _ingest_market_text(args, codes),
            timeout=ingest_budget_sec,
        )
        fetched = ingest_result.get("fetched") or {}
        saved = ingest_result.get("saved") or {}
        errors = ingest_result.get("errors") or []
        ingest_elapsed = (datetime.now() - ingest_started).total_seconds()
        print(
            f"[factor_context] market_text fetched={fetched} saved_keys={list(saved.keys())} "
            f"errors={len(errors)} elapsed={ingest_elapsed:.1f}s"
        )
        for err in errors[:5]:
            print(f"  - text-ingest error: {err}")
        if errors and not saved:
            failures.append(_format_error(
                task_type="factor_context",
                reason="market_text_no_saved_with_errors",
                error_count=len(errors),
                first_error=str(errors[0])[:200] if errors else "",
                mode="warmup" if warmup_mode else "full",
            ))
    except asyncio.TimeoutError:
        # In warmup mode this is a soft failure — we don't want to block the
        # factory cycle just because the public news endpoint is slow.
        ingest_elapsed = (datetime.now() - ingest_started).total_seconds()
        msg = _format_error(
            task_type="factor_context",
            reason="market_text_budget_exceeded",
            budget_sec=f"{ingest_budget_sec:g}",
            elapsed_sec=f"{ingest_elapsed:.1f}",
            mode="warmup" if warmup_mode else "full",
        )
        print(f"[factor_context] {msg} (soft-fail)")
        if not warmup_mode:
            failures.append(msg)
    except Exception as exc:
        logger.exception("market_text_source_ingest failed")
        ingest_elapsed = (datetime.now() - ingest_started).total_seconds()
        msg = _format_error(
            task_type="factor_context",
            reason="market_text_exception",
            exception_type=type(exc).__name__,
            detail=str(exc),
            elapsed_sec=f"{ingest_elapsed:.1f}",
            mode="warmup" if warmup_mode else "full",
        )
        print(f"[factor_context] {msg}")
        failures.append(msg)

    # 2) stock fund flow refresh (best-effort)
    try:
        ff = await _refresh_stock_fund_flow(codes)
        if ff.get("error"):
            print(f"[factor_context] stock_fund_flow soft-fail: {ff.get('error')}")
        else:
            print(f"[factor_context] stock_fund_flow: {ff}")
    except Exception as exc:
        logger.warning("stock_fund_flow refresh failed: %s", exc)
        print(f"[factor_context] stock_fund_flow soft-fail: {exc}")

    counts = await _table_counts([
        "market_documents",
        "market_doc_chunks",
        "vector_documents",
        "vector_collections",
        "stock_fund_flow",
    ])
    print("[factor_context] table counts:")
    for tbl, cnt in counts.items():
        print(f"  - {tbl:<28} {cnt:>10}")

    elapsed = (datetime.now() - started).total_seconds()
    print(f"[factor_context] done in {elapsed:.1f}s, failures={len(failures)}")
    return 0 if not failures else 1


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh factor research context (news / notice / research / fund flow)."
    )
    parser.add_argument("--codes", type=str, default="")
    parser.add_argument("--scope-sources", "--scope_sources", dest="scope_sources",
                        type=str, default="explicit,representative,active_pool,factory_targets")
    parser.add_argument("--active-pool-limit", "--active_pool_limit",
                        dest="active_pool_limit", type=int, default=12)
    parser.add_argument("--task-run-limit", "--task_run_limit",
                        dest="task_run_limit", type=int, default=50)
    parser.add_argument("--news-days", "--news_days", dest="news_days",
                        type=int, default=30)
    parser.add_argument("--notice-days", "--notice_days", dest="notice_days",
                        type=int, default=30)
    parser.add_argument("--item-limit", "--item_limit", dest="item_limit",
                        type=int, default=10)
    parser.add_argument("--full", action="store_true",
                        help="Disable warmup-mode caps and run a full ingest "
                             "with embedding + snapshot. Default is warmup-mode "
                             "to fit inside DATA_SYNC_FACTOR_CONTEXT_TIMEOUT_SEC=45s.")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = _build_arg_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
