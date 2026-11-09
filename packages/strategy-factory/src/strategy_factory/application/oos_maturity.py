"""Read-only audit and explicit replay of persisted prospective holdout seals.

This is an operator evidence tool, not an admission path. It uses one SQLite
read snapshot, preserves every selected raw bar and full engine result, and
never updates seals, strategy metrics or quality decisions. Run from a fresh
interpreter so the source manifest describes the imported execution code.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import math
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiask_quant_core.backtest import BacktestEngine
from aiask_quant_core.backtest.utils import normalize_trade_date

from ..infrastructure.holdout_seals import canonical_json, database_utc_now, decode_seal, json_digest
from .oos_freeze import execution_code_identity, prepare_holdout_execution, require_complete_json
from .oos_replay import replay_frozen_holdout


def _write_json(path: Path, value: Any) -> str:
    raw = canonical_json(value)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(raw + "\n")
    return json_digest(value)


def _read_snapshot(database: Path, seal_id: str) -> tuple[str, dict, dict]:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        now = database_utc_now(connection)
        row = connection.execute("SELECT * FROM strategy_holdout_seals WHERE seal_id=?", (seal_id,)).fetchone()
        if row is None:
            raise ValueError("holdout_seal_not_found")
        seal = decode_seal(row)
        snapshot = seal["snapshot"]
        # Fetch complete histories before filtering dates in Python; SQL string
        # date comparisons would silently omit alternate date representations.
        histories = {
            code: [dict(item) for item in connection.execute(
                "SELECT * FROM kline_1d WHERE code=? ORDER BY time", (code,)
            )]
            for code in snapshot["target_codes"]
        }
        return now, seal, histories
    finally:
        connection.close()


def _validate_histories(snapshot: dict, histories: dict) -> tuple[dict, dict]:
    sessions = snapshot["planned_sessions"]
    if (
        len(sessions) != snapshot["min_sessions"] or len(sessions) < 20
        or sessions != sorted(set(sessions))
        or sessions[0] != snapshot["planned_start"] or sessions[-1] != snapshot["planned_end"]
    ):
        raise ValueError("holdout_sealed_calendar_invalid")
    for day in sessions:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("holdout_sealed_calendar_invalid")
    selected, coverage = {}, {}
    for code in snapshot["target_codes"]:
        by_date = {}
        for row in histories.get(code, []):
            raw_day = normalize_trade_date(row.get("time"))
            try:
                day = datetime.strptime(raw_day, "%Y%m%d").date().isoformat()
            except (TypeError, ValueError) as exc:
                raise ValueError(f"holdout_invalid_trade_dates:{code}") from exc
            if day > snapshot["planned_end"]:
                continue
            if day in by_date:
                raise ValueError(f"holdout_duplicate_trade_date:{code}:{day}")
            for key in ("open", "high", "low", "close", "volume"):
                value = row.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"holdout_invalid_bar:{code}:{day}:{key}")
                if value < 0 or (key != "volume" and value == 0):
                    raise ValueError(f"holdout_invalid_bar:{code}:{day}:{key}")
            if not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]:
                raise ValueError(f"holdout_invalid_bar:{code}:{day}:ohlc_order")
            by_date[day] = row
        actual = [day for day in sorted(by_date) if day >= snapshot["planned_start"]]
        missing, unexpected = sorted(set(sessions) - set(actual)), sorted(set(actual) - set(sessions))
        coverage[code] = {"sessions": len(actual), "missing_sessions": missing, "unexpected_sessions": unexpected}
        if missing or unexpected:
            raise ValueError(f"holdout_calendar_incomplete_or_mismatched:{code}")
        warmup = [day for day in by_date if day < snapshot["planned_start"]]
        if len(warmup) < snapshot["protocol"]["warmup_min_sessions"]:
            raise ValueError(f"holdout_insufficient_warmup:{code}")
        coverage[code]["warmup_sessions"] = len(warmup)
        selected[code] = [by_date[day] for day in sorted(by_date)]
    return selected, coverage


async def evaluate_sealed_holdout(
    database: str | Path, seal_id: str, evidence_directory: str | Path,
    *, replay: bool = False, timeout: float = 30,
) -> dict[str, Any]:
    """Check readiness; replay only on explicit request. Never write the DB.

    Readiness requires each target to match every sealed calendar session. This
    verifies stored daily-bar coverage; it does not certify vendor accuracy,
    execution-audit quality, forward performance or strategy admission.
    """
    database = Path(database).expanduser().resolve()
    directory = Path(evidence_directory).expanduser().resolve() / ("holdout-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "unavailable", "reason": None, "total_return": None,
        "seal_id": seal_id, "database": str(database), "query_only": True,
        "quality_admission_granted": False, "database_writes": False,
        "evidence_directory": str(directory), "replay_requested": replay,
        "limitations": [
            "stored_bar_coverage_is_not_independent_vendor_accuracy_verification",
            "calendar_is_the_sealed_maintained_holiday_fallback",
            "strategy_admission_requires_separate_statistical_risk_forward_and_execution_evidence",
        ],
    }
    try:
        now, seal, histories = _read_snapshot(database, seal_id)
        snapshot = seal["snapshot"]
        require_complete_json(snapshot)
        result.update({
            "observed_at_utc": now, "snapshot_sha256": seal["snapshot_sha256"],
            "planned_start": seal["planned_start"], "planned_end": seal["planned_end"],
            "source_run_id": seal["source_run_id"], "candidate_id": seal["candidate_id"],
        })
        result["seal_evidence_sha256"] = _write_json(directory / "seal.json", seal)
        local_day = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        manifest = execution_code_identity()
        result["execution_code_sha256"] = manifest["sha256"]
        result["sealed_execution_code_sha256"] = snapshot["execution_code_identity"]["sha256"]
        result["execution_code_matches"] = manifest["sha256"] == result["sealed_execution_code_sha256"]
        result["window_elapsed"] = local_day > snapshot["planned_end"]
        # Conservatively wait until the next local date: an end-day bar could
        # still be intraday or not yet finalized by the data provider.
        if not result["window_elapsed"]:
            result.update(status="sealed_pending", reason="holdout_window_not_mature")
        elif not result["execution_code_matches"]:
            result["reason"] = "holdout_execution_code_mismatch"
        else:
            prepared = prepare_holdout_execution(snapshot["final_candidate"])
            for key in ("tested_object_hash", "candidate_contract_hash", "execution_params_hash", "target_codes", "normalized_target_weights"):
                if prepared[key] != snapshot[key]:
                    raise ValueError("holdout_sealed_execution_binding_mismatch")
            result["raw_input_snapshot_sha256"] = _write_json(directory / "raw-inputs.json", histories)
            selected, coverage = _validate_histories(snapshot, histories)
            result["coverage"] = coverage
            result["replay_input_snapshot_sha256"] = _write_json(directory / "replay-inputs.json", selected)
            result.update(status="ready_unmeasured", reason="holdout_replay_not_requested")
            if replay:
                candidate = deepcopy(snapshot["resolved_candidate"])
                candidate["oos_validation_contract"] = {
                    **{key: snapshot[key] for key in ("tested_object_hash", "candidate_contract_hash", "execution_params_hash")},
                    "sealed": True, "excluded_from_selection": True,
                    # The lower-level replay compares calendar-day labels.
                    # Express the same instant in market time so a UTC date
                    # boundary cannot incorrectly precede the local cutoff.
                    "frozen_at": datetime.fromisoformat(snapshot["frozen_at_utc"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
                    "selection_data_end": snapshot["selection_data_end"],
                    "start": snapshot["planned_start"], "end": snapshot["planned_end"],
                }
                captures = {}

                class EvidenceEngine:
                    @staticmethod
                    def run_backtest(code, *args):
                        output = BacktestEngine.run_backtest(code, *args)
                        captures[code] = output
                        return output

                measurement = await replay_frozen_holdout(
                    candidate, selected, EvidenceEngine, snapshot["effective_execution_params"],
                    codes=snapshot["target_codes"], timeout=timeout,
                )
                result["engine_results_sha256"] = _write_json(directory / "engine-results.json", captures)
                if execution_code_identity()["sha256"] != manifest["sha256"]:
                    raise ValueError("holdout_execution_code_changed_during_replay")
                result["measurement"] = measurement
                result.update({key: measurement[key] for key in ("status", "reason", "total_return")})
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        result.update(status="unavailable", reason=str(exc), total_return=None, error_type=type(exc).__name__)
    result["report_file"] = str(directory / "report.json")
    _write_json(directory / "report.json", result)
    return result
