"""Fixed-runner subprocess worker for persisted factory runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiask_quant_core.storage import close_db, get_db


RUNNER_FILES = {
    "strategy_factory": "run_strategy_factory.py",
    "factor_mining": "run_factor_mining_factory.py",
    "incubation": "run_incubation_factory.py",
    "market_event_ingest": "run_market_event_ingest.py",
}


def _project_root() -> Path:
    explicit = str(os.getenv("AIASK_PROJECT_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(__file__).resolve().parents[4]


def _runner_command(factory: str, params: dict[str, Any]) -> list[str]:
    script = _project_root() / "scripts" / "factories" / RUNNER_FILES[factory]
    if not script.is_file():
        raise FileNotFoundError(f"factory runner missing: {script}")
    command = [sys.executable, str(script)]
    if factory == "strategy_factory":
        command.extend(["--once", "--json"])
        if params.get("dry_run"):
            command.append("--dry-run")
        if params.get("codes"):
            command.extend(["--codes", *[str(item) for item in list(params["codes"])[:1000]]])
    elif factory == "factor_mining":
        command.append("--once")
        if params.get("candidate_count") is not None:
            command.extend(["--candidates", str(max(1, min(int(params["candidate_count"]), 1000)))])
        if params.get("evolution_generations") is not None:
            command.extend(["--generations", str(max(0, min(int(params["evolution_generations"]), 100)))])
        if params.get("engines"):
            command.extend(["--engines", *[str(item) for item in list(params["engines"])[:20]]])
        if params.get("codes"):
            command.extend(["--codes", *[str(item) for item in list(params["codes"])[:1000]]])
    elif factory == "incubation":
        command.append("--json")
        if params.get("dry_run"):
            command.append("--dry-run")
    else:
        command.append("--once")
        command.append("--allow-network" if params.get("allow_network", False) else "--no-network")
        if params.get("codes"):
            command.extend(["--codes", *[str(item) for item in list(params["codes"])[:1000]]])
        if params.get("lookback_days") is not None:
            command.extend(["--notice-days", str(max(1, min(int(params["lookback_days"]), 365)))])
    return command


def _last_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    best: dict[str, Any] = {}
    best_length = 0
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(stdout[index:])
        except Exception:
            continue
        if isinstance(value, dict) and end > best_length:
            best = value
            best_length = end
    return best


def _outcome(result: dict[str, Any], exit_code: int) -> tuple[str, str | None, list[str], str | None]:
    raw_status = str(result.get("status") or result.get("raw_status") or "").strip().lower() or None
    quality_flags = [str(item) for item in list(result.get("quality_flags") or [])]
    error = str(result.get("error") or "").strip() or None
    if exit_code != 0 or result.get("success") is False:
        return "failed", raw_status, quality_flags, error or f"runner exited with code {exit_code}"
    if raw_status in {"partial", "partial_infra", "partial_llm", "degraded"} or quality_flags:
        return "partial", raw_status, quality_flags, error
    if raw_status in {"blocked", "skipped"}:
        return "blocked", raw_status, quality_flags, error
    return "succeeded", raw_status, quality_flags, error


async def _execute(run_id: str) -> int:
    db = get_db()
    await db.initialize()
    await db.recover_stale_factory_runs(
        stale_after_seconds=max(30, int(os.getenv("AIASK_FACTORY_STALE_AFTER_SECONDS", "900")))
    )
    owner_id = f"factory-worker-{os.getpid()}-{uuid4().hex[:8]}"
    run = await db.claim_factory_run(run_id, owner_id=owner_id, pid=os.getpid())
    if run is None:
        return 3
    try:
        params = dict(run.get("params") or {})
        if os.getenv("AIASK_FACTORY_TEST_MODE") == "1" and isinstance(params.get("_test_result"), dict):
            await asyncio.sleep(float(params.get("_test_delay_seconds") or 0))
            result = dict(params["_test_result"])
            exit_code = int(params.get("_test_exit_code") or 0)
            stderr = ""
        else:
            command = _runner_command(str(run["factory"]), params)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            while True:
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=10)
                    break
                except asyncio.TimeoutError:
                    if not await db.heartbeat_factory_run(run_id, owner_id=owner_id, phase="runner_active"):
                        process.terminate()
                        await process.wait()
                        raise RuntimeError("factory worker lease was lost")
            exit_code = int(process.returncode or 0)
            stdout = stdout_bytes.decode("utf-8", errors="replace")[-65536:]
            stderr = stderr_bytes.decode("utf-8", errors="replace")[-8192:]
            result = _last_json(stdout)
        target, raw_status, quality_flags, error = _outcome(result, exit_code)
        blockers = [str(item) for item in list(result.get("blockers") or [])]
        await db.transition_factory_run(
            run_id,
            target,
            expected_status="running",
            phase="completed",
            exit_code=exit_code,
            summary=result or {"stderr_tail": stderr[-2000:]},
            quality_flags=quality_flags,
            blockers=blockers,
            raw_status=raw_status,
            error=error,
        )
        return 0 if target in {"succeeded", "partial", "blocked"} else 1
    except Exception as exc:
        await db.transition_factory_run(
            run_id,
            "failed",
            expected_status="running",
            phase="worker_failed",
            error=str(exc),
        )
        return 1
    finally:
        await db.release_factory_lock(run_id, owner_id=owner_id)


async def _drain(limit: int) -> int:
    db = get_db()
    await db.initialize()
    await db.recover_stale_factory_runs(
        stale_after_seconds=max(30, int(os.getenv("AIASK_FACTORY_STALE_AFTER_SECONDS", "900")))
    )
    queued = await db.list_factory_runs(status="queued", limit=max(1, min(limit, 200)))
    exit_code = 0
    for run in reversed(queued):
        result = await _execute(str(run["run_id"]))
        if result not in {0, 3}:
            exit_code = result
    return exit_code


async def _main(run_id: str | None, drain_limit: int | None) -> int:
    try:
        if run_id:
            return await _execute(run_id)
        return await _drain(drain_limit or 100)
    finally:
        await close_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one persisted AIASK factory run")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-id")
    mode.add_argument("--drain", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.run_id, args.limit if args.drain else None))


if __name__ == "__main__":
    raise SystemExit(main())
