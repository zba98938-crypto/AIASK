#!/usr/bin/env python3
"""SignalTracker 独立运行入口。

为什么需要这个入口:
    SignalTracker 在传统部署里依赖 MCP server 后台启动 (server.py:570),
    但我们的 ``run_all_factories.py`` 链路只起 strategy/factor/incubation
    三个独立工厂进程,从不启动 MCP server,因此 SignalTracker 永远没运行。

    后果: ``strategy_signals`` / ``strategy_signal_evidence`` 表始终为空,
    孵化工厂 Phase 3 拿不到任何 signal 做前向验证, warmup 账户的
    ``effective_n_5d`` 永远是 0,无法升 candidate。

    本 runner 单独把 ``SignalTracker.run_once()`` 包成一个独立的进程,
    时序上排在 ``incubation_factory`` 18:30 之前 (默认 18:00) ,这样
    一日流程是:
        18:00  SignalTracker → 写 strategy_signals + strategy_signal_evidence
        18:30  IncubationFactory → 读上面两张表,做前向验证

用法:
    python run_signal_tracker.py             # 守护进程(默认 18:00)
    python run_signal_tracker.py --once      # 单次执行
    python run_signal_tracker.py --run-time 18:00 --daemon
    python run_signal_tracker.py --verbose   # DEBUG 日志
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ERROR_BACKOFF_SEC = 300  # 5 minutes


def _ensure_src_path() -> None:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    for src in (
        repo_root / "packages" / "aiask-quant-core" / "src",
        repo_root / "packages" / "strategy-factory" / "src",
        repo_root / "packages" / "akshare-mcp" / "src",
    ):
        path = str(src)
        if src.exists() and path not in sys.path:
            sys.path.insert(0, path)


_ensure_src_path()

from strategy_factory.runtime_bootstrap import ensure_factory_runtime  # noqa: E402

ensure_factory_runtime(
    project_root=Path(__file__).resolve().parents[3],
    script_path=Path(__file__).resolve(),
    argv=sys.argv[1:],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_run_time(raw: str) -> tuple[int, int]:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"--run-time expects HH:MM, got {raw!r}")
    hh = int(parts[0])
    mm = int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"--run-time out of range: {raw!r}")
    return hh, mm


async def _run_once_inner(verbose: bool = False) -> dict:
    _ensure_src_path()
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime

    ensure_default_runtime_services()
    logger = logging.getLogger("signal_tracker_runner")
    tracker = get_signal_tracker_runtime()
    logger.info("SignalTracker: invoking run_once()")
    result = await tracker.run_once()
    logger.info(
        "SignalTracker: run_once finished | signals=%s snapshots=%s fwd_returns=%s "
        "incub_orders=%s incub_metrics=%s risk_events=%s errors=%s",
        result.get("signals_generated"),
        result.get("signal_event_snapshots"),
        result.get("forward_returns_computed"),
        result.get("incubation_orders"),
        result.get("incubation_metrics"),
        result.get("risk_events"),
        len(result.get("errors") or []),
    )
    if verbose:
        logger.debug("SignalTracker: full result = %s", json.dumps(result, default=str))
    errors = result.get("errors") or []
    if errors:
        for err in errors[:10]:
            logger.warning("SignalTracker: error: %s", err)
    return result


async def _run_once(args: argparse.Namespace) -> None:
    result = await _run_once_inner(verbose=args.verbose)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


async def _run_daemon(args: argparse.Namespace) -> None:
    _ensure_src_path()
    logger = logging.getLogger("signal_tracker_runner")
    hh, mm = _parse_run_time(args.run_time)
    logger.info(
        "SignalTracker daemon started (run_time=%02d:%02d, retry_backoff=%ds)",
        hh, mm, ERROR_BACKOFF_SEC,
    )
    try:
        while True:
            now = datetime.now()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(
                "SignalTracker: next run at %s (waiting %.0fs)",
                target.strftime("%Y-%m-%d %H:%M"),
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            try:
                await _run_once_inner(verbose=args.verbose)
            except Exception as exc:  # noqa: BLE001
                logger.exception("SignalTracker: run_once failed: %s", exc)
                logger.warning(
                    "SignalTracker: retrying in %ds", ERROR_BACKOFF_SEC,
                )
                await asyncio.sleep(ERROR_BACKOFF_SEC)
                try:
                    await _run_once_inner(verbose=args.verbose)
                except Exception as exc2:  # noqa: BLE001
                    logger.exception(
                        "SignalTracker: retry also failed: %s", exc2,
                    )
    except KeyboardInterrupt:
        print("\nSignalTracker daemon stopped")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SignalTracker daemon — 每日定时生成信号 + 前向收益回填",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true",
                      help="守护进程模式 (默认 18:00)")
    mode.add_argument("--once", action="store_true",
                      help="单次执行后退出")
    parser.add_argument("--run-time", default="18:00",
                        help="守护模式触发时间 HH:MM (默认 18:00)")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG 级日志")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    # Default to daemon mode if neither flag specified (matches incubation runner UX)
    if not args.once and not args.daemon:
        args.daemon = True

    if args.once:
        asyncio.run(_run_once(args))
    else:
        asyncio.run(_run_daemon(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
