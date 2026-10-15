#!/usr/bin/env python3
"""Run Strategy Factory repeatedly and record each completed run to Markdown.

This session runner is purpose-built for real runtime observation:
1. execute one real Strategy Factory run per round
2. collect run-quality snapshots through the same manager/service handlers that
   power the MCP strategy-manager surface
3. optionally execute one incubation-factory round after each strategy round
4. rewrite a root-level Markdown report after every round
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
for package_src in (
    ROOT / "packages" / "aiask-quant-core" / "src",
    ROOT / "packages" / "strategy-factory" / "src",
    ROOT / "packages" / "akshare-mcp" / "src",
):
    path = str(package_src)
    if package_src.exists() and path not in sys.path:
        sys.path.insert(0, path)

from _integration_paths import default_sqlite_path, ensure_runtime_dirs, runtime_home

from strategy_factory.runtime_bootstrap import ensure_factory_runtime

ensure_factory_runtime(
    project_root=ROOT,
    script_path=Path(__file__).resolve(),
    argv=sys.argv[1:],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
)

from akshare_mcp.env_loader import load_mcp_env
from akshare_mcp.storage.sqlite import close_db, get_db
from akshare_mcp.tools.managers.strategy_mgr_crud import handle_review_report
from akshare_mcp.tools.managers.strategy_mgr_lifecycle import (
    handle_execution_audit_verification,
    handle_factory_run_detail,
    handle_factory_runs,
    handle_incubation_overview,
)
from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services

from _quality_session_common import (
    _LEGACY_BUDGET_MISMATCH_FLAGS,
    _LEGACY_BUDGET_MISMATCH_NOTE_FRAGMENTS,
    _RECOMPUTED_AUDIT_FLAGS,
    _RECOMPUTED_AUDIT_NOTE_FRAGMENTS,
    DEFAULT_EXECUTION_MODE,
    LOGGER,
    MARKET_TZ,
    _format_dt,
    _iso_now,
    _json_dump,
    _now,
    _pct,
    _process_alive,
    _safe_float,
    _safe_int,
    _write_json,
)
from _quality_session_render import (
    _bool_text,
    _build_aggregate_summary,
    _build_blocker_summary,
    _build_priority_findings,
    _compact_run_detail,
    _extract_issue_flags,
    _format_strict_ready_example_evidence,
    _format_top_blockers,
    _merge_strategy_samples,
    _normalize_blocker_reason,
    _quality_strategy_pool,
    _render_entry,
    _render_report,
    _render_representative_samples,
    _render_strict_ready_example,
    _resolve_candidate_artifact,
    _sample_strategy_table,
    _select_representative_samples,
    _sort_strategy_samples,
    _strict_ready_example_payload,
)
from _quality_session_modes import (
    QualitySessionModeConfig,
    apply_quality_mode_env,
    mode_config_from_state,
    resolve_quality_session_modes,
    resolve_session_runtime_controls,
)


class SessionRunLock:
    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._held = False

    def acquire(self, *, session_id: str) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "pid": os.getpid(),
            "acquired_at": _iso_now(),
            "script": str(Path(__file__).resolve()),
        }

        if self._lock_path.exists():
            try:
                existing = json.loads(self._lock_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            existing_pid = _safe_int(existing.get("pid"), default=-1)
            if _process_alive(existing_pid):
                raise RuntimeError(
                    f"Session {session_id} is already running under pid={existing_pid}; "
                    "use --render-only to refresh markdown without starting a new run."
                )
            LOGGER.warning(
                "Removing stale session lock for session_id=%s from pid=%s at %s",
                session_id,
                existing.get("pid"),
                self._lock_path,
            )
            try:
                self._lock_path.unlink()
            except FileNotFoundError as _swallowed:
                logger.warning("scripts/factories/run_strategy_factory_quality_session.py:153: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass

        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        self._held = True
        atexit.register(self.release)

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self._lock_path.exists():
                existing = {}
                try:
                    existing = json.loads(self._lock_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
                if _safe_int(existing.get("pid"), default=os.getpid()) == os.getpid():
                    self._lock_path.unlink()
        except FileNotFoundError as _swallowed:
            logger.warning("scripts/factories/run_strategy_factory_quality_session.py:174: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
        finally:
            self._held = False


def _load_script_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_report_path(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return (runtime_home() / "reports" / f"strategy_factory_quality_{_now().strftime('%Y-%m-%d')}.md").resolve()


def _resolve_session_id(raw: str | None) -> str:
    if raw:
        return str(raw).strip()
    return f"strategy_factory_quality_{_now().strftime('%Y%m%d_%H%M%S')}"


def _resolve_logs_dir(session_id: str) -> Path:
    return (runtime_home() / "logs" / "strategy_factory_quality_sessions" / session_id).resolve()


def _configure_logging(logs_dir: Path, verbose: bool) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "session.log"
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _apply_base_session_env(sqlite_path: Path) -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ.setdefault("AKSHARE_MCP_SQLITE_PATH", str(sqlite_path))
    os.environ.setdefault("AIASK_SQLITE_PATH", str(sqlite_path))

    os.environ.setdefault("FACTOR_MINING_FACTORY_ENABLED", "1")
    os.environ.setdefault("STRATEGY_FACTORY_FACTOR_CATALOG_ENABLED", "1")
    os.environ.setdefault("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    # 1500/轮:observe(stage=paper)积压上万时加速收敛(实测每策略孵化 ~0.05s,
    # incubating200+paper1500 串行约 85s,远低于 BATCH_TIMEOUT_SEC=600)。
    os.environ["INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT"] = "1500"
    os.environ["INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED"] = "0"
    os.environ.setdefault("INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT", "300")
    os.environ.setdefault("INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE", "C")
    os.environ.setdefault("STRATEGY_FACTORY_FACTOR_IC_GENERIC_INTAKE_ENABLED", "1")
    os.environ.setdefault("STRATEGY_FACTORY_EVENT_RUNTIME_MODE", "refresh")

    os.environ.setdefault("STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED", "0")
    os.environ.setdefault("STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED", "1")
    os.environ.setdefault("STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED", "1")

    os.environ["LIVE_TRADING_ENABLED"] = "0"
    os.environ["LIVE_TRADING_ALLOW_WRITE"] = "0"
    os.environ["BROKER_ALLOW_WRITE"] = "0"
    os.environ["LIVE_TRADING_READ_ONLY"] = "1"
    os.environ["BROKER_READ_ONLY"] = "1"
    os.environ["INCUBATION_FACTORY_OWNS_PAPER_TRADING"] = "0"


def _apply_quality_session_runtime_defaults() -> None:
    """Keep quality-session verification bounded without changing app defaults."""
    os.environ.setdefault("STRATEGY_FACTORY_RUN_ONCE_TIMEOUT_SEC", "420")
    os.environ.setdefault("STRATEGY_FACTORY_RESEARCH_TASK_TIMEOUT_SEC", "120")
    os.environ.setdefault("STRATEGY_FACTORY_BULK_RESEARCH_TASK_TIMEOUT_SEC", "180")
    os.environ.setdefault("STRATEGY_FACTORY_EXTERNAL_RESEARCH_TASK_TIMEOUT_SEC", "90")
    os.environ.setdefault("STRATEGY_FACTORY_PAPER_TRADING_CYCLE_TIMEOUT_SEC", "120")
    os.environ["STRATEGY_PIPELINE_STAGE_TIMEOUT_SEC"] = "25"
    os.environ["STRATEGY_LLM_TIMEOUT_SEC"] = "25"
    os.environ["STRATEGY_LLM_STAGE_RETRY_COUNT"] = "0"
    os.environ["STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK"] = "1"
    os.environ["STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC"] = "180"
    os.environ["STRATEGY_LLM_RECENT_CONNECTIVITY_MINIMAL_STREAK"] = "1"
    os.environ["STRATEGY_LLM_RECENT_CONNECTIVITY_COOLDOWN_SEC"] = "180"
    os.environ["STRATEGY_LLM_RECENT_OVERLOAD_MINIMAL_STREAK"] = "1"
    os.environ["STRATEGY_LLM_RECENT_OVERLOAD_COOLDOWN_SEC"] = "180"
    os.environ["FACTOR_LLM_TIMEOUT_SEC"] = "25"
    os.environ["FACTOR_LLM_CONNECT_TIMEOUT_SEC"] = "8"
    os.environ["FACTOR_LLM_WRITE_TIMEOUT_SEC"] = "10"
    os.environ["FACTOR_LLM_POOL_TIMEOUT_SEC"] = "5"
    os.environ["STRATEGY_FACTORY_FACTOR_AUTO_REFRESH"] = "1"
    os.environ.setdefault("STRATEGY_FACTORY_FACTOR_REFRESH_TIMEOUT_SEC", "60")
    os.environ["STRATEGY_FACTORY_FACTOR_REFRESH_SELF_HEAL"] = "1"
    os.environ["STRATEGY_QUALITY_FACTOR_MINING_ENGINES"] = "rule_seed"
    os.environ["STRATEGY_QUALITY_FACTOR_MINING_CANDIDATE_COUNT"] = "4"
    os.environ["STRATEGY_QUALITY_FACTOR_MINING_EVOLUTION_GENERATIONS"] = "1"
    os.environ["FACTOR_MINING_STRICT_VALIDATION_CANDIDATE_LIMIT"] = "1"
    os.environ["STRATEGY_QUALITY_FACTOR_MAINTENANCE_AFTER_MINING"] = "1"
    os.environ.setdefault("STRATEGY_QUALITY_FACTOR_MAINTENANCE_TIMEOUT_SEC", "60")
    os.environ.setdefault("STRATEGY_QUALITY_SIGNAL_TRACKER_TIMEOUT_SEC", "90")
    # P0 FIX: 补偿逻辑默认禁用 - 只有通过 --enable-compensation 显式启用
    # os.environ.setdefault("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED", "1")
    # os.environ.setdefault("INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_BATCH_LIMIT", "200")
    # os.environ.setdefault("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED", "1")
    # os.environ.setdefault("INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_BATCH_LIMIT", "200")
    # os.environ.setdefault("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED", "1")
    # os.environ.setdefault("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT", "200")


def _apply_compensation_logic_if_enabled(args: argparse.Namespace) -> None:
    """Apply compensation logic ONLY if explicitly requested via --enable-compensation.

    P0 FIX: 补偿逻辑默认禁用,只能通过显式参数启用。
    这不是生产观察模式,仅用于补证据验证场景。
    """
    if not getattr(args, "enable_compensation", False):
        return

    # 打印警告
    LOGGER.warning("=" * 80)
    LOGGER.warning("⚠️  补偿逻辑已启用 - 这不是生产观察模式")
    LOGGER.warning("⚠️  Compensation logic enabled - NOT production observation mode")
    LOGGER.warning("=" * 80)

    # 启用补偿逻辑
    os.environ["INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_ENABLED"] = "1"
    os.environ["INCUBATION_FACTORY_PAPER_EXECUTION_BACKLOG_BATCH_LIMIT"] = "200"
    os.environ["INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_ENABLED"] = "1"
    os.environ["INCUBATION_FACTORY_EXECUTION_AUDIT_NATIVE_EVIDENCE_BACKFILL_BATCH_LIMIT"] = "200"
    os.environ["INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED"] = "1"
    os.environ["INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT"] = "200"

    LOGGER.info("Compensation settings applied:")
    LOGGER.info("  - Paper Execution Backlog: ENABLED")
    LOGGER.info("  - Native Evidence Backfill: ENABLED")
    LOGGER.info("  - Stale Position Closure: ENABLED")
    LOGGER.warning("=" * 80)


def _factory_module():
    return _load_script_module(
        "_strategy_factory_quality_run_strategy_factory",
        ROOT / "scripts" / "factories" / "run_strategy_factory.py",
    )


from _quality_session_collectors import (
    _resolve_latest_run_id_since,
    _call_optional_db_method,
    _collect_factor_shelf_status,
    _collect_paper_observation_backlog,
    _collect_run_snapshot,
    _collect_strategy_snapshot,
    _enrich_strategy_snapshot_with_persistence,
    _refresh_state_strategy_persistence_metadata,
    _run_factor_mining_once,
    _run_incubation_once,
    _run_signal_tracker_once,
    _run_strategy_factory_once,
)

logger = logging.getLogger(__name__)


@dataclass
class SessionPaths:
    logs_dir: Path
    state_json: Path
    report_md: Path
    rounds_dir: Path
    lock_json: Path


def _load_existing_state(paths: SessionPaths) -> dict[str, Any] | None:
    if not paths.state_json.exists():
        return None
    try:
        state = json.loads(paths.state_json.read_text(encoding="utf-8"))
        return _normalize_loaded_state(state, paths)
    except Exception as exc:
        LOGGER.warning("Failed to load existing state from %s: %s", paths.state_json, exc)
        return None


def _build_session_state(args: argparse.Namespace, paths: SessionPaths) -> dict[str, Any]:
    mode_configs = list(getattr(args, "quality_mode_configs", []) or [])
    runtime_controls = dict(getattr(args, "runtime_controls", {}) or {})
    mode_state = [mode.as_state_dict() for mode in mode_configs]
    return {
        "session": {
            "session_id": args.session_id,
            "started_at": _iso_now(),
            "hours": args.hours,
            "pause_sec": args.pause_sec,
            "codes": list(args.codes or []),
            "quality_session_mode": "compare" if len(mode_configs) > 1 else (mode_configs[0].mode_id if mode_configs else "observe_first"),
            "quality_modes": mode_state,
            "execution_mode": (
                mode_configs[0].execution_mode
                if len(mode_configs) == 1
                else ",".join(mode.execution_mode for mode in mode_configs)
            ),
            "runtime_controls": runtime_controls,
            "python_executable": sys.executable,
            "sqlite_path": str(default_sqlite_path()),
            "report_path": str(paths.report_md),
            "logs_dir": str(paths.logs_dir),
            "rounds_dir": str(paths.rounds_dir),
            "with_incubation": bool(args.with_incubation),
            "run_factor_mining": bool(args.with_incubation and not args.skip_factor_mining),
            "strategy_sample_limit": args.strategy_sample_limit,
            "compensation_enabled": bool(getattr(args, "enable_compensation", False)),  # P0 FIX: 记录补偿模式
        },
        "updated_at": _iso_now(),
        "entries": [],
    }


def _persist_state(state: dict[str, Any], paths: SessionPaths) -> None:
    state["updated_at"] = _iso_now()
    _write_json(paths.state_json, state)
    paths.report_md.write_text(_render_report(state), encoding="utf-8")


def _mark_session_active(state: dict[str, Any]) -> None:
    session = dict(state.get("session") or {})
    session.pop("completed_at", None)
    state["session"] = session


def _session_lock_is_active(paths: SessionPaths, session_id: str | None = None) -> bool:
    if not paths.lock_json.exists():
        return False
    try:
        payload = json.loads(paths.lock_json.read_text(encoding="utf-8"))
    except Exception as _swallowed:
        logger.warning("scripts/factories/run_strategy_factory_quality_session.py:415: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return False
    lock_session_id = str(payload.get("session_id") or "").strip()
    if session_id and lock_session_id and lock_session_id != session_id:
        return False
    return _process_alive(_safe_int(payload.get("pid"), default=-1))


def _normalize_loaded_state(state: dict[str, Any], paths: SessionPaths) -> dict[str, Any]:
    session = dict(state.get("session") or {})
    state["session"] = session
    if session.get("completed_at") and _session_lock_is_active(paths, str(session.get("session_id") or "")):
        session.pop("completed_at", None)

    for entry in list(state.get("entries") or []):
        quality = dict(entry.get("quality_snapshot") or {})
        detail = dict(quality.get("detail") or {})
        sampled_strategies = list(quality.get("sampled_strategies") or [])
        representative_samples = list(quality.get("representative_samples") or [])
        derived_flags, derived_notes = _extract_issue_flags(
            detail,
            _merge_strategy_samples([*representative_samples, *sampled_strategies]),
        )
        existing_flags = [
            item
            for item in list(quality.get("issue_flags") or [])
            if str(item or "").strip() not in _LEGACY_BUDGET_MISMATCH_FLAGS
            and str(item or "").strip() not in _RECOMPUTED_AUDIT_FLAGS
        ]
        existing_notes = [
            item
            for item in list(quality.get("issue_notes") or [])
            if not any(fragment in str(item or "") for fragment in _LEGACY_BUDGET_MISMATCH_NOTE_FRAGMENTS)
            and not any(fragment in str(item or "") for fragment in _RECOMPUTED_AUDIT_NOTE_FRAGMENTS)
        ]
        merged_flags = list(dict.fromkeys([*existing_flags, *derived_flags]))
        merged_notes = list(dict.fromkeys([*existing_notes, *derived_notes]))
        if merged_flags != existing_flags or merged_notes != existing_notes:
            quality["issue_flags"] = merged_flags
            quality["issue_notes"] = merged_notes
            entry["quality_snapshot"] = quality

    return state


def _persist_round(entry: dict[str, Any], paths: SessionPaths) -> None:
    paths.rounds_dir.mkdir(parents=True, exist_ok=True)
    round_no = _safe_int(entry.get("round"))
    mode_id = str(entry.get("quality_mode") or entry.get("mode") or "").strip()
    suffix = f"_{mode_id}" if mode_id else ""
    _write_json(paths.rounds_dir / f"round_{round_no:03d}{suffix}.json", entry)


def _session_mode_configs(args: argparse.Namespace, state: dict[str, Any] | None = None) -> list[QualitySessionModeConfig]:
    if state is not None and not bool(getattr(args, "quality_modes_explicit", False)):
        saved_modes = list(dict(state.get("session") or {}).get("quality_modes") or [])
        if saved_modes:
            return [mode_config_from_state(dict(item or {})) for item in saved_modes]
    return list(getattr(args, "quality_mode_configs", []) or [])


def _session_runtime_controls(args: argparse.Namespace, state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is not None and not bool(getattr(args, "runtime_controls_explicit", False)):
        saved_controls = dict(dict(state.get("session") or {}).get("runtime_controls") or {})
        if saved_controls:
            return saved_controls
    return dict(getattr(args, "runtime_controls", {}) or {})


def _append_quality_issue(
    quality_snapshot: dict[str, Any],
    flag: str,
    note: str,
) -> None:
    flags = list(quality_snapshot.get("issue_flags") or [])
    if flag not in flags:
        flags.append(flag)
    notes = list(quality_snapshot.get("issue_notes") or [])
    if note and note not in notes:
        notes.append(note)
    quality_snapshot["issue_flags"] = flags
    quality_snapshot["issue_notes"] = notes


def _augment_quality_snapshot_with_factor_evidence(
    quality_snapshot: dict[str, Any],
    factor_mining_run: dict[str, Any] | None,
    *,
    run_factor_mining: bool,
) -> None:
    if not run_factor_mining:
        _append_quality_issue(
            quality_snapshot,
            "factor_mining_not_run",
            "factor mining was disabled for this quality round, so fresh factor generation/consumption is unproven",
        )
        return
    if not factor_mining_run:
        _append_quality_issue(
            quality_snapshot,
            "factor_mining_missing_result",
            "factor mining was requested but did not return a result envelope",
        )
        return
    if bool(factor_mining_run.get("skipped")):
        _append_quality_issue(
            quality_snapshot,
            "factor_mining_skipped",
            f"factor mining was skipped by schedule ({factor_mining_run.get('reason') or 'unknown'})",
        )
        return

    result = dict(factor_mining_run.get("result") or {})
    if result and not bool(result.get("success")):
        _append_quality_issue(
            quality_snapshot,
            "factor_mining_failed",
            f"factor mining failed during the quality round ({result.get('error') or 'unknown error'})",
        )
    raw_count = _safe_int(result.get("raw_candidate_count"))
    evolved_count = _safe_int(result.get("evolved_count"))
    admitted_count = _safe_int(result.get("admitted_count"))
    active_promoted_count = _safe_int(result.get("active_promoted_count"))
    maintenance = dict(result.get("maintenance") or {})
    maintenance_promoted_count = _safe_int(maintenance.get("promoted_count"))
    if (raw_count > 0 or evolved_count > 0) and admitted_count <= 0:
        _append_quality_issue(
            quality_snapshot,
            "factor_mining_no_admissions",
            (
                "factor mining produced candidates but admitted none into the active pool "
                f"(raw={raw_count}, evolved={evolved_count}, admitted={admitted_count})"
            ),
        )
    if active_promoted_count > 0 or maintenance_promoted_count > 0:
        _append_quality_issue(
            quality_snapshot,
            "factor_pool_promoted_for_research_consumption",
            (
                "factor pool has promoted factors after quality-session mining/maintenance "
                f"(cycle_promoted={active_promoted_count}, maintenance_promoted={maintenance_promoted_count})"
            ),
        )


def _augment_quality_snapshot_with_runtime_health(
    quality_snapshot: dict[str, Any],
    *,
    factory_result: dict[str, Any],
    signal_tracker_run: dict[str, Any] | None,
    incubation_result: dict[str, Any],
) -> None:
    factory_data = dict((factory_result or {}).get("data") or {})
    factory_status = str(factory_data.get("status") or "").strip().lower()
    signal_payload = dict((signal_tracker_run or {}).get("result") or {})
    acceptance = dict(incubation_result.get("execution_audit_acceptance") or {})
    paper_execution_backlog = dict(incubation_result.get("paper_execution_backlog") or {})
    native_evidence_backfill = dict(
        incubation_result.get("native_execution_evidence_backfill") or {}
    )
    verification = dict(incubation_result.get("verification") or {})
    settlement = dict(incubation_result.get("settlement") or {})
    pipeline = dict(incubation_result.get("pipeline") or {})

    health_failures: list[str] = []

    def _fail(flag: str, note: str) -> None:
        health_failures.append(flag)
        _append_quality_issue(quality_snapshot, flag, note)

    if factory_status and factory_status != "success":
        _fail(
            "factory_result_not_healthy",
            f"factory result status is `{factory_status}`, so top-level success must be treated as degraded",
        )
    if bool(signal_payload.get("timeout")) or _safe_int(signal_payload.get("phase_timeout_count")) > 0:
        _fail(
            "signal_tracker_phase_timeout",
            "SignalTracker reported phase-level timeout(s): "
            f"{list(signal_payload.get('phase_timeouts') or [])}",
        )
    if _safe_int(signal_payload.get("phase_error_count")) > 0:
        _fail(
            "signal_tracker_phase_error",
            f"SignalTracker reported phase errors ({_safe_int(signal_payload.get('phase_error_count'))})",
        )
    if (
        _safe_int(signal_payload.get("signals_generated")) > 0
        and _safe_int(signal_payload.get("incubation_orders")) <= 0
        and _safe_int(settlement.get("filled")) <= 0
    ):
        _fail(
            "paper_signal_not_converted_to_orders",
            "SignalTracker generated non-zero signals but created no incubation/paper orders",
        )
    available_signal_evidence = max(
        _safe_int(acceptance.get("available_signal_evidence_count")),
        _safe_int(acceptance.get("saved_signal_evidence_count")),
    )
    acceptance_status = str(acceptance.get("status") or "").strip()
    acceptance_gate_status_counts = dict(acceptance.get("gate_status_counts") or {})
    acceptance_sample_blockers = {
        str(item or "").strip()
        for item in list(acceptance.get("sample_blockers") or [])
        if str(item or "").strip()
    }
    hard_gate_pending_is_sample_debt = bool(
        acceptance_status in {"ok", "pending_evidence"}
        and available_signal_evidence > 0
        and _safe_int(acceptance_gate_status_counts.get("missing")) <= 0
        and acceptance_sample_blockers.issubset(
            {"execution_hard_gate_pending", "trade_evidence_not_ready"}
        )
    )
    if (
        _safe_int(verification.get("verified")) > 0
        and _safe_int(pipeline.get("auto_promoted")) <= 0
        and _safe_int(acceptance.get("hard_gate_passed_count")) <= 0
    ):
        if hard_gate_pending_is_sample_debt:
            _append_quality_issue(
                quality_snapshot,
                "incubation_pending_execution_sample_evidence",
                "incubation has execution evidence but hard-gate pass is still waiting for realized closed trades",
            )
        else:
            _fail(
                "incubation_zero_promotion_and_zero_hard_gate",
                "incubation verified strategies but produced no auto promotion and no execution hard-gate pass",
            )
    if acceptance_status == "needs_remediation":
        _fail(
            "execution_audit_acceptance_needs_remediation",
            "execution audit acceptance reported needs_remediation",
        )
    if _safe_int(acceptance_gate_status_counts.get("missing")) > 0:
        _fail(
            "execution_audit_gate_missing",
            "execution audit acceptance still reported missing gate status for evaluated audit candidates",
        )
    if _safe_int(acceptance.get("awaiting_paper_execution_count")) > 0:
        _append_quality_issue(
            quality_snapshot,
            "execution_audit_awaiting_paper_execution",
            "some incubation strategies have signals/orders but no auditable paper execution yet",
        )
    signal_only_backlog = _safe_int(paper_execution_backlog.get("signal_only_backlog_count"))
    paper_backlog_selected = _safe_int(paper_execution_backlog.get("selected_count"))
    paper_backlog_orders_created = _safe_int(paper_execution_backlog.get("orders_created"))
    if signal_only_backlog > 0:
        _append_quality_issue(
            quality_snapshot,
            "paper_signal_only_backlog_remaining",
            f"signal-only paper execution backlog remains ({signal_only_backlog} strategies)",
        )
    if paper_backlog_selected > 0 and paper_backlog_orders_created <= 0:
        _fail(
            "paper_execution_backlog_not_converted",
            "paper execution backlog phase selected signal-only strategies but created no paper orders",
        )
    if str(native_evidence_backfill.get("status") or "").strip() == "needs_remediation":
        _fail(
            "native_execution_evidence_backfill_needs_remediation",
            "native execution evidence backfill found paper execution but saved no signal evidence",
        )
    if _safe_int(acceptance.get("evaluated")) > 0 and available_signal_evidence <= 0:
        _fail(
            "signal_evidence_unavailable",
            "execution audit acceptance evaluated strategies but no saved or existing signal evidence was available",
        )
    if _safe_int(settlement.get("evaluated")) > 0 and _safe_int(settlement.get("filled")) <= 0:
        _append_quality_issue(
            quality_snapshot,
            "paper_settlement_no_fills",
            "paper order settlement ran but produced no filled trades; this is a visible blocker unless explained by signal direction/liquidity",
        )

    quality_snapshot["health_summary"] = {
        "healthy": not health_failures,
        "failures": list(dict.fromkeys(health_failures)),
        "factory_status": factory_status or None,
        "signal_tracker_timeout": bool(signal_payload.get("timeout")),
        "signal_tracker_phase_timeouts": list(signal_payload.get("phase_timeouts") or []),
        "signals_generated": _safe_int(signal_payload.get("signals_generated")),
        "paper_orders_created": _safe_int(signal_payload.get("incubation_orders")),
        "paper_orders_filled": _safe_int(signal_payload.get("incubation_orders_filled")),
        "incubation_verified": _safe_int(verification.get("verified")),
        "incubation_auto_promoted": _safe_int(pipeline.get("auto_promoted")),
        "execution_audit_acceptance_status": acceptance_status or None,
        "execution_audit_gate_status_counts": acceptance_gate_status_counts,
        "execution_audit_sample_blockers": sorted(acceptance_sample_blockers),
        "saved_signal_evidence": _safe_int(acceptance.get("saved_signal_evidence_count")),
        "available_signal_evidence": available_signal_evidence,
        "awaiting_paper_execution": _safe_int(acceptance.get("awaiting_paper_execution_count")),
        "execution_hard_gate_passed": _safe_int(acceptance.get("hard_gate_passed_count")),
        "signal_only_backlog": signal_only_backlog,
        "paper_execution_backlog_selected": paper_backlog_selected,
        "paper_execution_backlog_orders_created": paper_backlog_orders_created,
        "paper_execution_backlog_orders_filled": _safe_int(paper_execution_backlog.get("orders_filled")),
        "paper_execution_backlog_skip_reasons": dict(
            paper_execution_backlog.get("skip_reason_counts") or {}
        ),
        "trades_without_signal_evidence": _safe_int(
            native_evidence_backfill.get("trades_without_signal_evidence_count")
        ),
        "native_evidence_backfill_saved": _safe_int(
            native_evidence_backfill.get("saved_signal_evidence_count")
        ),
        "open_positions": _safe_int(acceptance.get("open_position_count")),
        "closed_round_trips": _safe_int(acceptance.get("closed_round_trip_count")),
        "real_paper_round_trips": _safe_int(acceptance.get("real_paper_round_trip_count")),
        "bootstrap_round_trips": _safe_int(acceptance.get("bootstrap_round_trip_count")),
        "estimated_round_trip_sample_debt": _safe_int(
            acceptance.get("estimated_round_trip_sample_debt")
        ),
    }
    quality_snapshot["healthy"] = bool(quality_snapshot["health_summary"]["healthy"])


async def _run_round(
    *,
    round_no: int,
    factory_mod,
    codes: list[str],
    mode_config: QualitySessionModeConfig,
    runtime_controls: dict[str, Any],
    with_incubation: bool,
    run_factor_mining: bool,
    strategy_sample_limit: int,
    universe_limit: int = 0,
    mode_run_index: int = 1,
    mode_count: int = 1,
) -> dict[str, Any]:
    applied_env = apply_quality_mode_env(mode_config, runtime_controls=runtime_controls)
    factor_mining_run = await _run_factor_mining_once(run_factor_mining, round_no=round_no)
    factory_run = await _run_strategy_factory_once(
        factory_mod=factory_mod,
        codes=codes,
        execution_mode=mode_config.execution_mode,
        universe_limit=universe_limit,
    )
    run_ids = list(factory_run.get("run_ids") or [])
    signal_tracker_run = await _run_signal_tracker_once(with_incubation)
    incubation_run = await _run_incubation_once(with_incubation)
    factor_shelf_status = await _collect_factor_shelf_status() if with_incubation else None
    quality_snapshot = await _collect_run_snapshot(run_ids[0], strategy_sample_limit) if run_ids else {
        "run_id": None,
        "detail": {},
        "strategy_ids": [],
        "sampled_strategies": [],
        "blocker_summary": {},
        "issue_flags": ["missing_run_id"],
        "issue_notes": ["factory runner did not return a run_id and fallback lookup failed"],
    }
    _augment_quality_snapshot_with_factor_evidence(
        quality_snapshot,
        factor_mining_run,
        run_factor_mining=run_factor_mining,
    )
    incubation_result = dict((incubation_run or {}).get("result") or {})
    paper_backlog = await _collect_paper_observation_backlog()
    incubation_intake = dict(incubation_result.get("intake") or {})
    incubation_verification = dict(incubation_result.get("verification") or {})
    incubation_settlement = dict(incubation_result.get("settlement") or {})
    incubation_pipeline = dict(incubation_result.get("pipeline") or {})
    incubation_acceptance = dict(incubation_result.get("execution_audit_acceptance") or {})
    incubation_report = dict(incubation_result.get("report") or {})
    paper_intake = dict(incubation_intake.get("paper_observation_intake") or {})
    compact_factory_result = {
        "success": bool((factory_run.get("result") or {}).get("success")),
        "error": (factory_run.get("result") or {}).get("error"),
        "data": {
            "status": dict((factory_run.get("result") or {}).get("data") or {}).get("status"),
            "run_id": dict((factory_run.get("result") or {}).get("data") or {}).get("run_id"),
            "elapsed_seconds": dict((factory_run.get("result") or {}).get("data") or {}).get("elapsed_seconds"),
        },
    }
    _augment_quality_snapshot_with_runtime_health(
        quality_snapshot,
        factory_result=compact_factory_result,
        signal_tracker_run=signal_tracker_run,
        incubation_result=incubation_result,
    )
    compact_incubation_result = {
        "started_at": (incubation_run or {}).get("started_at"),
        "completed_at": (incubation_run or {}).get("completed_at"),
        "result": {
            "status": incubation_result.get("status"),
            "elapsed_seconds": incubation_result.get("elapsed_seconds"),
            "intake": {
                "accepted": incubation_intake.get("accepted"),
                "paper_observation_intake": {
                    "scanned": paper_intake.get("scanned"),
                    "recognized": paper_intake.get("recognized"),
                    "strategy_ids": list(paper_intake.get("strategy_ids") or []),
                },
            },
            "verification": {
                "total": incubation_verification.get("total"),
                "verified": incubation_verification.get("verified"),
                "incubating_count": incubation_verification.get("incubating_count"),
                "paper_count": incubation_verification.get("paper_count"),
                "diagnostic_count": incubation_verification.get("diagnostic_count"),
                "errors": incubation_verification.get("errors"),
            },
            "settlement": {
                "evaluated": incubation_settlement.get("evaluated"),
                "filled": incubation_settlement.get("filled"),
                "rejected": incubation_settlement.get("rejected"),
                "errors": incubation_settlement.get("errors"),
            },
            "pipeline": {
                "paper_count": incubation_verification.get("paper_count"),
                "count": incubation_pipeline.get("count"),
                "auto_promoted": incubation_pipeline.get("auto_promoted"),
                "stage_counts": dict(incubation_pipeline.get("stage_counts") or {}),
            },
            "execution_audit_acceptance": {
                "status": incubation_acceptance.get("status"),
                "healthy": incubation_acceptance.get("healthy"),
                "blockers": list(incubation_acceptance.get("blockers") or []),
                "sample_blockers": list(incubation_acceptance.get("sample_blockers") or []),
                "evaluated": incubation_acceptance.get("evaluated"),
                "errors": incubation_acceptance.get("errors"),
                "backfill": incubation_acceptance.get("backfill"),
                "candidate_count": incubation_acceptance.get("candidate_count"),
                "execution_evidence_candidate_count": incubation_acceptance.get("execution_evidence_candidate_count"),
                "awaiting_paper_execution_count": incubation_acceptance.get("awaiting_paper_execution_count"),
                "no_execution_evidence_count": incubation_acceptance.get("no_execution_evidence_count"),
                "saved_signal_evidence_count": incubation_acceptance.get("saved_signal_evidence_count"),
                "available_signal_evidence_count": incubation_acceptance.get("available_signal_evidence_count"),
                "hard_gate_passed_count": incubation_acceptance.get("hard_gate_passed_count"),
                "overall_ready_count": incubation_acceptance.get("overall_ready_count"),
                "native_lineage_ready_count": incubation_acceptance.get("native_lineage_ready_count"),
                "trade_evidence_ready_count": incubation_acceptance.get("trade_evidence_ready_count"),
                "status_counts": dict(incubation_acceptance.get("status_counts") or {}),
                "gate_status_counts": dict(incubation_acceptance.get("gate_status_counts") or {}),
            },
            "report": {
                "overall_hit_rate": incubation_report.get("overall_hit_rate"),
                "overall_skill_lcb": incubation_report.get("overall_skill_lcb"),
            },
            "paper_observation_backlog": {
                "paper_observation_backlog_count": paper_backlog.get("paper_observation_backlog_count"),
                "paper_observation_backlog_status": paper_backlog.get("paper_observation_backlog_status"),
                "paper_observation_active_count": paper_backlog.get("paper_observation_active_count"),
                "paper_observation_last_recognized_at": paper_backlog.get("paper_observation_last_recognized_at"),
                "paper_observation_latest_active_at": paper_backlog.get("paper_observation_latest_active_at"),
            },
        },
    } if incubation_run else None
    return {
        "round": round_no,
        "quality_mode": mode_config.mode_id,
        "quality_mode_label": mode_config.label,
        "quality_mode_description": mode_config.description,
        "mode_run_index": mode_run_index,
        "mode_count": mode_count,
        "mode_config": mode_config.as_state_dict(),
        "applied_runtime_env": applied_env,
        "factory_started_at": factory_run.get("started_at"),
        "factory_completed_at": factory_run.get("completed_at"),
        "factory_result": compact_factory_result,
        "run_ids": run_ids,
        "quality_snapshot": quality_snapshot,
        "factor_mining_result": factor_mining_run,
        "factor_shelf_status": factor_shelf_status,
        "signal_tracker_result": signal_tracker_run,
        "incubation_result": compact_incubation_result,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Strategy Factory repeatedly and record each round.")
    parser.add_argument("--hours", type=float, default=24.0, help="session duration in hours")
    parser.add_argument("--pause-sec", type=int, default=300, help="sleep time between completed rounds")
    parser.add_argument("--max-runs", type=int, default=0, help="stop after N rounds; 0 means no explicit limit")
    parser.add_argument("--codes", nargs="*", default=["601288"], help="target stock codes; omit value to use current default universe")
    parser.add_argument("--universe-limit", type=int, default=0, help="若 >0 走 dispatch 默认 universe 模式,覆盖全市场前 N 只(优先级高于 --codes;中等规模验证建议 200-500)。为 0 时用显式 --codes")
    parser.add_argument(
        "--quality-modes",
        default=None,
        help=(
            "comma/space separated quality session modes: observe-first, strict-gated, or compare; "
            "env: STRATEGY_QUALITY_SESSION_MODES"
        ),
    )
    parser.add_argument(
        "--execution-mode",
        default=None,
        help="base strategy factory execution mode for single-mode sessions",
    )
    parser.add_argument(
        "--observe-execution-mode",
        default=None,
        help="execution mode for observe-first quality sessions",
    )
    parser.add_argument(
        "--strict-execution-mode",
        default=None,
        help="execution mode for strict-gated quality sessions",
    )
    parser.add_argument(
        "--min-validation-grade",
        default=None,
        help="quality-session min validation grade; env: STRATEGY_QUALITY_SESSION_MIN_VALIDATION_GRADE",
    )
    parser.add_argument(
        "--gate3-record-only",
        default=None,
        choices=["0", "1", "false", "true", "off", "on", "no", "yes"],
        help="enable/disable Strategy Factory Gate-3 record-only mode for the session",
    )
    parser.add_argument(
        "--gate3-record-only-intake",
        default=None,
        choices=["0", "1", "false", "true", "off", "on", "no", "yes"],
        help="enable/disable Incubation Factory intake for Gate-3 record-only rows",
    )
    parser.add_argument("--report", default=None, help="root-level markdown report path")
    parser.add_argument("--session-id", default=None, help="explicit session id")
    parser.add_argument("--strategy-sample-limit", type=int, default=3, help="number of submitted strategies to sample per round")
    parser.add_argument("--with-incubation", action="store_true", help="run one incubation-factory round after each strategy round")
    parser.add_argument(
        "--skip-factor-mining",
        action="store_true",
        help="with --with-incubation, skip the heavy factor-mining cycle but still collect factor shelf status and run incubation",
    )
    parser.add_argument("--resume", action="store_true", help="resume an existing session_id/state.json if present")
    parser.add_argument("--render-only", action="store_true", help="only reload state.json and rewrite the markdown report")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    parser.add_argument(
        "--enable-compensation",
        action="store_true",
        help="⚠️ 启用补偿逻辑(backlog/backfill/stale close) - 这不是生产观察模式,仅用于补证据验证",
    )
    return parser.parse_args()


async def _async_main(args: argparse.Namespace) -> int:
    factory_mod = _factory_module()
    paths = SessionPaths(
        logs_dir=_resolve_logs_dir(args.session_id),
        state_json=_resolve_logs_dir(args.session_id) / "state.json",
        report_md=_resolve_report_path(args.report),
        rounds_dir=_resolve_logs_dir(args.session_id) / "rounds",
        lock_json=_resolve_logs_dir(args.session_id) / "session.lock.json",
    )
    run_lock = SessionRunLock(paths.lock_json)
    state = _load_existing_state(paths) if bool(args.resume) or bool(args.render_only) else None
    if state is None:
        if bool(args.render_only):
            LOGGER.error("Render-only requested but no existing state found for session_id=%s", args.session_id)
            return 1
        state = _build_session_state(args, paths)
        _persist_state(state, paths)
    else:
        mode_configs = _session_mode_configs(args, state)
        if mode_configs:
            args.quality_mode_configs = mode_configs
        runtime_controls = _session_runtime_controls(args, state)
        session = dict(state.get("session") or {})
        state["session"] = session
        session.setdefault("session_id", args.session_id)
        session.setdefault("report_path", str(paths.report_md))
        session.setdefault("logs_dir", str(paths.logs_dir))
        session.setdefault("rounds_dir", str(paths.rounds_dir))
        session.setdefault("hours", args.hours)
        session.setdefault("pause_sec", args.pause_sec)
        session.setdefault("codes", list(args.codes or []))
        session.setdefault("quality_modes", [mode.as_state_dict() for mode in mode_configs])
        session.setdefault(
            "quality_session_mode",
            "compare" if len(mode_configs) > 1 else (mode_configs[0].mode_id if mode_configs else "observe_first"),
        )
        session.setdefault(
            "execution_mode",
            (
                mode_configs[0].execution_mode
                if len(mode_configs) == 1
                else ",".join(mode.execution_mode for mode in mode_configs)
            ),
        )
        session.setdefault("runtime_controls", runtime_controls)
        session.setdefault("python_executable", sys.executable)
        session.setdefault("sqlite_path", str(default_sqlite_path()))
        session.setdefault("with_incubation", bool(args.with_incubation))
        session.setdefault("run_factor_mining", bool(args.with_incubation and not args.skip_factor_mining))
        session.setdefault("strategy_sample_limit", args.strategy_sample_limit)
        if not bool(args.render_only):
            _mark_session_active(state)
        await _refresh_state_strategy_persistence_metadata(state)
        _persist_state(state, paths)

    if bool(args.render_only):
        await close_db()
        LOGGER.info("Render-only refresh complete: report=%s rounds=%s", paths.report_md, len(state.get("entries") or []))
        return 0

    run_lock.acquire(session_id=args.session_id)
    try:
        mode_configs = _session_mode_configs(args, state)
        if not mode_configs:
            LOGGER.error("No quality session modes resolved; cannot start session")
            return 2
        runtime_controls = _session_runtime_controls(args, state)
        session = dict(state.get("session") or {})
        session_hours = max(0.01, float(session.get("hours") or args.hours))
        session_started_at = str(session.get("started_at") or _iso_now())
        try:
            session_deadline = datetime.fromisoformat(session_started_at) + timedelta(hours=session_hours)
        except Exception:
            session_deadline = _now() + timedelta(hours=session_hours)

        round_no = max((_safe_int(entry.get("round")) for entry in list(state.get("entries") or [])), default=0)
        while _now() < session_deadline:
            if args.max_runs > 0 and round_no >= args.max_runs:
                break
            round_no += 1
            LOGGER.info(
                "Starting comparison round %s with modes=%s",
                round_no,
                ",".join(mode.mode_id for mode in mode_configs),
            )
            for mode_index, mode_config in enumerate(mode_configs, start=1):
                LOGGER.info(
                    "Starting round %s mode=%s execution_mode=%s observe_first=%s wide_intake=%s",
                    round_no,
                    mode_config.mode_id,
                    mode_config.execution_mode,
                    mode_config.observe_first_enabled,
                    mode_config.wide_intake_observe_enabled,
                )
                entry = await _run_round(
                    round_no=round_no,
                    factory_mod=factory_mod,
                    codes=list(args.codes or []),
                    mode_config=mode_config,
                    runtime_controls=runtime_controls,
                    with_incubation=bool(args.with_incubation),
                    run_factor_mining=bool(args.with_incubation and not args.skip_factor_mining),
                    strategy_sample_limit=max(1, int(args.strategy_sample_limit)),
                    universe_limit=max(0, int(getattr(args, "universe_limit", 0) or 0)),
                    mode_run_index=mode_index,
                    mode_count=len(mode_configs),
                )
                state["entries"].append(entry)
                _persist_round(entry, paths)
                _persist_state(state, paths)
                await close_db()

            if args.max_runs > 0 and round_no >= args.max_runs:
                break
            if _now() >= session_deadline:
                break
            if args.pause_sec > 0:
                LOGGER.info("Sleeping %ss before next round", args.pause_sec)
                await asyncio.sleep(args.pause_sec)

        state["updated_at"] = _iso_now()
        state["session"]["completed_at"] = _iso_now()
        _persist_state(state, paths)
        await close_db()
        LOGGER.info("Session finished: rounds=%s report=%s", len(state.get("entries") or []), paths.report_md)
        return 0
    finally:
        run_lock.release()


def main() -> int:
    args = _parse_args()
    ensure_runtime_dirs()
    args.session_id = _resolve_session_id(args.session_id)
    logs_dir = _resolve_logs_dir(args.session_id)
    _configure_logging(logs_dir, verbose=bool(args.verbose))
    sqlite_path = default_sqlite_path()
    _apply_base_session_env(sqlite_path)
    _apply_quality_session_runtime_defaults()
    _apply_compensation_logic_if_enabled(args)  # P0 FIX: 补偿逻辑条件启用
    load_mcp_env(override=False)
    try:
        args.quality_mode_configs = resolve_quality_session_modes(
            args.quality_modes,
            execution_mode=args.execution_mode,
            observe_execution_mode=args.observe_execution_mode,
            strict_execution_mode=args.strict_execution_mode,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2
    args.runtime_controls = resolve_session_runtime_controls(args)
    args.runtime_controls_explicit = bool(
        str(args.min_validation_grade or "").strip()
        or args.gate3_record_only is not None
        or args.gate3_record_only_intake is not None
        or str(os.getenv("STRATEGY_QUALITY_SESSION_MIN_VALIDATION_GRADE") or "").strip()
        or str(os.getenv("STRATEGY_QUALITY_SESSION_GATE3_RECORD_ONLY_ENABLED") or "").strip()
        or str(os.getenv("STRATEGY_QUALITY_SESSION_GATE3_RECORD_ONLY_INTAKE_ENABLED") or "").strip()
    )
    args.quality_modes_explicit = bool(
        str(args.quality_modes or "").strip()
        or str(os.getenv("STRATEGY_QUALITY_SESSION_MODES") or "").strip()
    )
    if args.quality_mode_configs:
        apply_quality_mode_env(args.quality_mode_configs[0], runtime_controls=args.runtime_controls)
    ensure_default_runtime_services()
    LOGGER.info(
        "Session bootstrap: session_id=%s report=%s python=%s sqlite=%s quality_modes=%s runtime_controls=%s",
        args.session_id,
        _resolve_report_path(args.report),
        sys.executable,
        sqlite_path,
        ",".join(mode.mode_id for mode in list(args.quality_mode_configs or [])),
        args.runtime_controls,
    )
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
