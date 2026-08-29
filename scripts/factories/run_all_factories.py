#!/usr/bin/env python3
"""Entrypoint for the AIASK factory supervisor.

The maintained supervisor lives at ``scripts/factories/run_three_factories.py``.
This module keeps a small importable API for tests and tooling in this directory.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CHILD_OUTPUT_READ_CHUNK_SIZE = 65536
_CONSOLE_QUEUE: asyncio.Queue[str] | None = None
_CONSOLE_DROPPED = 0


@dataclass(frozen=True)
class FactorySpec:
    name: str
    script: Path
    args: tuple[str, ...] = ()
    log_name: str | None = None


@dataclass
class FactoryState:
    name: str
    pid: int | None = None
    started_at: datetime | None = None
    restart_count: int = 0
    last_exit_code: int | None = None
    status: str = "pending"
    last_output_at: datetime | None = None


def _console(message: str) -> None:
    global _CONSOLE_DROPPED
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}"
    queue = _CONSOLE_QUEUE
    if queue is None:
        print(line, flush=True)
        return
    try:
        queue.put_nowait(line)
    except asyncio.QueueFull:
        _CONSOLE_DROPPED += 1


def _script_path(name: str) -> Path:
    return SCRIPT_DIR / name


def _build_specs(args) -> list[FactorySpec]:
    specs: list[FactorySpec] = []

    if not getattr(args, "no_strategy", False):
        spec_args: list[str] = []
        strategy_interval = getattr(args, "strategy_interval", None)
        if strategy_interval is not None:
            spec_args.extend(["--interval", str(strategy_interval)])
        strategy_codes = list(getattr(args, "strategy_codes", None) or [])
        if strategy_codes:
            spec_args.extend(["--codes", *[str(code) for code in strategy_codes]])
        specs.append(
            FactorySpec(
                name="strategy_factory",
                script=_script_path("run_strategy_factory.py"),
                args=tuple(spec_args),
                log_name="strategy_factory.log",
            )
        )

    if not getattr(args, "no_factor", False):
        spec_args = []
        factor_interval = int(getattr(args, "factor_interval", 0) or 0)
        if factor_interval > 0:
            spec_args.extend(["--interval", str(factor_interval)])
        factor_candidates = getattr(args, "factor_candidates", None)
        if factor_candidates is not None:
            spec_args.extend(["--candidates", str(factor_candidates)])
        factor_generations = getattr(args, "factor_generations", None)
        if factor_generations is not None:
            spec_args.extend(["--generations", str(factor_generations)])
        factor_engines = list(getattr(args, "factor_engines", None) or [])
        if factor_engines:
            spec_args.extend(["--engines", *[str(engine) for engine in factor_engines]])
        factor_codes = list(getattr(args, "factor_codes", None) or [])
        if factor_codes:
            spec_args.extend(["--codes", *[str(code) for code in factor_codes]])
        specs.append(
            FactorySpec(
                name="factor_mining_factory",
                script=SCRIPT_DIR / "run_factor_mining_factory.py",
                args=tuple(spec_args),
                log_name="factor_mining_factory.log",
            )
        )

    if not getattr(args, "no_incubation", False):
        spec_args = ["--daemon", "--run-time", str(getattr(args, "incubation_run_time", "18:30"))]
        if getattr(args, "incubation_dry_run", False):
            spec_args.append("--dry-run")
        if getattr(args, "incubation_verbose", False):
            spec_args.append("--verbose")
        specs.append(
            FactorySpec(
                name="incubation_factory",
                script=SCRIPT_DIR / "run_incubation_factory.py",
                args=tuple(spec_args),
                log_name="incubation_factory.log",
            )
        )

    if not getattr(args, "no_signal_tracker", True):
        spec_args = ["--run-time", str(getattr(args, "signal_tracker_run_time", "17:00"))]
        if getattr(args, "signal_tracker_verbose", False):
            spec_args.append("--verbose")
        specs.append(
            FactorySpec(
                name="signal_tracker",
                script=SCRIPT_DIR / "run_signal_tracker.py",
                args=tuple(spec_args),
                log_name="signal_tracker.log",
            )
        )

    return specs


async def _tee_child_output(
    spec: FactorySpec,
    stream,
    log_file: TextIO,
    *,
    stream_logs: bool,
    state: FactoryState,
) -> None:
    while True:
        chunk = await stream.read(CHILD_OUTPUT_READ_CHUNK_SIZE)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
        log_file.write(text)
        state.last_output_at = datetime.now().astimezone()
        if stream_logs:
            _console(f"[{spec.name}] {text.rstrip()}")


def _load_supervisor_module():
    path = SCRIPT_DIR / "run_three_factories.py"
    spec = importlib.util.spec_from_file_location("_aiask_factory_supervisor", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load factory supervisor from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    return int(_load_supervisor_module().main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
