#!/usr/bin/env python3
"""SignalTracker standalone runner."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


ERROR_BACKOFF_SEC = 300


def _configure_stdio_utf8() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_signal_tracker.py:30: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_signal_tracker.py:39: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
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
            except Exception as _swallowed:
                logger.warning("scripts/factories/run_signal_tracker.py:54: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_FACTORY_SRC = PROJECT_ROOT / "packages" / "strategy-factory" / "src"
if str(STRATEGY_FACTORY_SRC) not in sys.path:
    sys.path.insert(0, str(STRATEGY_FACTORY_SRC))

logger = logging.getLogger(__name__)


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


BOOTSTRAP_REPORT: dict | None = None


def _bootstrap_runtime() -> dict:
    """Preflight and inject local package paths before loading runtime providers."""
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
    return _bootstrap_local_package_paths()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _status_path() -> Path:
    explicit = str(os.getenv("AIASK_SIGNAL_TRACKER_STATUS_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime_home = str(os.getenv("AIASK_RUNTIME_HOME") or "").strip()
    base = Path(runtime_home).expanduser() if runtime_home else Path.home() / ".aiask"
    return base / "run" / "signal_tracker.status.json"


def _write_status(payload: dict) -> None:
    path = _status_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - status must never break a run
        logging.getLogger("signal_tracker_runner").warning(
            "SignalTracker: status write failed: %s", exc
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


def _build_runtime():
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime

    ensure_default_runtime_services()
    return get_signal_tracker_runtime()


async def _run_once_inner(verbose: bool = False) -> dict:
    logger = logging.getLogger("signal_tracker_runner")
    started_at = datetime.now().isoformat(timespec="seconds")
    try:
        tracker = _build_runtime()
        logger.info("SignalTracker: invoking run_once()")
        result = await tracker.run_once()
    except Exception as exc:  # noqa: BLE001 - persist failure before propagating
        _write_status({
            "name": "signal_tracker",
            "status": "failed",
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
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
    _write_status({
        "name": "signal_tracker",
        "status": "completed" if not errors else "degraded",
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "last_run_at": datetime.now().isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "summary": {
            "signals_generated": result.get("signals_generated"),
            "signal_event_snapshots": result.get("signal_event_snapshots"),
            "forward_returns_computed": result.get("forward_returns_computed"),
            "incubation_orders": result.get("incubation_orders"),
            "incubation_metrics": result.get("incubation_metrics"),
            "risk_events": result.get("risk_events"),
            "errors": len(errors),
        },
        "errors": list(errors)[:10],
    })
    return result


async def _run_once(args: argparse.Namespace) -> None:
    result = await _run_once_inner(verbose=args.verbose)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


async def _run_daemon(args: argparse.Namespace) -> None:
    logger = logging.getLogger("signal_tracker_runner")
    hh, mm = _parse_run_time(args.run_time)
    logger.info(
        "SignalTracker daemon started (run_time=%02d:%02d, retry_backoff=%ds)",
        hh,
        mm,
        ERROR_BACKOFF_SEC,
    )
    _write_status({
        "name": "signal_tracker",
        "status": "running",
        "pid": os.getpid(),
        "heartbeat_period_seconds": 60,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "run_time": f"{hh:02d}:{mm:02d}",
    })
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
            while wait_seconds > 0:
                _write_status({
                    "name": "signal_tracker",
                    "status": "running",
                    "pid": os.getpid(),
                    "heartbeat_period_seconds": 60,
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "run_time": f"{hh:02d}:{mm:02d}",
                })
                interval = min(60.0, wait_seconds)
                await asyncio.sleep(interval)
                wait_seconds -= interval
            try:
                await _run_once_inner(verbose=args.verbose)
            except Exception as exc:
                logger.exception("SignalTracker: run_once failed: %s", exc)
                logger.warning("SignalTracker: retrying in %ds", ERROR_BACKOFF_SEC)
                await asyncio.sleep(ERROR_BACKOFF_SEC)
                try:
                    await _run_once_inner(verbose=args.verbose)
                except Exception as exc2:
                    logger.exception("SignalTracker: retry also failed: %s", exc2)
    except KeyboardInterrupt:
        _write_status({
            "name": "signal_tracker",
            "status": "stopped",
            "pid": os.getpid(),
            "stopped_at": datetime.now().isoformat(timespec="seconds"),
        })
        print("\nSignalTracker daemon stopped")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SignalTracker daemon",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--daemon",
        action="store_true",
        help="daemon mode (default 18:00)",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="run once and exit",
    )
    parser.add_argument(
        "--run-time",
        default="18:00",
        help="daily daemon trigger time in HH:MM format",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable verbose logging",
    )

    args = parser.parse_args()
    _configure_stdio_utf8()
    _setup_logging(args.verbose)
    global BOOTSTRAP_REPORT
    BOOTSTRAP_REPORT = _bootstrap_runtime()

    if not args.once and not args.daemon:
        args.daemon = True

    if args.once:
        asyncio.run(_run_once(args))
    else:
        asyncio.run(_run_daemon(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
