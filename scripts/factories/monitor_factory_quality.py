#!/usr/bin/env python3
"""Observe the four-factory supervisor and record each strategy-run review.

The four-runtime supervisor already owns execution.  This process is deliberately
read-only with respect to the factory database: it waits for completed strategy
runs, joins their quality reports through ``factory_run_id``, and writes a compact
JSON ledger plus a daily Markdown report.  It never submits a factory run and it
never enables trading writes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_HOME = Path(os.getenv("AIASK_RUNTIME_HOME") or (Path.home() / ".aiask")).expanduser()
DB_PATH = Path(
    os.getenv("AIASK_SQLITE_PATH")
    or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    or (RUNTIME_HOME / "data" / "db" / "akshare_mcp.sqlite3")
).expanduser()
LOG_DIR = RUNTIME_HOME / "logs" / "four_runtime_supervisor"
STATE_PATH = RUNTIME_HOME / "run" / "four_factory_quality_observer.state.json"
PID_PATH = RUNTIME_HOME / "run" / "four_factory_quality_observer.pid"
REPORT_DIR = ROOT / "docs" / "strategy-factory-quality"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
REVIEW_VERSION = 4


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ)


def _fmt_dt(value: Any) -> str:
    parsed = _parse_dt(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S %z") if parsed else str(value or "")


def _json(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "" or isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _safe_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _read_state(*, since: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            for key, expected in (("since", since), ("session_id", session_id)):
                if expected is not None and payload.get(key) != expected:
                    raise RuntimeError(f"observer_state_scope_mismatch:{key}; select a new --state path")
            payload.setdefault("entries", [])
            return payload
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    startup = LOG_DIR / "supervisor_startup.json"
    started_at = None
    try:
        started_at = json.loads(startup.read_text(encoding="utf-8")).get("started_at")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {
        "observer_started_at": _now().isoformat(),
        "supervisor_started_at": started_at,
        "last_run_rowid": 0,
        "since": since,
        "session_id": session_id,
        "entries": [],
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _supervisor_health() -> dict[str, Any]:
    path = LOG_DIR / "execution_records.jsonl"
    latest: dict[str, Any] | None = None
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
            try:
                item = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue
            if item.get("event_type") == "supervisor_heartbeat":
                latest = item
    except OSError:
        return {"status": "unavailable", "reason": "heartbeat log unavailable"}
    if not latest:
        return {"status": "unavailable", "reason": "no heartbeat found"}
    factories = list(latest.get("factories") or [])
    statuses = {str(x.get("factory")): str(x.get("status")) for x in factories if isinstance(x, dict)}
    restarts = {str(x.get("factory")): int(x.get("restart_count") or 0) for x in factories if isinstance(x, dict)}
    heartbeat = _parse_dt(latest.get("ts"))
    fresh = heartbeat is not None and 0 <= (_now() - heartbeat).total_seconds() <= 180
    expected = {"strategy_factory", "factor_mining_factory", "incubation_factory", "market_event_ingest"}
    alive = all(_alive(x.get("pid")) for x in factories if isinstance(x, dict))
    healthy = (set(statuses) == expected and all(value == "running" for value in statuses.values())
               and fresh and alive)
    return {
        "heartbeat_fresh": fresh,
        "processes_alive": alive,
        "status": "healthy" if healthy else "degraded",
        "heartbeat_at": latest.get("ts"),
        "factories": statuses,
        "restart_count": restarts,
    }


def _log_cycle_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {}
    market_path = LOG_DIR / "market_event_ingest.log"
    try:
        lines = market_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-800:]):
            match = re.search(r"round=(\d+) summary=(\{.*\})", line)
            if not match:
                continue
            payload = _json(match.group(2), {})
            result["market_event_ingest"] = {
                "round": int(match.group(1)),
                "timestamp": line[:19],
                "saved_docs": payload.get("saved_docs"),
                "normalized_total": payload.get("normalized_total"),
                "normalized_verified": payload.get("normalized_verified"),
                "errors": payload.get("error_count", len(payload.get("errors") or [])),
                "signals": (payload.get("bridge") or {}).get("signals"),
                "elapsed_sec": payload.get("elapsed_sec"),
            }
            break
    except OSError:
        pass

    factor_path = LOG_DIR / "factor_mining_factory.log"
    try:
        lines = factor_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-800:]):
            match = re.search(r"第\s*(\d+)\s*轮完成.*原始=(\d+)\s*进化=(\d+)\s*验证=(\d+)\s*入池=(\d+).*池大小=(\d+)", line)
            if match:
                result["factor_mining"] = {
                    "cycle": int(match.group(1)),
                    "timestamp": line[:19],
                    "raw_candidates": int(match.group(2)),
                    "evolved": int(match.group(3)),
                    "validated": int(match.group(4)),
                    "admitted": int(match.group(5)),
                    "pool_size": int(match.group(6)),
                }
                break
    except OSError:
        pass

    incubation_path = LOG_DIR / "incubation_factory.log"
    try:
        lines = incubation_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-1000:]):
            match = re.search(r"completed in ([0-9.]+)s \(status=([^,]+), verified=(\d+), promoted=(\d+)\)", line)
            if match:
                result["incubation"] = {
                    "timestamp": line[:19],
                    "elapsed_sec": float(match.group(1)),
                    "status": match.group(2).strip(),
                    "verified": int(match.group(3)),
                    "promoted": int(match.group(4)),
                }
                break
        for line in reversed(lines[-1000:]):
            match = re.search(r"execution audit acceptance: status=([^ ]+) evaluated=(\d+) saved_signal_evidence=(\d+) available_signal_evidence=(\d+) hard_gate_passed=(\d+)", line)
            if match:
                result.setdefault("incubation", {})["execution_audit"] = {
                    "status": match.group(1),
                    "evaluated": int(match.group(2)),
                    "saved_signal_evidence": int(match.group(3)),
                    "available_signal_evidence": int(match.group(4)),
                    "hard_gate_passed": int(match.group(5)),
                }
                break
    except OSError:
        pass
    return result


def _query_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM strategies GROUP BY status ORDER BY status").fetchall()
    return {str(row[0] or "unknown"): int(row[1]) for row in rows}


def _candidate_evidence(summary: dict, gate: dict, metrics: dict) -> dict[str, Any]:
    holdout = dict(metrics.get("oos_validation") or {})
    oos_return = _num(metrics.get("target_layer_oos_return"))
    measured = (holdout.get("status") == "measured"
                and holdout.get("source") == "frozen_candidate_holdout_replay"
                and holdout.get("sealed") is True
                and holdout.get("excluded_from_selection") is True
                and oos_return is not None)
    return {
        "post_cost_sharpe": _num(metrics.get("post_cost_sharpe")),
        "total_return": _num(metrics.get("total_return")),
        "trade_count": _num(metrics.get("trade_count", metrics.get("trades_count"))),
        "max_drawdown": _num(metrics.get("max_drawdown")),
        "parameter_perturbation_trade_stability": _num(metrics.get("parameter_perturbation_trade_stability")),
        "oos_measured": measured,
        "oos_return": oos_return if measured else None,
        "oos_unavailable_reason": None if measured else holdout.get("reason") or "missing_verified_oos_evidence",
        "oos_validation": holdout,
        "execution_audit_snapshot_id": summary.get("execution_audit_snapshot_id"),
        # Runtime readiness and incubation acceptance do not prove execution.
        "execution_hard_gate_passed": None,
        "requires_current_forward_and_execution_review": True,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _current_candidate_ledger(conn: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    """Keep every reported strategy and refresh forward/execution evidence.

    Completed run reviews remain historical. This ledger separately records the
    latest database state, so later paper evidence is not frozen at submission.
    """
    candidates: dict[str, dict[str, Any]] = {}
    for entry in state.get("entries") or []:
        for sample in (entry.get("quality") or {}).get("candidates") or []:
            sid = str(sample.get("strategy_id") or "")
            if not sid:
                continue
            old = candidates.get(sid, {})
            candidates[sid] = {
                **sample,
                "observed_run_ids": sorted(set(old.get("observed_run_ids") or []) | {entry["run_id"]}),
                "current_execution": {"status": "missing", "execution_hard_gate_passed": None},
                "current_forward": {"status": "missing"},
            }
    available = {name: _table_exists(conn, name) for name in (
        "strategies", "strategy_execution_audit_snapshots", "strategy_incubation_metrics",
    )}
    for sid, item in candidates.items():
        if available["strategies"]:
            row = conn.execute("SELECT id,name,strategy_type,params,factor_weights,status,created_at,updated_at FROM strategies WHERE id=?", (sid,)).fetchone()
            if row:
                item["strategy_snapshot"] = {**dict(row), "params": _json(row["params"], {}), "factor_weights": _json(row["factor_weights"], {})}
        if available["strategy_execution_audit_snapshots"]:
            row = conn.execute("SELECT snapshot_id,as_of_date,verdict_status,verdict_reasons,execution_hard_gate_passed,updated_at FROM strategy_execution_audit_snapshots WHERE strategy_id=? ORDER BY as_of_date DESC,updated_at DESC LIMIT 1", (sid,)).fetchone()
            if row:
                item["current_execution"] = {
                    **dict(row), "status": "observed",
                    "verdict_reasons": _json(row["verdict_reasons"], []),
                    "execution_hard_gate_passed": bool(row["execution_hard_gate_passed"]) if row["execution_hard_gate_passed"] is not None else None,
                }
        if available["strategy_incubation_metrics"]:
            row = conn.execute("SELECT metric_date,stage,hit_rate_5d,skill_lcb_5d,effective_n_5d,recent_hit_rate_5d,recent_skill_lcb_5d,forward_ic_5d,forward_sharpe_5d,blockers,decision,updated_at FROM strategy_incubation_metrics WHERE strategy_id=? ORDER BY metric_date DESC,updated_at DESC LIMIT 1", (sid,)).fetchone()
            if row:
                item["current_forward"] = {**dict(row), "status": "observed", "blockers": _json(row["blockers"], [])}
    return {
        "session_id": state.get("session_id"), "since": state.get("since"),
        "observed_at": _now().isoformat(), "review_version": REVIEW_VERSION,
        "recording_scope": "all_submission_reports_with_persisted_strategy_snapshots",
        "unique_strategy_count": len(candidates),
        "unique_tested_object_count": len({x["tested_object_hash"] for x in candidates.values() if x.get("tested_object_hash")}),
        "execution_hard_gate_passed_count": sum(x["current_execution"].get("execution_hard_gate_passed") is True for x in candidates.values()),
        "goal_completion_verified": False,
        "candidates": list(candidates.values()),
    }


def _archive_run_evidence(conn: sqlite3.Connection, entry: dict[str, Any]) -> dict[str, Any]:
    artifacts = []
    recording_gaps = []
    recording = None
    expected_counts = []
    if _table_exists(conn, "strategy_factory_run_artifacts"):
        for row in conn.execute("SELECT id,artifact_type,artifact_version,payload_json,payload_hash,storage_mode,created_at FROM strategy_factory_run_artifacts WHERE run_id=? ORDER BY id", (entry["run_id"],)):
            payload = _json(row["payload_json"], {})
            artifacts.append({**dict(row), "payload_json": payload})
            if row["artifact_type"] == "candidate_recording":
                recording = payload
            if row["artifact_type"] == "candidate_artifact":
                expected = int(payload.get("candidate_count") or 0)
                expected_counts.append(expected)
                details = payload.get("candidates") or payload.get("candidate_briefs") or []
                if len(details) < expected:
                    recording_gaps.append({"reason": "upstream_candidate_details_truncated", "candidate_count": expected, "retained_details": len(details)})
    full_recording = {"complete": False, "phases": {}}
    if recording is not None:
        from strategy_factory.application.candidate_archive import read_candidate_archive

        for phase in ("generated", "evaluated"):
            reference = recording.get(phase)
            if not isinstance(reference, dict):
                recording_gaps.append({"reason": "candidate_archive_phase_missing", "phase": phase})
                continue
            try:
                if reference.get("run_id") != entry["run_id"] or reference.get("phase") != phase:
                    raise ValueError("candidate_archive_reference_identity_mismatch")
                full_recording["phases"][phase] = read_candidate_archive(reference)
            except (OSError, ValueError, TypeError, KeyError, EOFError) as exc:
                recording_gaps.append({"reason": "candidate_archive_unreadable", "phase": phase,
                                       "error": f"{type(exc).__name__}: {exc}"})
        phases = full_recording["phases"]
        if set(phases) == {"generated", "evaluated"}:
            generated_count = len(phases["generated"].get("candidates") or [])
            evaluated_count = len(phases["evaluated"].get("candidates") or [])
            count_matches = generated_count == evaluated_count and all(
                count == generated_count for count in expected_counts
            )
            full_recording.update(generated_count=generated_count, evaluated_count=evaluated_count,
                                  complete=recording.get("complete") is True and count_matches)
            if full_recording["complete"]:
                recording_gaps = [gap for gap in recording_gaps
                                  if gap["reason"] != "upstream_candidate_details_truncated"]
            elif not count_matches:
                recording_gaps.append({"reason": "candidate_archive_count_mismatch",
                                       "generated_count": generated_count, "evaluated_count": evaluated_count,
                                       "summary_counts": expected_counts})
        if recording.get("complete") is not True:
            recording_gaps.append({"reason": "candidate_archive_incomplete"})
    return {"run": entry, "persisted_artifacts": artifacts,
            "full_candidate_recording": full_recording,
            "recording_gaps": recording_gaps,
            "recording_scope": "persisted artifacts and hash-verified full candidate archives when available"}


def _review_run(conn: sqlite3.Connection, run: sqlite3.Row, *, report_index: dict[str, list[int]] | None = None) -> dict[str, Any]:
    run_id = str(run["run_id"])
    started = _parse_dt(run["started_at"])
    completed = _parse_dt(run["completed_at"]) or started or _now()
    # Exact identity is authoritative, independent of SQLite UTC timestamps.
    select = ("SELECT id, strategy_id, passed, summary, quality_gate, validation_report, risk_report, "
              "backtest_metrics, created_at FROM strategy_quality_reports WHERE report_type='submission' ")
    identity = "json_extract(CASE WHEN json_valid(summary) THEN summary ELSE '{}' END, '$.factory_run_id')"
    if report_index is None:
        linked = conn.execute(select + f"AND {identity} = ? ORDER BY id", (run_id,)).fetchall()
    else:
        ids = report_index.get(run_id, [])
        linked = conn.execute(select + "AND id IN (" + ",".join("?" for _ in ids) + ") ORDER BY id", ids).fetchall() if ids else []
    report_link_mode = "factory_run_id"
    if not linked:
        # Never borrow another run's reports. Legacy unbound rows are tentative,
        # excluded from verified admission totals.
        linked = conn.execute(
            select + "AND julianday(created_at) BETWEEN julianday(?) - 0.0025 AND julianday(?) + 0.0025 "
            + f"AND {identity} IS NULL ORDER BY id",
            (run["started_at"], run["completed_at"] or run["started_at"]),
        ).fetchall()
        report_link_mode = "time_window_unverified" if linked else "none"

    sharpes: list[float] = []
    drawdowns: list[float] = []
    wins: list[float] = []
    passed = 0
    legacy_passed = 0
    reason_counter: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    stage_counts: Counter[str] = Counter()
    for report in linked:
        legacy_passed += int(bool(report["passed"]))
        gate = _json(report["quality_gate"], {}) or {}
        metrics = _json(report["backtest_metrics"], {}) or {}
        summary = _json(report["summary"], {}) or {}
        stage = gate.get("admission_stage") or summary.get("admission_stage")
        stage_counts[str(stage or "unknown")] += 1
        incubation = (gate.get("admission_evaluations") or {}).get("incubation") or {}
        admitted = (report_link_mode == "factory_run_id" and stage in ("incubation", "live")
                    and incubation.get("passed") is True and not incubation.get("reasons")
                    and not gate.get("hard_fail_reasons") and not gate.get("diagnostic_only")
                    and not summary.get("diagnostic_only"))
        passed += int(admitted)
        for target, key in ((sharpes, "sharpe_ratio"), (drawdowns, "max_drawdown"), (wins, "win_rate")):
            value = _num(metrics.get(key))
            if value is not None:
                target.append(value)
        reasons = (gate.get("hard_fail_reasons") or incubation.get("reasons")
                   or gate.get("admission_block_reasons") or gate.get("reasons") or gate.get("reason_codes") or [])
        if isinstance(reasons, str):
            reasons = [reasons]
        for reason in reasons:
            reason_counter[_safe_text(reason, 120)] += 1
        samples.append(
            {
                "report_id": report["id"],
                "strategy_id": report["strategy_id"],
                "name": summary.get("name"),
                "factory_run_id": summary.get("factory_run_id"),
                "tested_object_hash": summary.get("tested_object_hash") or (gate.get("multiple_testing_registry") or {}).get("tested_object_hash"),
                "report_created_at": report["created_at"],
                "report_link_mode": report_link_mode,
                "run_link_verified": report_link_mode == "factory_run_id",
                "evidence": _candidate_evidence(summary, gate, metrics),
                "blockers": list(reasons),
                "passed": admitted,
                "legacy_passed": bool(report["passed"]),
                "admission_stage": stage,
                "validation_grade": summary.get("effective_validation_grade") or gate.get("validation_grade"),
                "admission_decision": summary.get("admission_decision") or gate.get("business_admission_decision"),
                "sharpe": _num(metrics.get("sharpe_ratio")),
                "max_drawdown": _num(metrics.get("max_drawdown")),
                "win_rate": _num(metrics.get("win_rate")),
            }
        )
    samples.sort(key=lambda item: (item.get("passed"), item.get("sharpe") is not None, item["sharpe"] if item.get("sharpe") is not None else -999), reverse=True)
    snapshot = _json(run["snapshot_summary"], {}) or {}
    return {
        "row_id": int(run["id"]),
        "run_id": run_id,
        "status": run["status"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "elapsed_seconds": _num(run["elapsed_seconds"]),
        "execution_mode": run["execution_mode"],
        "engine_version": run["engine_version"],
        "error": run["error"],
        "run_summary": _json(dict(run).get("summary"), {}) or {},
        "snapshot": {
            "date": snapshot.get("date"),
            "fear_greed": snapshot.get("fear_greed", snapshot.get("fear_greed_index")),
            "fg_level": snapshot.get("fg_level"),
            "completion_ratio": snapshot.get("completion_ratio"),
            "failure_reason_count": snapshot.get("failure_reason_count"),
            "incubating_count": snapshot.get("incubating_count"),
            "optional_unavailable_sources": snapshot.get("optional_unavailable_sources") or [],
        },
        "quality": {
            "review_version": REVIEW_VERSION,
            "report_link_mode": report_link_mode,
            "stage_counts": dict(stage_counts),
            "unique_strategy_count": len({str(item["strategy_id"]) for item in samples if item.get("strategy_id")}),
            "unique_tested_object_count": len({str(item["tested_object_hash"]) for item in samples if item.get("tested_object_hash")}),
            "oos_measured_report_count": sum(item["evidence"]["oos_measured"] for item in samples),
            "execution_hard_gate_passed_report_count": sum(item["evidence"]["execution_hard_gate_passed"] is True for item in samples),
            "goal_completion_verified": False,
            "candidates": samples,
            "report_count": len(linked),
            "legacy_passed_count": legacy_passed,
            "passed_count": passed,
            "failed_count": max(0, len(linked) - passed),
            "pass_rate": round(passed / len(linked), 4) if linked else None,
            "negative_sharpe_count": sum(value < 0 for value in sharpes),
            "avg_sharpe": round(sum(sharpes) / len(sharpes), 4) if sharpes else None,
            "worst_sharpe": round(min(sharpes), 4) if sharpes else None,
            "max_drawdown": round(max(drawdowns), 4) if drawdowns else None,
            "avg_win_rate": round(sum(wins) / len(wins), 4) if wins else None,
            "top_blockers": [{"reason": reason, "count": count} for reason, count in reason_counter.most_common(5)],
            "samples": samples[:5],
        },
    }


def _collect_new_entries(conn: sqlite3.Connection, state: dict[str, Any]) -> list[dict[str, Any]]:
    # Dispatches may finish out of insertion order. A high-water row id would
    # permanently skip an older still-running dispatch when a newer one ends.
    reviewed = set(state.get("reviewed_run_ids") or [])
    reviewed.update(str(entry["run_id"]) for entry in state.get("entries") or [] if entry.get("run_id"))
    params: tuple[Any, ...] = ()
    scope = ""
    if state.get("since"):
        scope = " AND julianday(started_at) >= julianday(?)"
        params = (state["since"],)
    rows = conn.execute(
        "SELECT id, run_id, status, started_at, completed_at, elapsed_seconds, execution_mode, engine_version, "
        "summary, snapshot_summary, error FROM strategy_factory_runs WHERE "
        "status IN ('success', 'success_observe_only', 'success_no_submission', 'success_no_strategy', "
        "'partial', 'partial_infra', 'partial_llm', 'skipped', 'failed', 'blocked') "
        "AND completed_at IS NOT NULL" + scope + " ORDER BY id", params,
    ).fetchall()
    rows = [row for row in rows if str(row["run_id"]) not in reviewed]
    if not rows:
        return []
    wanted = {str(row["run_id"]) for row in rows}
    report_index: dict[str, list[int]] = {}
    for report in conn.execute(
        "SELECT id, json_extract(CASE WHEN json_valid(summary) THEN summary ELSE '{}' END, '$.factory_run_id') "
        "FROM strategy_quality_reports WHERE report_type='submission'"
    ):
        if report[1] in wanted:
            report_index.setdefault(report[1], []).append(report[0])
    entries = []
    for row in rows:
        entries.append(_review_run(conn, row, report_index=report_index))
        reviewed.add(str(row["run_id"]))
        state["last_run_rowid"] = max(int(state.get("last_run_rowid") or 0), int(row["id"]))
    state["reviewed_run_ids"] = sorted(reviewed)
    return entries


def _render_day(entries: list[dict[str, Any]], *, day: str, inventory: dict[str, Any], health: dict[str, Any], cycles: dict[str, Any], session: dict[str, Any] | None = None) -> str:
    day_entries = [entry for entry in entries if str(entry.get("started_at") or "")[:10] == day]
    total_reports = sum(int((entry.get("quality") or {}).get("report_count") or 0) for entry in day_entries)
    total_passed = sum(int((entry.get("quality") or {}).get("passed_count") or 0) for entry in day_entries)
    overall_rate = total_passed / total_reports if total_reports else None
    oos_reports = sum(int((entry.get("quality") or {}).get("oos_measured_report_count") or 0) for entry in day_entries)
    lines = [
        f"# 四工厂策略质量审查记录 — {day}",
        "",
        f"> 由 `monitor_factory_quality.py` 持续生成；最后更新：{_now().strftime('%Y-%m-%d %H:%M:%S %z')}。",
        "> 数据源：四工厂 supervisor heartbeat、`strategy_factory_runs`、`strategy_quality_reports`。观察器只读数据库，不触发工厂运行。",
        f"> 观察会话：`{(session or {}).get('session_id') or 'legacy'}`；轮次开始时间下界：`{(session or {}).get('since') or '全部历史'}`。",
        "",
        "## 当前运行健康",
        "",
        f"- supervisor：**{health.get('status', 'unknown')}**；heartbeat：`{_fmt_dt(health.get('heartbeat_at'))}`。",
        f"- 工厂状态：`{json.dumps(health.get('factories') or {}, ensure_ascii=False)}`；重启次数：`{json.dumps(health.get('restart_count') or {}, ensure_ascii=False)}`。",
        f"- 当前策略库存：`{json.dumps(inventory, ensure_ascii=False)}`。",
        f"- 最近侧链摘要：`{json.dumps(cycles, ensure_ascii=False)}`。",
        "",
        "## 当日汇总",
        "",
        f"- 完成策略工厂轮次：**{len(day_entries)}**",
        f"- 质量报告：**{total_reports}**；孵化评估通过：**{total_passed}**；准入率：**{overall_rate:.1%}**" if overall_rate is not None else "- 质量报告：**0**（尚未出现可关联报告）",
        "- 计数口径 v3：仅精确关联 factory_run_id、admission_stage=incubation/live 且 incubation 评估明确通过、无硬拒绝的报告计为孵化评估通过；不使用历史 passed 标记。",
        "- 以上仅为孵化评估通过报告数，不代表实际获配孵化预算/持久化准入，不等于独立高质量策略数或可实盘交易；持续高质量目标仍需去重、收益/风险、样本外与执行证据独立审查。",
        f"- 带候选冻结契约的样本外回放报告：**{oos_reports}**；全部提交候选及最新前向/执行证据见 [candidates.json](candidates.json)，每轮持久化原始产物见 `runs/`。上游压缩省略的候选仍属于记录缺口。",
        "",
        "## 每轮记录",
        "",
        "| # | run_id | 完成时间 | 运行 | 质量报告 | 通过率 | 负 Sharpe | 平均 Sharpe | 最大回撤 | 审查结论 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for index, entry in enumerate(day_entries, start=1):
        quality = entry.get("quality") or {}
        rate = quality.get("pass_rate")
        conclusion = "通过样本可继续观察" if rate is not None and rate >= 0.5 and not quality.get("negative_sharpe_count") else "保留观察：存在 gate/风险证据"
        rate_text = f"{rate:.1%}" if rate is not None else "—"
        if entry.get("status") != "success":
            conclusion = f"运行状态 {entry.get('status')}，需复核"
        lines.append(
            f"| {index} | `{entry.get('run_id')}` | {_fmt_dt(entry.get('completed_at'))} | "
            f"{entry.get('status')} / {float(entry.get('elapsed_seconds') or 0):.1f}s | "
            f"{quality.get('passed_count', 0)}/{quality.get('report_count', 0)} | "
            f"{rate_text} | {quality.get('negative_sharpe_count', 0)} | "
            f"{quality.get('avg_sharpe') if quality.get('avg_sharpe') is not None else '—'} | "
            f"{quality.get('max_drawdown') if quality.get('max_drawdown') is not None else '—'} | {conclusion} |"
        )
    lines.extend(["", "## 轮次审查明细", ""])
    for entry in day_entries:
        quality = entry.get("quality") or {}
        lines.append(f"### `{entry.get('run_id')}`")
        lines.append("")
        lines.append(f"- 工厂状态：`{entry.get('status')}`；execution mode：`{entry.get('execution_mode')}`；完成：`{_fmt_dt(entry.get('completed_at'))}`。")
        lines.append(f"- 市场快照：`{json.dumps(entry.get('snapshot') or {}, ensure_ascii=False)}`。")
        blockers = quality.get("top_blockers") or []
        lines.append(f"- 质量：报告 {quality.get('report_count', 0)}；通过 {quality.get('passed_count', 0)}；负 Sharpe {quality.get('negative_sharpe_count', 0)}；平均 Sharpe {quality.get('avg_sharpe')}; 最大回撤 {quality.get('max_drawdown')}；关联方式 `{quality.get('report_link_mode', 'unknown')}`。")
        lines.append(f"- 主要阻塞：{', '.join(f"{item.get('reason')} ({item.get('count')})" for item in blockers) if blockers else '未提取到显式阻塞原因'}。")
        samples = quality.get("samples") or []
        if samples:
            lines.append("- 代表样本：")
            for sample in samples[:3]:
                lines.append(
                    f"  - `{sample.get('strategy_id')}` {sample.get('name') or ''}："
                    f"gate={'pass' if sample.get('passed') else 'fail'}，grade={sample.get('validation_grade') or '—'}，"
                    f"Sharpe={sample.get('sharpe') if sample.get('sharpe') is not None else '—'}，"
                    f"回撤={sample.get('max_drawdown') if sample.get('max_drawdown') is not None else '—'}。"
                )
        lines.append("")
    lines.extend(["## 口径与边界", "", "- 该文档记录观察到的运行和质量证据，不把 `success` 自动解释为策略可交易。", "- 监督器的四个运行时为 Market Event Ingest、Factor Mining、Strategy Factory、Incubation；SignalTracker 作为单独 sidecar 状态保留在运行环境中。", "- 质量报告缺失或无法按 `factory_run_id` 关联时会保留时间窗口回退结果，并在 JSON 状态中体现报告数量。", ""])
    return "\n".join(lines)


def _render_reports(state: dict[str, Any], conn: sqlite3.Connection) -> None:
    entries = list(state.get("entries") or [])
    # Backfill the linkage marker for entries collected before this field was
    # introduced.  The marker makes the report auditable without copying the
    # full quality payload into the Markdown document.
    for entry in entries:
        quality = entry.setdefault("quality", {})
        if quality.get("report_link_mode"):
            continue
        run_id = str(entry.get("run_id") or "")
        linked = conn.execute(
            "SELECT 1 FROM strategy_quality_reports WHERE report_type='submission' "
            "AND summary LIKE ? LIMIT 1",
            (f'%\"factory_run_id\": \"{run_id}\"%',),
        ).fetchone()
        quality["report_link_mode"] = "factory_run_id" if linked else "time_window"
    inventory = _query_inventory(conn)
    health = _supervisor_health()
    cycles = _log_cycle_snapshot()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    days = sorted({str(entry.get("started_at") or "")[:10] for entry in entries if entry.get("started_at")})
    if not days:
        days = [_now().strftime("%Y-%m-%d")]
    for day in days:
        (REPORT_DIR / f"{day}.md").write_text(
            _render_day(entries, day=day, inventory=inventory, health=health, cycles=cycles, session=state), encoding="utf-8"
        )
    current = REPORT_DIR / "CURRENT.md"
    current.write_text(_render_day(entries, day=days[-1], inventory=inventory, health=health, cycles=cycles, session=state), encoding="utf-8")
    ledger = _current_candidate_ledger(conn, state)
    _write_json(REPORT_DIR / "candidates.json", ledger)
    state["candidate_ledger_path"] = str(REPORT_DIR / "candidates.json")
    state["current_candidate_counts"] = {key: ledger[key] for key in (
        "unique_strategy_count", "unique_tested_object_count", "execution_hard_gate_passed_count",
    )}
    state["last_rendered_at"] = _now().isoformat()
    state["latest_inventory"] = inventory
    state["latest_supervisor_health"] = health
    state["latest_factory_cycle_snapshot"] = cycles


def _acquire_pid() -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PID_PATH.exists():
        try:
            existing = int(PID_PATH.read_text().strip())
        except (OSError, ValueError):
            existing = 0
        if existing and _alive(existing) and existing != os.getpid():
            raise SystemExit(f"quality observer already running: pid={existing}")
        PID_PATH.unlink(missing_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _release_pid() -> None:
    try:
        if PID_PATH.read_text().strip() == str(os.getpid()):
            PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def run(*, once: bool, interval: int, since: str | None = None, session_id: str | None = None) -> int:
    _acquire_pid()
    try:
        state = _read_state(since=since, session_id=session_id)
        if state.get("review_version") != REVIEW_VERSION:
            # Rebuild from authoritative rows, including terminal statuses the
            # old observer skipped. Do not mix old advisory counts with v3.
            state["entries"] = []
            state["last_run_rowid"] = 0
            state["reviewed_run_ids"] = []
            state["review_version"] = REVIEW_VERSION
        while True:
            if not DB_PATH.exists():
                state["last_error"] = f"database not found: {DB_PATH}"
            else:
                conn = None
                try:
                    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA query_only = ON")
                    working = {**state, "entries": list(state.get("entries") or []),
                               "reviewed_run_ids": list(state.get("reviewed_run_ids") or [])}
                    new_entries = _collect_new_entries(conn, working)
                    if new_entries:
                        working["entries"].extend(new_entries)
                        for entry in new_entries:
                            filename = re.sub(r"[^A-Za-z0-9_.-]", "_", entry["run_id"]) + ".json"
                            _write_json(REPORT_DIR / "runs" / filename, _archive_run_evidence(conn, entry))
                    _render_reports(working, conn)
                    working.pop("last_error", None)
                    state = working
                except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                    # Retain the last successful cursor, so the next poll can
                    # retry both a transient DB failure and failed archiving.
                    state["last_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"quality observer poll failed: {state['last_error']}", file=sys.stderr, flush=True)
                finally:
                    if conn is not None:
                        conn.close()
            _write_json(STATE_PATH, state)
            if once:
                return 1 if state.get("last_error") else 0
            time.sleep(max(5, interval))
    finally:
        _release_pid()


def _daemonize(log_path: Path) -> None:
    """Detach on macOS/Linux so the observer survives its launching shell."""
    child_pid = os.fork()
    if child_pid > 0:
        print(f"quality observer started detached: pid={child_pid} log={log_path}", flush=True)
        raise SystemExit(0)
    os.setsid()
    os.chdir(ROOT)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    if fd > 2:
        os.close(fd)


def main() -> int:
    global DB_PATH, LOG_DIR, STATE_PATH, PID_PATH, REPORT_DIR
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="process current completed runs and exit")
    parser.add_argument("--daemon", action="store_true", help="detach and keep polling until stopped")
    parser.add_argument("--log", default=str(RUNTIME_HOME / "logs" / "four_factory_quality_observer.log"))
    parser.add_argument("--interval", type=int, default=60, help="poll interval in seconds")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Existing database to observe read-only")
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR, help="Actual four-runtime supervisor log directory")
    parser.add_argument("--state", type=Path, default=STATE_PATH, help="Observer state; use a new path for a new session")
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR, help="Markdown and candidate ledger output directory")
    parser.add_argument("--session-id", help="Identifier for this continuous quality observation")
    parser.add_argument("--since", help="Only runs started at or after this ISO timestamp with explicit offset")
    args = parser.parse_args()
    if args.since:
        try:
            parsed_since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
            if parsed_since.tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError:
            parser.error("--since requires an ISO timestamp with an explicit UTC offset")
    DB_PATH, LOG_DIR, STATE_PATH, REPORT_DIR = (item.expanduser().resolve() for item in (args.db, args.log_dir, args.state, args.report_dir))
    PID_PATH = STATE_PATH.with_suffix(".pid") if args.state != parser.get_default("state") else PID_PATH
    if args.daemon:
        _daemonize(Path(args.log).expanduser())
    return run(once=bool(args.once), interval=max(5, int(args.interval)), since=args.since, session_id=args.session_id)


if __name__ == "__main__":
    raise SystemExit(main())
