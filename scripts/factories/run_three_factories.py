#!/usr/bin/env python3
"""Four-runtime supervisor for the AIASK factories and market-event ingest.

Despite the filename, this supervises FOUR runtimes, not three. The name is
retained as a compatibility entrypoint (plugin commands and tests reference it
by path); the product-facing term is "four-runtime supervisor".

Do not describe this as the 24h quality-session path. Three distinct things are
easy to conflate:

- ``run_strategy_factory_quality_session.py --hours N`` — a bounded quality
  session, and the only one of the three whose success is about session evidence.
- this script — the long-running four-runtime process supervisor.
- ``hermes aiask factory cycle`` — one sequential pass over selected research
  factories.

This script owns only process lifecycle. The actual factory logic stays in the
four factory runners:

- scripts/factories/run_strategy_factory.py
- scripts/factories/run_factor_mining_factory.py
- scripts/factories/run_incubation_factory.py --daemon
- scripts/factories/run_market_event_ingest.py

Factor Catalog is not a daemon. The supervisor enables its toggle in the child
environment so Strategy Factory and MCP tools can use it when they build or
query the catalog. SignalTracker is a sidecar and is NOT one of the four.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SCRIPTS_DIR = SCRIPT_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import ensure_runtime_dirs, runtime_home
import logging

logger = logging.getLogger(__name__)

_LEGACY_LOG_DIR = runtime_home() / "logs" / "three_factories"
_LOG_DIR = runtime_home() / "logs" / "four_runtime_supervisor"

# Product-facing name is "four-runtime supervisor" (see CURRENT_PRODUCT_TRUTH).
# Keep writing to an existing legacy directory rather than orphaning its history:
# an operator mid-soak should not end up with logs split across two paths.
DEFAULT_LOG_DIR = _LEGACY_LOG_DIR if _LEGACY_LOG_DIR.is_dir() else _LOG_DIR
REQUIRED_SCRIPTS = (
    "run_strategy_factory.py",
    "run_factor_mining_factory.py",
    "run_incubation_factory.py",
    "run_market_event_ingest.py",
)

LLM_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "AIASK_AGENT_IMAGE_MODEL",
    "AIASK_AGENT_MODEL",
    "AIASK_AGENT_MODEL_PROVIDER",
    "AIASK_AGENT_MODEL_PROVIDERS",
    "AIASK_AGENT_MODEL_TIMEOUT",
    "AIASK_AGENT_CONTEXT_MAX_TOKENS",
    "AIASK_AGENT_MAX_TOKENS",
    "AIASK_AGENT_TTS_FORMAT",
    "AIASK_AGENT_TTS_PROVIDER",
    "AIASK_AGENT_TTS_VOICE",
    "AIASK_AGENT_VISION_MODEL",
    "AIASK_AGENT_VISION_PROVIDER",
    "AIASK_MOA_AGGREGATOR_MODEL",
    "AIASK_MOA_REFERENCE_MODELS",
    "AIASK_VIDEO_API_KEY",
    "AIASK_VIDEO_API_URL",
    "AIASK_VIDEO_BASE_URL",
    "AIASK_VIDEO_DURATION_SECONDS",
    "AIASK_VIDEO_MODEL",
    "AIASK_VIDEO_PROVIDER",
    "AIASK_VIDEO_SIZE",
    "AIASK_VOICE_STT_PROVIDER",
    "AIASK_VOICE_TTS_PROVIDER",
    "DEEPSEEK_API_KEY",
    "FINANCIAL_SEMANTIC_API_KEY",
    "FINANCIAL_SEMANTIC_BASE_URL",
    "FINANCIAL_SEMANTIC_CONNECT_TIMEOUT_SEC",
    "FINANCIAL_SEMANTIC_ENABLED",
    "FINANCIAL_SEMANTIC_MAX_DOCS",
    "FINANCIAL_SEMANTIC_MAX_TEXT_CHARS",
    "FINANCIAL_SEMANTIC_MODEL",
    "FINANCIAL_SEMANTIC_POOL_TIMEOUT_SEC",
    "FINANCIAL_SEMANTIC_PROVIDER",
    "FINANCIAL_SEMANTIC_TEMPERATURE",
    "FINANCIAL_SEMANTIC_TIMEOUT_SEC",
    "FINANCIAL_SEMANTIC_WRITE_TIMEOUT_SEC",
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "OPENAI_BASE_URL",
}

LLM_ENV_PREFIXES = (
    "AIASK_AGENT_MODEL_",
    "AIASK_AGENT_IMAGE_",
    "AIASK_AGENT_VISION_",
    "AIASK_AGENT_TTS_",
    "AIASK_MOA_",
    # AI_VALIDATION_* / STRATEGY_FACTORY_AI_VALIDATION_* dropped with the
    # ai_validation package (unreachable, config defaulted to disabled).
    "AIASK_VIDEO_",
    "AIASK_VOICE_",
    "ANTHROPIC_",
    "DASHSCOPE_",
    "DEEPSEEK_",
    "FACTOR_LLM_",
    "FINANCIAL_SEMANTIC_",
    "MOONSHOT_",
    "OPENAI_",
    "QWEN_",
    "STRATEGY_FACTORY_LLM_",
    "STRATEGY_EMBEDDING_",
    "STRATEGY_LLM_",
    "STRATEGY_PIPELINE_STAGE_",
    "ZHIPU_",
)


@dataclass(frozen=True)
class FactorySpec:
    name: str
    command: tuple[str, ...]
    log_name: str


@dataclass
class FactoryState:
    name: str
    pid: int | None = None
    started_at: datetime | None = None
    restart_count: int = 0
    last_exit_code: int | None = None
    status: str = "pending"


def _configure_stdio_utf8() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_three_factories.py:164: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_three_factories.py:173: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
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
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_three_factories.py:189: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _elapsed(started_at: datetime | None) -> int:
    if started_at is None:
        return 0
    return int((datetime.now(timezone.utc) - started_at).total_seconds())


def _load_dotenv(env: dict[str, str]) -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        if key in env and not _is_llm_env_key(key):
            continue
        env[key] = value.strip().strip('"').strip("'")


def _is_llm_env_key(key: str) -> bool:
    name = str(key or "").strip()
    return bool(name and (name in LLM_ENV_KEYS or any(name.startswith(prefix) for prefix in LLM_ENV_PREFIXES)))


def _env_disabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    _load_dotenv(env)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("FACTOR_MINING_FACTORY_ENABLED", "1")
    env.setdefault("STRATEGY_FACTORY_FACTOR_CATALOG_ENABLED", "1")
    env.setdefault("INCUBATION_FACTORY_OWNS_PAPER_TRADING", "true")
    env.setdefault("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    # 1500/轮:observe 积压上万时加速收敛(每策略孵化 ~0.05s,串行远低于 600s 上限)。
    env["INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT"] = "1500"
    env["INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED"] = "0"
    env.setdefault("INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT", "300")
    env.setdefault("INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE", "C")
    env.setdefault("STRATEGY_FACTORY_FACTOR_IC_GENERIC_INTAKE_ENABLED", "1")
    env.setdefault("STRATEGY_FACTORY_EVENT_RUNTIME_MODE", "refresh")
    env["STRATEGY_FACTORY_EXECUTION_MODE"] = "stock_first_observe_primary"
    env["STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED"] = "1"
    env["STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED"] = "1"
    env.setdefault("STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED", "0")
    env.setdefault("STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED", "1")
    env.setdefault("STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED", "1")
    env["STRATEGY_FACTORY_MIN_VALIDATION_GRADE"] = "C"
    env["STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED"] = "0"
    env["LIVE_TRADING_ENABLED"] = "0"
    env["LIVE_TRADING_ALLOW_WRITE"] = "0"
    env["BROKER_ALLOW_WRITE"] = "0"
    env["LIVE_TRADING_READ_ONLY"] = "1"
    env["BROKER_READ_ONLY"] = "1"
    return env


def _python_path(args: argparse.Namespace) -> str:
    configured = str(
        args.python
        or os.getenv("AIASK_FACTORY_PYTHON")
        or os.getenv("AIASK_VENV_PYTHON")
        or ""
    ).strip()
    if configured:
        return configured
    raise SystemExit(
        "Set --python, AIASK_FACTORY_PYTHON, or AIASK_VENV_PYTHON to a Python "
        ">=3.12,<3.14 AIASK venv. This supervisor does not fall back to the "
        "current interpreter or PATH python."
    )


def _script_path(name: str) -> str:
    return str(SCRIPT_DIR / name)


def _command_text(command: tuple[str, ...]) -> str:
    return subprocess.list2cmdline(list(command))


def _validate_preflight() -> list[str]:
    errors: list[str] = []
    for script in REQUIRED_SCRIPTS:
        path = SCRIPT_DIR / script
        if not path.exists():
            errors.append(f"missing required runner: {path}")
    return errors


def _build_specs(args: argparse.Namespace, env: dict[str, str]) -> list[FactorySpec]:
    python = _python_path(args)
    specs: list[FactorySpec] = []

    if not args.no_strategy:
        command = [python, "-u", _script_path("run_strategy_factory.py")]
        if args.strategy_dispatch_run:
            command.append("--dispatch-run")
        if args.strategy_parallel_full_cycles:
            command.append("--parallel-full-cycles")
        if args.strategy_dispatch_concurrency is not None:
            command.extend(["--dispatch-concurrency", str(args.strategy_dispatch_concurrency)])
        if args.strategy_dispatch_shard_size is not None:
            command.extend(["--dispatch-shard-size", str(args.strategy_dispatch_shard_size)])
        if args.strategy_dispatch_default_universe:
            command.append("--dispatch-default-universe")
        if args.strategy_dispatch_default_universe_limit is not None:
            command.extend([
                "--dispatch-default-universe-limit",
                str(args.strategy_dispatch_default_universe_limit),
            ])
        if args.strategy_execution_mode:
            command.extend(["--execution-mode", str(args.strategy_execution_mode)])
        if args.strategy_interval is not None:
            command.extend(["--interval", str(args.strategy_interval)])
        if args.strategy_codes:
            command.extend(["--codes", *args.strategy_codes])
        specs.append(
            FactorySpec(
                name="strategy_factory",
                command=tuple(command),
                log_name="strategy_factory.log",
            )
        )

    if not args.no_factor:
        command = [python, "-u", _script_path("run_factor_mining_factory.py")]
        if args.factor_interval > 0:
            command.extend(["--interval", str(args.factor_interval)])
        if args.factor_candidates is not None:
            command.extend(["--candidates", str(args.factor_candidates)])
        if args.factor_generations is not None:
            command.extend(["--generations", str(args.factor_generations)])
        if args.factor_engines:
            command.extend(["--engines", *args.factor_engines])
        if args.factor_codes:
            command.extend(["--codes", *args.factor_codes])
        specs.append(
            FactorySpec(
                name="factor_mining_factory",
                command=tuple(command),
                log_name="factor_mining_factory.log",
            )
        )

    if not args.no_incubation:
        command = [
            python,
            "-u",
            _script_path("run_incubation_factory.py"),
            "--daemon",
            "--run-time",
            args.incubation_run_time,
        ]
        if args.incubation_dry_run:
            command.append("--dry-run")
        if args.incubation_verbose:
            command.append("--verbose")
        specs.append(
            FactorySpec(
                name="incubation_factory",
                command=tuple(command),
                log_name="incubation_factory.log",
            )
        )

    if not args.no_event_ingest and not _env_disabled(env.get("MARKET_EVENT_INGEST_ENABLED")):
        specs.append(
            FactorySpec(
                name="market_event_ingest",
                command=(python, "-u", _script_path("run_market_event_ingest.py")),
                log_name="market_event_ingest.log",
            )
        )

    return specs


def _should_run_incubation_catchup(args: argparse.Namespace) -> bool:
    return not (
        args.no_incubation
        or args.incubation_dry_run
        or args.no_incubation_catchup
    )


def _build_incubation_catchup_command(args: argparse.Namespace) -> tuple[str, ...]:
    command = [
        _python_path(args),
        "-u",
        _script_path("run_incubation_factory.py"),
        "--json",
    ]
    if args.incubation_verbose:
        command.append("--verbose")
    return tuple(command)


def _write_startup_manifest(
    *,
    log_dir: Path,
    specs: list[FactorySpec],
    args: argparse.Namespace,
    env: dict[str, str],
) -> None:
    """Leave an on-disk record of every child the supervisor intends to own."""
    started_at = _timestamp()
    log_dir.mkdir(parents=True, exist_ok=True)
    planned = [
        {
            "name": spec.name,
            "log": str(log_dir / spec.log_name),
            "command": list(spec.command),
        }
        for spec in specs
    ]
    payload = {
        "started_at": started_at,
        "project_root": str(PROJECT_ROOT),
        "log_dir": str(log_dir),
        "restart": not args.no_restart,
        "market_event_ingest_enabled": not args.no_event_ingest
        and not _env_disabled(env.get("MARKET_EVENT_INGEST_ENABLED")),
        "mcp_service_expected": False,
        "mcp_service_command": None,
        "incubation_catchup": _command_text(_build_incubation_catchup_command(args))
        if _should_run_incubation_catchup(args)
        else None,
        "factories": planned,
    }
    (log_dir / "supervisor_startup.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (log_dir / "supervisor_startup.log").open("a", encoding="utf-8", buffering=1) as log_file:
        log_file.write("\n" + "=" * 100 + "\n")
        log_file.write(f"{started_at} planned supervisor startup\n")
        for spec in specs:
            log_file.write(
                f"{started_at} plan {spec.name} log={log_dir / spec.log_name} "
                f"command={_command_text(spec.command)}\n"
            )
        if payload["incubation_catchup"]:
            log_file.write(f"{started_at} plan incubation_catchup command={payload['incubation_catchup']}\n")
    for spec in specs:
        with (log_dir / spec.log_name).open("a", encoding="utf-8", buffering=1) as log_file:
            log_file.write("\n" + "=" * 100 + "\n")
            log_file.write(f"{started_at} supervisor planned {spec.name}\n")
            log_file.write("command: " + _command_text(spec.command) + "\n")
            log_file.write("=" * 100 + "\n")


def _write_execution_record(log_dir: Path, event_type: str, **fields: object) -> None:
    """Append a machine-readable supervisor event for 24h acceptance audits."""
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _timestamp(),
        "event_type": event_type,
        **fields,
    }
    with (log_dir / "execution_records.jsonl").open("a", encoding="utf-8", buffering=1) as record_file:
        record_file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _write_planned_execution_records(
    *,
    log_dir: Path,
    specs: list[FactorySpec],
    args: argparse.Namespace,
    env: dict[str, str],
) -> None:
    _write_execution_record(
        log_dir,
        "supervisor_plan",
        project_root=str(PROJECT_ROOT),
        restart=not args.no_restart,
        mcp_service_expected=False,
        mcp_service_command=None,
        market_event_ingest_enabled=not args.no_event_ingest
        and not _env_disabled(env.get("MARKET_EVENT_INGEST_ENABLED")),
        factory_count=len(specs),
    )
    for spec in specs:
        _write_execution_record(
            log_dir,
            "factory_planned",
            factory=spec.name,
            command=list(spec.command),
            log=str(log_dir / spec.log_name),
            mcp_service_expected=False,
        )
    if _should_run_incubation_catchup(args):
        _write_execution_record(
            log_dir,
            "incubation_catchup_planned",
            command=list(_build_incubation_catchup_command(args)),
            log=str(log_dir / "incubation_factory_catchup.log"),
        )


def _write_header(log_file: TextIO, spec: FactorySpec) -> None:
    log_file.write("\n" + "=" * 100 + "\n")
    log_file.write(f"{_timestamp()} starting {spec.name}\n")
    log_file.write("command: " + _command_text(spec.command) + "\n")
    log_file.write("=" * 100 + "\n")
    log_file.flush()


async def _run_incubation_catchup(
    args: argparse.Namespace,
    *,
    env: dict[str, str],
    log_dir: Path,
    quiet: bool,
) -> None:
    command = _build_incubation_catchup_command(args)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "incubation_factory_catchup.log"
    _write_execution_record(
        log_dir,
        "incubation_catchup_start",
        command=list(command),
        log=str(log_path),
    )
    print(
        f"{_timestamp()} [incubation_factory_catchup] starting "
        f"{subprocess.list2cmdline(list(command))} log={log_path}",
        flush=True,
    )
    with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
        log_file.write("\n" + "=" * 100 + "\n")
        log_file.write(f"{_timestamp()} starting incubation catch-up\n")
        log_file.write("command: " + subprocess.list2cmdline(list(command)) + "\n")
        log_file.write("=" * 100 + "\n")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        while True:
            chunk = await process.stdout.readline()
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace").rstrip()
            log_file.write(text + "\n")
            if not quiet:
                print(f"{_timestamp()} [incubation_factory_catchup] {text}", flush=True)
        exit_code = await process.wait()
        log_file.write(f"{_timestamp()} catch-up exited code={exit_code}\n")
    _write_execution_record(
        log_dir,
        "incubation_catchup_exit",
        exit_code=exit_code,
        log=str(log_path),
    )
    if exit_code == 0:
        print(f"{_timestamp()} [incubation_factory_catchup] completed", flush=True)
    else:
        print(
            f"{_timestamp()} [incubation_factory_catchup] warning: exited code={exit_code}; "
            "continuing daemon startup",
            file=sys.stderr,
            flush=True,
        )


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError as _swallowed:
        logger.warning("scripts/factories/run_three_factories.py:578: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return
    except Exception as _swallowed:
        logger.warning("scripts/factories/run_three_factories.py:580: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
        return
    except asyncio.TimeoutError as _swallowed:
        logger.warning("scripts/factories/run_three_factories.py:585: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    if sys.platform == "win32" and process.pid is not None:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
            return
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_three_factories.py:600: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    try:
        process.kill()
    except ProcessLookupError as _swallowed:
        logger.warning("scripts/factories/run_three_factories.py:604: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return
    except Exception as _swallowed:
        logger.warning("scripts/factories/run_three_factories.py:606: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except Exception as _swallowed:
        logger.warning("scripts/factories/run_three_factories.py:610: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass


async def _run_factory(
    spec: FactorySpec,
    *,
    env: dict[str, str],
    log_dir: Path,
    restart_delay_sec: int,
    restart: bool,
    quiet: bool,
    stop_event: asyncio.Event,
    state: FactoryState,
) -> None:
    log_path = log_dir / spec.log_name
    while not stop_event.is_set():
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
            _write_header(log_file, spec)
            state.status = "starting"
            process = await asyncio.create_subprocess_exec(
                *spec.command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            state.pid = process.pid
            state.started_at = datetime.now(timezone.utc)
            state.status = "running"
            _write_execution_record(
                log_dir,
                "factory_start",
                factory=spec.name,
                pid=process.pid,
                restart_count=state.restart_count,
                command=list(spec.command),
                log=str(log_path),
                mcp_service_expected=False,
            )
            print(
                f"{_timestamp()} [{spec.name}] started pid={process.pid} log={log_path}",
                flush=True,
            )

            assert process.stdout is not None
            try:
                while True:
                    chunk = await process.stdout.readline()
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace").rstrip()
                    log_file.write(text + "\n")
                    if not quiet:
                        print(f"{_timestamp()} [{spec.name}] {text}", flush=True)
            except asyncio.CancelledError:
                await _terminate_process(process)
                raise
            finally:
                if stop_event.is_set() and process.returncode is None:
                    await _terminate_process(process)
                exit_code = await process.wait()
                state.last_exit_code = exit_code
                state.pid = None
                state.status = "stopped" if stop_event.is_set() else "exited"
                _write_execution_record(
                    log_dir,
                    "factory_exit",
                    factory=spec.name,
                    exit_code=exit_code,
                    status=state.status,
                    uptime_seconds=_elapsed(state.started_at),
                    restart_count=state.restart_count,
                    log=str(log_path),
                )
                print(
                    f"{_timestamp()} [{spec.name}] exited code={exit_code} "
                    f"uptime={_elapsed(state.started_at)}s",
                    flush=True,
                )

        if stop_event.is_set() or not restart:
            break
        state.restart_count += 1
        state.status = "waiting_restart"
        _write_execution_record(
            log_dir,
            "factory_restart_scheduled",
            factory=spec.name,
            restart_count=state.restart_count,
            restart_delay_sec=restart_delay_sec,
            last_exit_code=state.last_exit_code,
        )
        await _sleep_or_stop(stop_event, restart_delay_sec)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: int) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError as _swallowed:
        logger.warning("scripts/factories/run_three_factories.py:712: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return


async def _heartbeat(
    states: dict[str, FactoryState],
    *,
    log_dir: Path,
    stop_event: asyncio.Event,
    interval_sec: int,
) -> None:
    if interval_sec <= 0:
        return
    while not stop_event.is_set():
        await _sleep_or_stop(stop_event, interval_sec)
        if stop_event.is_set():
            return
        parts = []
        for state in states.values():
            pid = state.pid if state.pid is not None else "-"
            parts.append(
                f"{state.name}: status={state.status} pid={pid} "
                f"uptime={_elapsed(state.started_at)}s restarts={state.restart_count}"
            )
        _write_execution_record(
            log_dir,
            "supervisor_heartbeat",
            factories=[
                {
                    "factory": state.name,
                    "status": state.status,
                    "pid": state.pid,
                    "uptime_seconds": _elapsed(state.started_at),
                    "restart_count": state.restart_count,
                    "last_exit_code": state.last_exit_code,
                }
                for state in states.values()
            ],
            mcp_service_expected=False,
        )
        print(f"{_timestamp()} [supervisor] " + " | ".join(parts), flush=True)


async def _run(args: argparse.Namespace) -> int:
    preflight_errors = _validate_preflight()
    if preflight_errors:
        for error in preflight_errors:
            print(error, file=sys.stderr)
        return 2

    env = _child_env()
    specs = _build_specs(args, env)
    if not specs:
        print("No factory selected.", file=sys.stderr)
        return 2

    log_dir = Path(args.log_dir).resolve()

    print("=" * 100)
    print(f"{_timestamp()} AIASK factory supervisor")
    print(f"{_timestamp()} project_root: {PROJECT_ROOT}")
    print(f"{_timestamp()} log_dir:      {log_dir}")
    print(f"{_timestamp()} restart:      {not args.no_restart}")
    print(f"{_timestamp()} factor_catalog_enabled: {env.get('STRATEGY_FACTORY_FACTOR_CATALOG_ENABLED')}")
    print(
        f"{_timestamp()} trade_prediction_hard_controls: "
        f"promotion={env.get('STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED')} "
        f"budget={env.get('STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED')} "
        f"factor_decay={env.get('STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED')}"
    )
    print(
        f"{_timestamp()} gate3: "
        f"record_only={env.get('STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED')} "
        f"min_validation_grade={env.get('STRATEGY_FACTORY_MIN_VALIDATION_GRADE')}"
    )
    print(
        f"{_timestamp()} incubation_gate3_record_only_audit_mirror: "
        f"enabled={env.get('INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED')} "
        f"min_grade={env.get('INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE')} "
        f"batch_limit={env.get('INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT')}"
    )
    print(
        f"{_timestamp()} live_trading_writes: "
        f"LIVE_TRADING_ENABLED={env.get('LIVE_TRADING_ENABLED')} "
        f"LIVE_TRADING_ALLOW_WRITE={env.get('LIVE_TRADING_ALLOW_WRITE')} "
        f"BROKER_ALLOW_WRITE={env.get('BROKER_ALLOW_WRITE')}"
    )
    print(f"{_timestamp()} factories:")
    for spec in specs:
        print(f"{_timestamp()}   - {spec.name}: {_command_text(spec.command)}")
    if _should_run_incubation_catchup(args):
        catchup_command = _build_incubation_catchup_command(args)
        print(
            f"{_timestamp()} incubation_catchup: "
            f"{_command_text(catchup_command)}"
        )
    elif not args.no_incubation:
        print(f"{_timestamp()} incubation_catchup: skipped")
    print("=" * 100, flush=True)

    if args.dry_run:
        return 0

    _write_startup_manifest(log_dir=log_dir, specs=specs, args=args, env=env)
    _write_planned_execution_records(log_dir=log_dir, specs=specs, args=args, env=env)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(signum: int | None = None, _frame: object | None = None) -> None:
        name = signal.Signals(signum).name if signum is not None else "manual"
        _write_execution_record(log_dir, "supervisor_stop_requested", signal=name)
        print(f"\n{_timestamp()} received {name}; stopping factories...", flush=True)
        loop.call_soon_threadsafe(stop_event.set)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_three_factories.py:830: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

    states = {spec.name: FactoryState(name=spec.name) for spec in specs}
    tasks = [
        asyncio.create_task(
            _run_factory(
                spec,
                env=env,
                log_dir=log_dir,
                restart_delay_sec=max(1, int(args.restart_delay)),
                restart=not args.no_restart,
                quiet=args.quiet,
                stop_event=stop_event,
                state=states[spec.name],
            ),
            name=f"factory:{spec.name}",
        )
        for spec in specs
    ]
    if _should_run_incubation_catchup(args):
        async def run_catchup_background() -> None:
            try:
                await _run_incubation_catchup(
                    args,
                    env=env,
                    log_dir=log_dir,
                    quiet=args.quiet,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"{_timestamp()} [incubation_factory_catchup] warning: {type(exc).__name__}: {exc}; "
                    "continuing supervised factory startup",
                    file=sys.stderr,
                    flush=True,
                )

        tasks.append(
            asyncio.create_task(
                run_catchup_background(),
                name="factory:incubation_catchup",
            )
        )
    tasks.append(
        asyncio.create_task(
            _heartbeat(
                states,
                log_dir=log_dir,
                stop_event=stop_event,
                interval_sec=max(0, int(args.heartbeat_interval)),
            ),
            name="supervisor:heartbeat",
        )
    )

    try:
        await stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _write_execution_record(
            log_dir,
            "supervisor_stopped",
            factories=[
                {
                    "factory": state.name,
                    "status": state.status,
                    "pid": state.pid,
                    "restart_count": state.restart_count,
                    "last_exit_code": state.last_exit_code,
                }
                for state in states.values()
            ],
            mcp_service_expected=False,
        )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start Strategy Factory, Factor Mining Factory, Incubation Factory, and market event ingest.",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python >=3.12,<3.14 AIASK venv for child runners; falls back only to AIASK_FACTORY_PYTHON or AIASK_VENV_PYTHON.",
    )
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="Child log directory.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without starting children.")
    parser.add_argument("--quiet", action="store_true", help="Do not mirror child logs to console.")
    parser.add_argument("--no-restart", action="store_true", help="Do not restart a child after exit.")
    parser.add_argument("--restart-delay", type=int, default=30, help="Restart delay in seconds.")
    parser.add_argument("--heartbeat-interval", type=int, default=60, help="Supervisor heartbeat seconds; 0 disables.")

    parser.add_argument("--no-strategy", action="store_true", help="Do not start Strategy Factory.")
    parser.add_argument("--strategy-interval", type=int, default=None, help="Strategy Factory interval seconds.")
    parser.add_argument("--strategy-codes", nargs="*", help="Optional Strategy Factory target codes.")
    parser.add_argument("--strategy-execution-mode", default=None, help="Strategy Factory execution mode.")
    parser.add_argument(
        "--strategy-dispatch-run",
        action="store_true",
        help="Start Strategy Factory through dispatch_run mode.",
    )
    parser.add_argument(
        "--strategy-parallel-full-cycles",
        action="store_true",
        help="Enable bounded full-cycle dispatch concurrency for Strategy Factory.",
    )
    parser.add_argument(
        "--strategy-dispatch-concurrency",
        type=int,
        default=None,
        help="Strategy Factory dispatch concurrency limit.",
    )
    parser.add_argument(
        "--strategy-dispatch-shard-size",
        type=int,
        default=None,
        help="Number of strategy target codes per dispatch batch.",
    )
    parser.add_argument(
        "--strategy-dispatch-default-universe",
        action="store_true",
        help="When strategy codes are omitted, load the default stock universe and shard it into dispatches.",
    )
    parser.add_argument(
        "--strategy-dispatch-default-universe-limit",
        type=int,
        default=None,
        help="Max default stock-universe codes to load for strategy dispatch sharding.",
    )

    parser.add_argument("--no-factor", action="store_true", help="Do not start Factor Mining Factory.")
    parser.add_argument("--factor-interval", type=int, default=0, help="Factor Mining fixed interval seconds; 0 uses schedule mode.")
    parser.add_argument("--factor-candidates", type=int, default=None, help="Factor candidates per cycle.")
    parser.add_argument("--factor-generations", type=int, default=None, help="Factor evolution generations per cycle.")
    parser.add_argument("--factor-engines", nargs="*", help="Optional factor engines.")
    parser.add_argument("--factor-codes", nargs="*", help="Optional Factor Mining target codes.")

    parser.add_argument("--no-incubation", action="store_true", help="Do not start Incubation Factory.")
    parser.add_argument("--incubation-run-time", default="18:30", help="Daily incubation run time, HH:MM.")
    parser.add_argument("--incubation-dry-run", action="store_true", help="Run Incubation daemon in dry-run mode.")
    parser.add_argument("--incubation-verbose", action="store_true", help="Enable verbose incubation logs.")
    parser.add_argument(
        "--no-incubation-catchup",
        action="store_true",
        help="Skip the one-shot incubation --json catch-up before starting the daemon.",
    )
    parser.add_argument(
        "--no-event-ingest",
        action="store_true",
        help="Do not start the real market-event ingest runner.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    ensure_runtime_dirs()
    args = parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
