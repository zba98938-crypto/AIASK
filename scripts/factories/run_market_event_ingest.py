#!/usr/bin/env python3
"""Run the real market-event ingest loop for Strategy Factory anchors.

This runner intentionally keeps CNINFO / public notice ingestion outside the
Strategy Factory production loop. It refreshes official notice-first market
events, persists normalized events, and bridges verified anchors into Strategy
Factory event tables through the existing AKShare ingest service.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_FACTORY_SRC = PROJECT_ROOT / "packages" / "strategy-factory" / "src"
if str(STRATEGY_FACTORY_SRC) not in sys.path:
    sys.path.insert(0, str(STRATEGY_FACTORY_SRC))

from strategy_factory.runtime_bootstrap import ensure_factory_runtime

ensure_factory_runtime(
    project_root=PROJECT_ROOT,
    script_path=Path(__file__).resolve(),
    argv=sys.argv[1:],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
    distribution_names=(
        "strategy-factory",
        "aiask-quant-core",
        "akshare-mcp",
    ),
    uv_project="packages/agent",
)
DEFAULT_INTERVAL_SEC = 3600
DEFAULT_ERROR_SLEEP_SEC = 300


def _configure_stdio_utf8() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception:
            pass
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        try:
            setattr(
                sys,
                stream_name,
                io.TextIOWrapper(
                    buffer,
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                ),
            )
        except Exception:
            pass


def _bootstrap_local_package_paths() -> dict:
    """Provider-neutral bootstrap.

    Injects whichever packages are present rather than hardcoding a data
    provider, so this runner works with a provider, with only the standalone
    provider set, and in a standalone repository.
    """
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from _runner_bootstrap import bootstrap_runner  # noqa: PLC0415

    return bootstrap_runner(Path(__file__), load_env=False)


BOOTSTRAP_REPORT = _bootstrap_local_package_paths()


def _load_dotenv() -> None:
    """Load env through whichever data provider is installed.

    Neutral replacement for the previous direct ``akshare_mcp.env_loader`` call.
    Absence of a provider is not an error: a standalone install has none, and the
    runtime contract then reports an honestly empty cycle.
    """
    from _runner_bootstrap import load_provider_env  # noqa: PLC0415

    load_provider_env(override=False)


_load_dotenv()


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("market_event_ingest_runner")


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except Exception:
        value = int(default)
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_codes(values: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for part in str(raw or "").replace(";", ",").split(","):
            code = part.strip()
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(code)
    return out


def _count_nested_ints(value: Any, keys: set[str]) -> int:
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key in keys:
                try:
                    total += int(item or 0)
                except Exception:
                    pass
            elif isinstance(item, (dict, list, tuple)):
                total += _count_nested_ints(item, keys)
        return total
    if isinstance(value, (list, tuple)):
        return sum(_count_nested_ints(item, keys) for item in value)
    return 0


def _compact_round_summary(result: dict[str, Any]) -> dict[str, Any]:
    fetched = dict(result.get("fetched") or {})
    saved = dict(result.get("saved") or {})
    normalized = dict(result.get("normalized_events") or {})
    bridge = dict(result.get("strategy_factory_bridge") or {})
    errors = list(result.get("errors") or [])
    totals = dict(result.get("totals") or {})
    return {
        "fetched": fetched,
        "saved_docs": int(totals.get("saved_docs") or _count_nested_ints(saved, {"documents", "candidate_docs"})),
        "normalized_total": _count_nested_ints(normalized, {"total"}),
        "normalized_verified": _count_nested_ints(normalized, {"verified"}),
        "normalized_provisional": _count_nested_ints(normalized, {"provisional"}),
        "bridge": bridge,
        "errors": errors[:10],
        "error_count": int(totals.get("errors") or len(errors)),
    }


def _build_ingest_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    codes = _normalize_codes(args.codes)
    return {
        "stock_codes": codes or None,
        "doc_types": ["notice"],
        "news_limit": 0,
        "notice_limit": max(0, int(args.notice_limit)),
        "official_notice_limit": max(0, int(args.official_notice_limit)),
        "notice_days": max(1, int(args.notice_days)),
        "code_notice_limit": max(0, int(args.code_notice_limit)),
        "research_code_limit": 0,
        "research_per_code": 0,
        "chunk_size": 1000,
        "overlap": 120,
        "version": "v1",
        "embed": False,
        "build_snapshot": False,
        "activate_snapshot": False,
        "allow_network": bool(args.allow_network),
        "dry_run": False,
    }


async def _run_one_round(args: argparse.Namespace, round_no: int) -> dict[str, Any]:
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.market_event_ingest import get_market_event_ingest_runtime

    ensure_default_runtime_services()
    kwargs = _build_ingest_kwargs(args)
    logger.info("round=%s starting args=%s", round_no, json.dumps(kwargs, ensure_ascii=False, default=str))
    started_at = datetime.now(timezone.utc)
    result = await get_market_event_ingest_runtime().run_once(**kwargs)
    elapsed_sec = int((datetime.now(timezone.utc) - started_at).total_seconds())
    summary = _compact_round_summary(result)
    summary["elapsed_sec"] = elapsed_sec
    logger.info("round=%s summary=%s", round_no, json.dumps(summary, ensure_ascii=False, default=str))
    return summary


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: int) -> None:
    if seconds <= 0 or stop_event.is_set():
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


async def _run(args: argparse.Namespace) -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(signum: int | None = None, _frame: object | None = None) -> None:
        name = signal.Signals(signum).name if signum is not None else "manual"
        logger.info("received %s; stopping market event ingest", name)
        loop.call_soon_threadsafe(stop_event.set)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except Exception:
            pass

    logger.info("=" * 80)
    logger.info("market event ingest runner started")
    logger.info("project_root=%s", PROJECT_ROOT)
    logger.info("interval_sec=%s error_sleep_sec=%s once=%s", args.interval, args.error_sleep, args.once)
    logger.info(
        "defaults=doc_types:notice official_notice_limit:%s notice_limit:%s code_notice_limit:%s "
        "news_limit:0 research_code_limit:0 embed:false build_snapshot:false allow_network:%s",
        args.official_notice_limit,
        args.notice_limit,
        args.code_notice_limit,
        args.allow_network,
    )
    logger.info("=" * 80)

    round_no = 0
    while not stop_event.is_set():
        round_no += 1
        try:
            summary = await _run_one_round(args, round_no)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("round=%s failed: %s: %s", round_no, type(exc).__name__, exc)
            if args.once:
                return 1
            await _sleep_or_stop(stop_event, max(1, int(args.error_sleep)))
            continue

        if args.once:
            return 0
        sleep_sec = max(1, int(args.error_sleep)) if int(summary.get("error_count") or 0) > 0 else max(1, int(args.interval))
        await _sleep_or_stop(stop_event, sleep_sec)

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run official notice-first market event ingest and Strategy Factory bridge.",
    )
    parser.add_argument("--once", action="store_true", help="Run one ingest round and exit.")
    parser.add_argument(
        "--interval",
        type=int,
        default=_env_int("MARKET_EVENT_INGEST_INTERVAL_SEC", DEFAULT_INTERVAL_SEC, minimum=1),
        help="Seconds between successful ingest rounds.",
    )
    parser.add_argument(
        "--codes",
        nargs="*",
        help="Optional stock codes. Accepts space-separated or comma-separated values.",
    )
    parser.add_argument(
        "--official-notice-limit",
        type=int,
        default=_env_int("MARKET_EVENT_INGEST_OFFICIAL_NOTICE_LIMIT", 30, minimum=0, maximum=1000),
        help="Maximum Tier A official notice documents per round.",
    )
    parser.add_argument(
        "--notice-limit",
        type=int,
        default=_env_int("MARKET_EVENT_INGEST_NOTICE_LIMIT", 40, minimum=0, maximum=1000),
        help="Maximum auxiliary market notice-head documents per round.",
    )
    parser.add_argument(
        "--code-notice-limit",
        type=int,
        default=_env_int("MARKET_EVENT_INGEST_CODE_NOTICE_LIMIT", 2, minimum=0, maximum=100),
        help="Maximum per-code auxiliary notice documents per round.",
    )
    parser.add_argument(
        "--notice-days",
        type=int,
        default=_env_int("MARKET_EVENT_INGEST_NOTICE_DAYS", 30, minimum=1, maximum=365),
        help="Notice lookback window in days.",
    )
    parser.add_argument(
        "--error-sleep",
        type=int,
        default=_env_int("MARKET_EVENT_INGEST_ERROR_SLEEP_SEC", DEFAULT_ERROR_SLEEP_SEC, minimum=1),
        help="Seconds to wait before retrying after a failed round.",
    )
    network_default = _as_bool(os.getenv("MARKET_EVENT_INGEST_ALLOW_NETWORK"), True)
    network_group = parser.add_mutually_exclusive_group()
    network_group.add_argument("--allow-network", dest="allow_network", action="store_true", help="Allow network fetches.")
    network_group.add_argument("--no-network", dest="allow_network", action="store_false", help="Disable network fetches.")
    parser.set_defaults(allow_network=network_default)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
