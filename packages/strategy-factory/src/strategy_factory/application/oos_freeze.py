"""Prospective OOS registration; sealing never grants quality admission.

Phase one creates immutable pending experiments from lossless evaluated
candidate archives. Maturity checks, input-data snapshots, evaluation claims,
replay publication and admission re-evaluation are deliberately not implied.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
from importlib import metadata
import math
from pathlib import Path
import platform
from typing import Any
from zoneinfo import ZoneInfo

from ..infrastructure.holdout_seals import (
    canonical_json, database_utc_now, decode_seal, insert_seal,
    json_digest, open_seal_writer, read_seals,
)
from .candidate_archive import CANDIDATE_RECORDING_VERSION, read_candidate_archive


PROTOCOL_VERSION = "strategy_factory.prospective_holdout.v1"
MARKET_TIMEZONE = "Asia/Shanghai"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_COMPACT_MODES = {"compact_json", "dropped_large_payload", "inline_compact_json", "inline_fallback_summary"}


def require_complete_json(value: Any) -> None:
    """Reject storage previews, including nested params and archive references."""
    if isinstance(value, dict):
        if (
            value.get("truncated") is True
            or value.get("storage_mode") in _COMPACT_MODES
            or value.get("truncated_item_count")
            or value.get("truncated_key_count")
            or value.get("dropped_large_nodes")
        ):
            raise ValueError("holdout_compact_or_truncated_input")
        for item in value.values():
            require_complete_json(item)
    elif isinstance(value, list):
        for item in value:
            require_complete_json(item)


def prepare_holdout_execution(candidate: dict[str, Any]) -> dict[str, Any]:
    """Use exactly the envelope, target resolver and merge used by Gate 2/OOS."""
    from aiask_quant_core.backtest.builtin_strategies import BuyAndHoldStrategy, MaCrossStrategy, MomentumStrategy
    from aiask_quant_core.backtest.dsl_strategy import DslRuleStrategy
    from aiask_quant_core.backtest.strategy_registry import StrategyRegistry
    from .backtest_filter import BacktestFilter, _ensure_strategy_params
    from .candidate_contract import apply_resolved_candidate_envelope, build_factory_backtest_assumptions
    from .oos_replay import holdout_identity

    require_complete_json(candidate)
    if not isinstance(candidate.get("params"), dict) or not candidate.get("strategy_type"):
        raise ValueError("holdout_candidate_execution_contract_missing")
    resolved = apply_resolved_candidate_envelope(candidate)
    _, codes, _, _, _ = BacktestFilter._resolve_backtest_plan(resolved)
    if not codes:
        raise ValueError("holdout_target_symbols_missing")
    params = {
        **_ensure_strategy_params(resolved["strategy_type"], resolved["params"], research_task=resolved.get("research_task")),
        **build_factory_backtest_assumptions(resolved).to_backtest_kwargs(),
    }
    runtime, semantic_mode = StrategyRegistry.create_runtime_strategy(resolved["strategy_type"], params)
    if params.get("dsl") and semantic_mode != "compiled_dsl":
        raise ValueError("holdout_executable_contract_unavailable")
    if type(runtime) not in {BuyAndHoldStrategy, MaCrossStrategy, MomentumStrategy, DslRuleStrategy}:
        raise ValueError("holdout_strategy_state_reset_unsupported")
    raw_weights = params.get("target_weight_map") or {}
    weights = [float(raw_weights.get(code, 1.0)) for code in codes]
    if any(not math.isfinite(weight) or weight < 0 for weight in weights) or sum(weights) <= 0:
        raise ValueError("holdout_invalid_target_weights")
    return {
        "resolved_candidate": resolved,
        "effective_execution_params": params,
        "target_codes": list(codes),
        "normalized_target_weights": dict(zip(codes, [weight / sum(weights) for weight in weights])),
        "runtime_strategy": f"{type(runtime).__module__}.{type(runtime).__qualname__}",
        "semantic_mode": semantic_mode,
        **holdout_identity(resolved, params),
    }


def execution_code_identity() -> dict[str, Any]:
    """Hash actual working-tree sources, including dirty changes, plus versions."""
    import aiask_quant_core.backtest as backtest

    roots = {
        "strategy_factory": Path(__file__).resolve().parents[1],
        # DSL evaluation, slippage and other engine dependencies live outside
        # backtest/, so hashing just the engine package would miss code changes.
        "aiask_quant_core": Path(backtest.__file__).resolve().parents[1],
    }
    sources = {}
    for label, root in roots.items():
        for path in sorted(root.rglob("*.py")):
            sources[f"{label}/{path.relative_to(root).as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    dependencies = {}
    for name in ("numpy", "pandas", "scipy", "numba", "strategy-factory", "aiask-quant-core"):
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None
    manifest = {"source_sha256": sources, "dependencies": dependencies, "python": platform.python_version(), "platform": platform.platform()}
    return {**manifest, "sha256": json_digest(manifest)}


def plan_future_window(frozen_at_utc: str, min_sessions: int = 20) -> dict[str, Any]:
    from ..domain import trading_calendar

    if isinstance(min_sessions, bool) or not isinstance(min_sessions, int) or min_sessions < 20:
        raise ValueError("holdout_minimum_twenty_sessions")
    if min_sessions > 252:
        raise ValueError("holdout_window_too_large")
    frozen = datetime.fromisoformat(frozen_at_utc.replace("Z", "+00:00"))
    if frozen.tzinfo is None:
        raise ValueError("holdout_freeze_timezone_missing")
    local_date = frozen.astimezone(ZoneInfo(MARKET_TIMEZONE)).date()
    cursor, sessions = local_date, []
    # Explicitly use this known fallback rather than silently misattribute a
    # failed external provider. Refuse years absent from its maintained table.
    while len(sessions) < min_sessions:
        cursor += timedelta(days=1)
        if cursor.year not in {2025, 2026}:
            raise ValueError("holdout_calendar_year_unverified")
        if cursor.weekday() < 5 and cursor not in trading_calendar._KNOWN_HOLIDAYS:
            sessions.append(cursor.isoformat())
    return {
        "planned_start": sessions[0], "planned_end": sessions[-1],
        "min_sessions": min_sessions, "planned_sessions": sessions,
        "market_timezone": MARKET_TIMEZONE,
        "calendar": {
            "source": "strategy_factory.domain.trading_calendar._KNOWN_HOLIDAYS",
            "mode": "maintained_holiday_fallback",
            "verified_years": [2025, 2026],
            "source_sha256": hashlib.sha256(Path(trading_calendar.__file__).read_bytes()).hexdigest(),
            "actual_sessions_require_maturity_verification": True,
        },
    }


def _pending_view(seal: dict[str, Any], now: str, *, include_snapshot: bool = False) -> dict[str, Any]:
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(ZoneInfo(MARKET_TIMEZONE)).date().isoformat()
    result = {key: value for key, value in seal.items() if key != "snapshot"}
    result.update({
        "status": "sealed_pending", "total_return": None,
        "reason": "holdout_window_not_mature" if today <= seal["planned_end"] else "holdout_maturity_evaluator_not_implemented",
        "quality_admission_granted": False,
    })
    if include_snapshot:
        result["snapshot"] = seal["snapshot"]
    return result


def freeze_candidates_from_archive(
    database: str | Path,
    archive_reference: dict[str, Any],
    *,
    selection_data_end: str,
    candidate_ids: list[str] | None = None,
    min_sessions: int = 20,
    max_candidates: int = 100,
) -> dict[str, Any]:
    """Atomically register a bounded set; identical retries return old seals.

    selection_data_end is an operator-declared upper bound, not a fabricated
    per-security data audit. All information is conservatively bounded by the
    database freeze time, so the observation window always starts in the future.
    No caller clock, historical window or quality override is accepted.
    """
    require_complete_json(archive_reference)
    if (
        archive_reference.get("contract_version") != CANDIDATE_RECORDING_VERSION
        or archive_reference.get("storage_mode") != "external_gzip_json"
        or archive_reference.get("complete") is not True
        or archive_reference.get("phase") != "evaluated"
    ):
        raise ValueError("holdout_complete_evaluated_archive_required")
    cutoff = date.fromisoformat(selection_data_end)
    if cutoff.isoformat() != selection_data_end:
        raise ValueError("holdout_selection_data_end_invalid")
    document = read_candidate_archive(archive_reference)
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("holdout_archive_candidates_missing")
    # Validate selection before opening the writer; no silent first-N preview.
    indexed = {}
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError("holdout_archive_candidate_invalid")
        identifier = str(candidate.get("candidate_id") or f"{document['run_id']}:archive:{index}")
        if identifier in indexed:
            raise ValueError("holdout_archive_candidate_id_duplicate")
        indexed[identifier] = (index, candidate)
    selected = list(indexed) if candidate_ids is None else list(dict.fromkeys(candidate_ids))
    if not selected or len(selected) > max_candidates or max_candidates < 1:
        raise ValueError("holdout_candidate_budget_invalid")
    if any(identifier not in indexed for identifier in selected):
        raise ValueError("holdout_candidate_id_not_found")
    code_identity = execution_code_identity()
    prepared = []
    protocol = {
        "version": PROTOCOL_VERSION, "min_sessions": min_sessions,
        "warmup_min_sessions": 60, "initial_state": "cash_at_holdout_start",
        "force_liquidate_at_end": True, "aggregation": "fixed_initial_capital_sleeves",
        "missing_data_policy": "unavailable_without_dropping_targets_or_moving_window",
        "selection_policy": "explicit_archive_candidate_ids" if candidate_ids is not None else "all_archive_candidates",
        "quality_admission": "unchanged_requires_separate_gate_evaluation",
    }
    for identifier in selected:
        index, candidate = indexed[identifier]
        execution = prepare_holdout_execution(candidate)
        intent = {
            "source_run_id": document["run_id"], "candidate_id": identifier,
            "source_archive_sha256": archive_reference["sha256"], "source_candidate_index": index,
            "source_candidate_sha256": json_digest(candidate),
            "execution_identity": {key: execution[key] for key in ("tested_object_hash", "candidate_contract_hash", "execution_params_hash")},
            "code_sha256": code_identity["sha256"], "protocol": protocol,
            "selection_data_end": selection_data_end,
        }
        snapshot = {
            **intent, "experiment_key": json_digest(intent),
            "archive_reference": archive_reference, "final_candidate": candidate,
            **execution, "execution_code_identity": code_identity,
            "selection_data_end_basis": "operator_declared_upper_bound",
            "selection_information_as_of_basis": "conservative_database_freeze_time_upper_bound",
            "selection_input_data_snapshots": None,
            "selection_input_data_snapshots_reason": "archive_does_not_contain_per_source_market_input_snapshots",
            "quality_admission_granted": False,
        }
        if len(canonical_json(snapshot).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("holdout_snapshot_size_limit_exceeded")
        prepared.append(snapshot)
    # Protect against another editor changing the execution sources while this
    # invocation resolves candidates. Subsequent evaluation must pin this hash.
    if execution_code_identity()["sha256"] != code_identity["sha256"]:
        raise ValueError("holdout_execution_code_changed_during_preparation")
    connection = open_seal_writer(database)
    seals, created = [], 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = database_utc_now(connection)
        local_day = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(ZoneInfo(MARKET_TIMEZONE)).date()
        if cutoff > local_day:
            raise ValueError("holdout_selection_data_end_in_future")
        window = None
        for snapshot in prepared:
            row = connection.execute("SELECT * FROM strategy_holdout_seals WHERE experiment_key=?", (snapshot["experiment_key"],)).fetchone()
            if row is not None:
                seal = decode_seal(row)
                was_created = False
            else:
                if window is None:
                    window = plan_future_window(now, min_sessions)
                snapshot.update({
                    **window, "seal_id": "oos_" + snapshot["experiment_key"],
                    "frozen_at_utc": now, "selection_information_as_of": now,
                })
                if len(canonical_json(snapshot).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                    raise ValueError("holdout_snapshot_size_limit_exceeded")
                seal = insert_seal(connection, snapshot)
                was_created = True
                created += 1
            seals.append({**_pending_view(seal, now), "created": was_created})
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"status": "sealed_pending", "observed_at_utc": now, "created_count": created, "existing_count": len(seals) - created, "measured_count": 0, "seals": seals}


def pending_holdout_status(database: str | Path, *, include_snapshots: bool = False) -> dict[str, Any]:
    now, seals, reason = read_seals(database)
    return {
        "status": "unavailable" if reason else "ok", "reason": reason,
        "observed_at_utc": now, "frozen_count": len(seals), "pending_count": len(seals),
        "measured_count": 0, "next_planned_end": min((seal["planned_end"] for seal in seals), default=None),
        "maturity_evaluator_implemented": False, "quality_admission_granted": False,
        "seals": [_pending_view(seal, now, include_snapshot=include_snapshots) for seal in seals],
    }
