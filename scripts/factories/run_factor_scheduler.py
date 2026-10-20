#!/usr/bin/env python3
"""Standalone FactorScheduler sidecar.

The four-factory supervisor owns the factory runtimes; FactorScheduler is a
separate provider service (daily at 18:00) and is not embedded in the MCP host
anymore.  Running it here keeps factor_values/factor_ic_history fresh without
starting a full MCP server.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package in ("akshare-mcp", "aiask-quant-core", "strategy-factory"):
    source = ROOT / "packages" / package / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def _run() -> None:
    from akshare_mcp.env_loader import load_mcp_env
    from akshare_mcp.services.factor_scheduler import FactorScheduler

    load_mcp_env(override=False)
    scheduler = FactorScheduler()
    scheduler.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    print(f"FactorScheduler sidecar started; daily run at {scheduler.run_time}", flush=True)
    await stop.wait()
    await scheduler.shutdown()


def main() -> None:
    _setup_logging()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
