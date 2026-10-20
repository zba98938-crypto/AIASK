"""Host-neutral ``aiask factory`` control plane.

Until now the authoritative four-factory control surface lived in the Hermes
plugin (``hermes aiask factory ...``), which means the control plane was owned by
one specific host. This module moves it into the AIASK package itself so any
host — Hermes, a stock MCP client, or a bare shell — drives factories the same
way, and the Hermes command becomes a thin shim over this.

Every subcommand emits the governed run envelope (re-exported from
``strategy_factory.api.runtime``) so callers can parse output generically
instead of screen-scraping per-runner text.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from strategy_factory.api.runtime import (
    RunEnvelope,
    envelope_from_runtime_result,
    utc_now_iso,
)
import logging

logger = logging.getLogger(__name__)


#: Canonical four-factory membership. SignalTracker is deliberately absent — it
#: is a sidecar, not a factory, and conflating them has caused topology drift.
FACTORY_RUNNERS: dict[str, str] = {
    "strategy_factory": "run_strategy_factory.py",
    "factor_mining": "run_factor_mining_factory.py",
    "incubation": "run_incubation_factory.py",
    "market_event_ingest": "run_market_event_ingest.py",
}

SIDECAR_RUNNERS: dict[str, str] = {
    "signal_tracker": "run_signal_tracker.py",
}

ALL_RUNNERS: dict[str, str] = {**FACTORY_RUNNERS, **SIDECAR_RUNNERS}

#: Flags each runner needs for a single non-daemon pass.
_ONCE_FLAGS: dict[str, tuple[str, ...]] = {
    "strategy_factory": ("--once", "--json"),
    "factor_mining": ("--once",),
    "incubation": ("--json",),
    "market_event_ingest": ("--once",),
    "signal_tracker": ("--once",),
}

DEFAULT_TIMEOUT_SEC = 7200.0


def project_root() -> Path:
    """AIASK integration root. ``AIASK_PROJECT_ROOT`` wins when set."""
    explicit = str(os.getenv("AIASK_PROJECT_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(__file__).resolve().parents[4]


def runner_path(name: str) -> Path:
    return project_root() / "scripts" / "factories" / ALL_RUNNERS[name]


def resolve_targets(only: list[str] | None, *, include_sidecar: bool) -> list[str]:
    """Resolve requested runtimes, rejecting unknown names loudly."""
    if only:
        unknown = [name for name in only if name not in ALL_RUNNERS]
        if unknown:
            raise SystemExit(
                f"unknown runtime(s): {', '.join(sorted(unknown))}. "
                f"known: {', '.join(sorted(ALL_RUNNERS))}"
            )
        return list(dict.fromkeys(only))
    targets = list(FACTORY_RUNNERS)
    if include_sidecar:
        targets.extend(SIDECAR_RUNNERS)
    return targets


def _child_env() -> dict[str, str]:
    """Child environment with live trading forced off.

    The control plane is a research/paper surface. Never let an inherited env
    quietly enable live trading in a spawned runner.
    """
    env = dict(os.environ)
    env["LIVE_TRADING_ENABLED"] = "0"
    env.setdefault("AIASK_PROJECT_ROOT", str(project_root()))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _extract_json_payload(stdout: str) -> dict[str, Any] | None:
    """Recover a JSON object from runner output that also prints prose.

    Runners interleave human-readable banners with their JSON, so scan from the
    last line backwards for a decodable object rather than assuming clean stdout.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError as _swallowed:
        logger.warning("packages/agent/src/aiask_agent/factory_cli.py:112: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    lines = text.splitlines()
    for start in range(len(lines)):
        candidate = "\n".join(lines[start:]).strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as _swallowed:
            logger.warning("packages/agent/src/aiask_agent/factory_cli.py:121: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _run_runner(
    name: str,
    extra_args: tuple[str, ...] = (),
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    python: str | None = None,
) -> RunEnvelope:
    """Spawn one runner and normalize its result into the governed envelope."""
    started_at = utc_now_iso()
    script = runner_path(name)
    if not script.is_file():
        envelope = RunEnvelope.start(name)
        envelope.started_at = started_at
        envelope.fail(f"runner_missing:{script}")
        return envelope.finish("blocked")

    command = [python or sys.executable, str(script), *_ONCE_FLAGS.get(name, ()), *extra_args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(project_root()),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        envelope = RunEnvelope.start(name)
        envelope.started_at = started_at
        envelope.fail(f"timeout_after_{int(timeout)}s")
        return envelope.finish("failed")
    except OSError as exc:
        envelope = RunEnvelope.start(name)
        envelope.started_at = started_at
        envelope.fail(f"spawn_failed:{exc}")
        return envelope.finish("failed")

    payload = _extract_json_payload(completed.stdout)
    if payload is not None:
        envelope = envelope_from_runtime_result(name, payload, started_at=started_at)
    else:
        # A runner that emits no JSON cannot be graded on its payload, so the
        # exit code is the only signal. Say so rather than inventing counts.
        envelope = RunEnvelope.start(name)
        envelope.started_at = started_at
        envelope.degrade("runner_emitted_no_json")
        envelope.finish("success" if completed.returncode == 0 else "failed")

    if completed.returncode != 0:
        envelope.fail(f"exit_code={completed.returncode}")
        tail = (completed.stderr or "").strip().splitlines()
        if tail:
            envelope.fail(f"stderr:{tail[-1][:300]}")
        envelope.status = "failed"
    envelope.data.setdefault("command", command)
    envelope.data.setdefault("returncode", completed.returncode)
    return envelope


def _aggregate(runtime: str, children: list[RunEnvelope]) -> RunEnvelope:
    """Roll child envelopes into one parent without hiding partial failure."""
    parent = RunEnvelope.start(runtime)
    if children:
        parent.started_at = min(child.started_at for child in children)
    parent.data["steps"] = [child.to_dict() for child in children]

    for child in children:
        parent.add_count(child.runtime, 1 if child.ok else 0)
        for error in child.errors:
            parent.fail(f"{child.runtime}:{error}")
        for reason in child.degraded_reasons:
            parent.degrade(f"{child.runtime}:{reason}")
        for ref in child.evidence_refs:
            parent.add_evidence(ref)

    parent.counts["total"] = len(children)
    parent.counts["succeeded"] = sum(1 for child in children if child.ok)
    parent.counts["failed"] = sum(1 for child in children if child.status in {"failed", "blocked"})

    if not children:
        parent.fail("no_targets_selected")
        return parent.finish("failed")
    if parent.counts["succeeded"] == len(children):
        return parent.finish("success")
    if parent.counts["succeeded"] == 0:
        return parent.finish("failed")
    return parent.finish("partial")


# --- subcommands ---------------------------------------------------------------


def cmd_once(args: argparse.Namespace) -> RunEnvelope:
    targets = resolve_targets(args.only, include_sidecar=args.with_sidecar)
    children = [
        _run_runner(name, timeout=args.timeout, python=args.venv_python or None)
        for name in targets
    ]
    if len(children) == 1:
        return children[0]
    return _aggregate("factory_once", children)


def cmd_cycle(args: argparse.Namespace) -> RunEnvelope:
    """One sequential pass over the selected runtimes.

    Distinct from ``once`` only in intent and labelling; kept as its own verb
    because the existing Hermes surface and docs already name it that way.
    """
    targets = resolve_targets(args.only, include_sidecar=args.with_sidecar)
    children: list[RunEnvelope] = []
    for name in targets:
        child = _run_runner(name, timeout=args.timeout, python=args.venv_python or None)
        children.append(child)
        if child.status in {"failed", "blocked"} and args.stop_on_failure:
            break
    return _aggregate("factory_cycle", children)


def cmd_status(args: argparse.Namespace) -> RunEnvelope:
    """Report which runners exist and are runnable. No cycles are started."""
    envelope = RunEnvelope.start("factory_status")
    runners: dict[str, Any] = {}
    missing = 0
    for name in ALL_RUNNERS:
        script = runner_path(name)
        present = script.is_file()
        if not present:
            missing += 1
            envelope.degrade(f"{name}:runner_missing")
        runners[name] = {
            "path": str(script),
            "present": present,
            "role": "factory" if name in FACTORY_RUNNERS else "sidecar",
        }
    envelope.data["runners"] = runners
    envelope.data["project_root"] = str(project_root())
    envelope.data["four_factories"] = sorted(FACTORY_RUNNERS)
    envelope.data["sidecars"] = sorted(SIDECAR_RUNNERS)
    envelope.add_count("runners", len(ALL_RUNNERS))
    envelope.add_count("missing", missing)
    return envelope.finish()


def cmd_evidence(args: argparse.Namespace) -> RunEnvelope:
    """Summarize evidence rows from the local sqlite ledger, read-only."""
    import sqlite3

    def _resolve_default_sqlite_path(explicit: str) -> tuple[Path, str]:
        if getattr(args, "legacy_db", False) and not explicit:
            # Explicit opt-in to the historical legacy shell location. It holds
            # no data; the authoritative runtime DB comes from runtime_paths.
            return Path.home() / ".aiask" / "akshare_mcp.sqlite3", "legacy_db_flag"
        if explicit:
            return Path(explicit).expanduser(), "cli_arg"
        aiask_env = os.getenv("AIASK_SQLITE_PATH")
        if aiask_env:
            return Path(aiask_env).expanduser(), "AIASK_SQLITE_PATH"
        akshare_env = os.getenv("AKSHARE_MCP_SQLITE_PATH")
        if akshare_env:
            return Path(akshare_env).expanduser(), "AKSHARE_MCP_SQLITE_PATH"
        # Authoritative runtime database: ~/.aiask/data/db/akshare_mcp.sqlite3
        # (via AIASK_RUNTIME_HOME). NOT the legacy 0-row shell at
        # ~/.aiask/akshare_mcp.sqlite3 — use --legacy-db only if you must.
        try:
            from aiask_quant_core.runtime_paths import resolve_sqlite_path_with_source
            path, source = resolve_sqlite_path_with_source("akshare_mcp.sqlite3")
            return path, f"runtime_paths({source})"
        except Exception:
            # Fallback: the same canonical directory via the agent's own resolver
            from aiask_agent.adapters._runtime_paths import configured_sqlite_path
            path, _ = configured_sqlite_path("akshare_mcp.sqlite3")
            return path, "agent_runtime_paths_fallback"

    envelope = RunEnvelope.start("factory_evidence")
    path, path_source = _resolve_default_sqlite_path(args.sqlite_path)
    envelope.data["sqlite_path_source"] = path_source
    envelope.data["sqlite_path"] = str(path)

    if not path.is_file():
        envelope.fail(f"sqlite_missing:{path}")
        return envelope.finish("blocked")

    tables = (
        "strategy_factory_runs",
        "strategy_factory_task_evidence",
        "strategy_factory_run_artifacts",
        "factory_run_ledger",
    )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        envelope.fail(f"sqlite_open_failed:{exc}")
        return envelope.finish("failed")
    try:
        for table in tables:
            try:
                cursor = connection.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                envelope.add_count(table, cursor.fetchone()[0])
                envelope.add_evidence(f"sqlite://{path}#{table}")
            except sqlite3.Error:
                envelope.degrade(f"{table}:unreadable")
    finally:
        connection.close()
    return envelope.finish()


def cmd_ops_status(args: argparse.Namespace) -> RunEnvelope:
    """Report supervisor process state. Read-only — starts and stops nothing.

    Reads pid files under the runtime home. A pid file whose process is gone is
    reported as ``stale`` rather than ``running``, so a crashed supervisor cannot
    look healthy.
    """
    envelope = RunEnvelope.start("factory_ops_status")

    explicit = getattr(args, "runtime_home", "") or os.getenv("AIASK_RUNTIME_HOME") or ""
    runtime_home = (
        Path(explicit).expanduser() if explicit else Path.home() / ".aiask"
    )
    envelope.data["runtime_home"] = str(runtime_home)

    pid_dir = runtime_home / "pids"
    if not pid_dir.is_dir():
        envelope.data["processes"] = {}
        envelope.degrade("no_pid_directory")
        return envelope.finish()

    processes: dict[str, Any] = {}
    running = 0
    stale = 0
    for pid_file in sorted(pid_dir.glob("*.pid")):
        name = pid_file.stem
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            processes[name] = {"state": "unreadable", "pid": None}
            envelope.degrade(f"{name}:unreadable_pid_file")
            continue
        alive = _pid_alive(pid)
        processes[name] = {"state": "running" if alive else "stale", "pid": pid}
        if alive:
            running += 1
        else:
            stale += 1
            envelope.degrade(f"{name}:stale_pid")

    envelope.data["processes"] = processes
    envelope.add_count("running", running)
    envelope.add_count("stale", stale)
    envelope.add_count("total", len(processes))
    return envelope.finish()


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process, without signalling it."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError as _swallowed:
        logger.warning("packages/agent/src/aiask_agent/factory_cli.py:365: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return False
    except PermissionError:
        # Exists but owned by another user.
        return True
    except OSError as _swallowed:
        logger.warning("packages/agent/src/aiask_agent/factory_cli.py:370: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return False
    return True


def cmd_readiness(args: argparse.Namespace) -> RunEnvelope:
    """Aggregate readiness: runners, providers, storage and evidence ledger.

    A single command a host can call to decide whether anything is worth running,
    rather than composing four separate calls itself.
    """
    envelope = RunEnvelope.start("readiness")

    status = cmd_status(_namespace_like(args))
    preflight = cmd_preflight(_namespace_like(args))

    envelope.data["runners"] = status.data.get("runners", {})
    envelope.data["providers"] = {
        "missing": preflight.data.get("missing_providers", []),
        "runtimes": preflight.data.get("runtimes", {}),
    }
    envelope.data["storage"] = preflight.data.get("storage", {})
    envelope.data["four_factories"] = status.data.get("four_factories", [])
    envelope.data["sidecars"] = status.data.get("sidecars", [])

    for child in (status, preflight):
        for reason in child.degraded_reasons:
            envelope.degrade(f"{child.runtime}:{reason}")
        for error in child.errors:
            envelope.fail(f"{child.runtime}:{error}")

    envelope.add_count("runners", int(status.counts.get("runners") or 0))
    envelope.add_count("missing_runners", int(status.counts.get("missing") or 0))
    envelope.add_count(
        "missing_providers", int(preflight.counts.get("missing_providers") or 0)
    )
    return envelope.finish()


def _namespace_like(args: argparse.Namespace) -> argparse.Namespace:
    """A namespace carrying the fields the delegated commands read."""
    return argparse.Namespace(
        only=None,
        with_sidecar=False,
        timeout=getattr(args, "timeout", DEFAULT_TIMEOUT_SEC),
        venv_python=getattr(args, "venv_python", ""),
        stop_on_failure=False,
        sqlite_path=getattr(args, "sqlite_path", ""),
        runtime_home=getattr(args, "runtime_home", ""),
    )


def cmd_schedule_template(args: argparse.Namespace) -> RunEnvelope:
    """Emit disabled cron samples. Never installs anything."""
    envelope = RunEnvelope.start("factory_schedule_template")
    root = project_root()
    samples = {
        "strategy_factory": "17 8 * * 1-5",
        "factor_mining": "23 7 * * 1-5",
        "incubation": "31 18 * * 1-5",
        "market_event_ingest": "11 * * * *",
        "signal_tracker": "3 18 * * 1-5",
    }
    lines = ["# AIASK factory schedule template (all entries disabled)."]
    for name, cron in samples.items():
        script = root / "scripts" / "factories" / ALL_RUNNERS[name]
        flags = " ".join(_ONCE_FLAGS.get(name, ()))
        lines.append(f"# {cron} {sys.executable} {script} {flags}".rstrip())
    envelope.data["crontab"] = "\n".join(lines)
    envelope.data["installed"] = False
    envelope.degrade("template_only_nothing_installed")
    return envelope.finish()


def _storage_capability_report(missing_providers: list[str]) -> dict[str, Any]:
    """Report whether the injected connection covers the factory-domain surface.

    Uses Strategy Factory's repository facade so the check is driven by the
    declared contract rather than a hand-kept list. Returns a ``skipped`` payload
    when the connection cannot be resolved — that is a provider problem already
    reported separately, not a storage verdict.
    """
    if "db_provider" in missing_providers:
        return {"status": "skipped", "reason": "db_provider_missing"}
    try:
        # Public surface only; internal subpackages are package-private.
        from strategy_factory.api.runtime import FactoryRepository, get_db_provider
    except ImportError as exc:
        return {"status": "skipped", "reason": f"import_failed:{exc}"}

    try:
        provider = get_db_provider()
        connection = provider() if callable(provider) else provider
        if connection is None:
            return {"status": "skipped", "reason": "db_provider_returned_none"}
        report = FactoryRepository(connection).capability_report()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

    report["status"] = "ready" if report.get("ready") else "incomplete"
    return report


def cmd_preflight(args: argparse.Namespace) -> RunEnvelope:
    """Check runtime provider readiness before spawning anything.

    ``preflight()`` exists on all four runtimes but had zero callers, so a
    missing provider only surfaced as a child crash. This exposes it.
    """
    envelope = RunEnvelope.start("factory_preflight")
    try:
        from strategy_factory.runtime.default_bootstrap import (
            ensure_default_runtime_services,
            get_missing_required_runtime_providers,
        )
    except ImportError as exc:
        envelope.fail(f"strategy_factory_unavailable:{exc}")
        return envelope.finish("blocked")

    try:
        ensure_default_runtime_services()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        envelope.fail(f"bootstrap_failed:{type(exc).__name__}:{exc}")

    missing = sorted(get_missing_required_runtime_providers())
    envelope.data["missing_providers"] = missing
    envelope.add_count("missing_providers", len(missing))
    for name in missing:
        envelope.fail(f"missing_provider:{name}")

    # Factory-domain persistence readiness. Checked before any runtime so a
    # missing storage capability is reported here rather than surfacing midway
    # through a cycle as an AttributeError.
    envelope.data["storage"] = _storage_capability_report(missing)
    for gap in envelope.data["storage"].get("missing", []):
        envelope.degrade(f"storage_capability_missing:{gap}")

    runtimes: dict[str, Any] = {}
    if not missing:
        from strategy_factory.runtime import (
            get_factor_mining_runtime,
            get_incubation_runtime,
            get_market_event_ingest_runtime,
            get_signal_tracker_runtime,
        )

        factories = {
            "factor_mining": get_factor_mining_runtime,
            "incubation": get_incubation_runtime,
            "market_event_ingest": get_market_event_ingest_runtime,
            "signal_tracker": get_signal_tracker_runtime,
        }
        for name, factory in factories.items():
            try:
                runtimes[name] = factory().preflight()
            except Exception as exc:  # noqa: BLE001
                runtimes[name] = {"error": f"{type(exc).__name__}: {exc}"}
                envelope.fail(f"{name}:preflight_failed")
    envelope.data["runtimes"] = runtimes
    return envelope.finish()


def build_readiness_parser(sub: Any) -> None:
    """Attach the top-level ``readiness`` command.

    Top level rather than under ``factory`` because it reports on the whole
    installation — runners, providers and storage — not on a factory run.
    """
    readiness = sub.add_parser(
        "readiness",
        help="Aggregate readiness of runners, providers, storage and evidence.",
    )
    readiness.add_argument(
        "--json",
        action="store_true",
        help="emit the governed JSON envelope (always on; accepted for compatibility)",
    )
    readiness.add_argument("--sqlite-path", default="")
    readiness.add_argument("--venv-python", default="")
    readiness.set_defaults(func=cmd_readiness)


def build_factory_parser(sub: Any) -> None:
    """Attach the ``factory`` subcommand tree to an existing subparsers object."""
    factory = sub.add_parser("factory", help="Four-factory control plane (research/paper only).")
    actions = factory.add_subparsers(dest="factory_action", required=True)

    def _json_flag(parser: argparse.ArgumentParser) -> None:
        """Accept ``--json`` on every subcommand.

        Output is always the governed envelope, so the flag is a no-op — but the
        contract promises it, and a caller scripting against that promise must not
        get ``exit 2`` for passing it.
        """
        parser.add_argument(
            "--json",
            action="store_true",
            help="emit the governed JSON envelope (always on; accepted for compatibility)",
        )

    def _shared(parser: argparse.ArgumentParser) -> None:
        _json_flag(parser)
        parser.add_argument(
            "--only",
            action="append",
            metavar="RUNTIME",
            help="limit to one runtime; repeatable. Default: the four factories.",
        )
        parser.add_argument(
            "--with-sidecar",
            action="store_true",
            help="also run signal_tracker (a sidecar, not one of the four factories)",
        )
        parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
        parser.add_argument("--venv-python", default="")

    once = actions.add_parser("once", help="Run one pass per selected runtime.")
    _shared(once)
    once.set_defaults(func=cmd_once)

    cycle = actions.add_parser("cycle", help="Sequential pass over selected runtimes.")
    _shared(cycle)
    cycle.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="abort the remaining runtimes after the first failure",
    )
    cycle.set_defaults(func=cmd_cycle)

    status = actions.add_parser("status", help="Report runner presence and topology.")
    _json_flag(status)
    status.set_defaults(func=cmd_status)

    preflight = actions.add_parser("preflight", help="Check runtime provider readiness.")
    _json_flag(preflight)
    preflight.set_defaults(func=cmd_preflight)

    evidence = actions.add_parser("evidence", help="Summarize the evidence ledger (read-only).")
    _json_flag(evidence)
    evidence.add_argument("--sqlite-path", default="")
    evidence.add_argument(
        "--legacy-db",
        action="store_true",
        help="read the legacy ~/.aiask/akshare_mcp.sqlite3 shell instead of the runtime DB",
    )
    evidence.set_defaults(func=cmd_evidence)

    template = actions.add_parser(
        "schedule-template",
        help="Print disabled cron samples; installs nothing.",
    )
    _json_flag(template)
    template.set_defaults(func=cmd_schedule_template)

    ops_status = actions.add_parser(
        "ops-status",
        help="Report supervisor process state (read-only; starts nothing).",
    )
    _json_flag(ops_status)
    ops_status.add_argument("--runtime-home", default="")
    ops_status.set_defaults(func=cmd_ops_status)


__all__ = [
    "ALL_RUNNERS",
    "FACTORY_RUNNERS",
    "SIDECAR_RUNNERS",
    "build_factory_parser",
    "build_readiness_parser",
    "cmd_ops_status",
    "cmd_readiness",
    "cmd_cycle",
    "cmd_evidence",
    "cmd_once",
    "cmd_preflight",
    "cmd_schedule_template",
    "cmd_status",
    "project_root",
    "resolve_targets",
    "runner_path",
]
