"""Snapshot collection (local + agent) for shadow validation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from _shadow_common import (
    SECRET_PATTERNS,
    _redact,
    CommandSpec,
    DATA_GAP_STATUSES,
    DEFAULT_AGENT_BASE_URL,
    DEFAULT_BATCH_ID,
    DEFAULT_REPORT_ROOT,
    DEFAULT_SHADOW_DB,
    PARTIAL_STATUSES,
    REPO_ROOT,
    REQUIRED_TABLES,
    SAFETY_ENV,
    TOGGLE_KEYS,
    _decode_json,
    _fetch_one,
    _fetch_pairs,
    _string,
    append_pythonpath,
    build_shadow_env,
    check_shadow_schema,
    command_report_path,
    copy_shadow_database,
    json_default,
    manifest_path,
    read_json,
    resolve_shadow_db,
    resolve_source_db,
    shadow_report_dir,
    snapshot_path,
    toggle_phase_for_day,
    utc_now,
    write_json,
)

def _dimension_values(prediction: dict[str, Any], outcome: dict[str, Any], dimension: str) -> list[str]:
    aliases = {
        "family": ("family", "strategy_family", "strategy_type"),
        "stage": ("stage", "incubation_stage", "pipeline_stage"),
        "regime": ("regime", "market_regime", "profile_regime"),
        "event": ("event", "event_family", "event_type", "theme_event"),
        "factor": ("factor", "factor_family", "factor_name"),
    }
    sources = [
        _decode_json(prediction.get("metadata")),
        _decode_json(prediction.get("contract_json")),
        _decode_json(outcome.get("metadata")),
        _decode_json(outcome.get("outcome_json")),
    ]
    values: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in aliases.get(dimension, (dimension,)):
            value = source.get(key)
            if value in (None, "", []):
                continue
            if isinstance(value, (list, tuple, set)):
                values.extend(_string(item) for item in value if _string(item))
            else:
                values.append(_string(value))
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique or ["unknown"]


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _matrix_rows(
    predictions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    dimensions: tuple[str, ...] = ("family", "regime", "event", "factor"),
) -> list[dict[str, Any]]:
    predictions_by_id = {_string(item.get("prediction_id")): item for item in predictions}
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for outcome in outcomes:
        prediction = predictions_by_id.get(_string(outcome.get("prediction_id"))) or {}
        outcome_json = _decode_json(outcome.get("outcome_json"))
        score = _safe_float(outcome.get("trade_prediction_score"))
        for dimension in dimensions:
            for value in _dimension_values(prediction, outcome, dimension):
                key = (dimension, value)
                cell = cells.setdefault(
                    key,
                    {
                        "dimension": dimension,
                        "value": value,
                        "sample_n": 0,
                        "score_sum": 0.0,
                        "score_n": 0,
                        "direction_hit_n": 0,
                        "target_touch_n": 0,
                        "status_counts": {},
                        "data_quality_status_counts": {},
                    },
                )
                cell["sample_n"] += 1
                if score is not None:
                    cell["score_sum"] += score
                    cell["score_n"] += 1
                if outcome_json.get("direction_hit") is True:
                    cell["direction_hit_n"] += 1
                if outcome_json.get("target_touch") is True:
                    cell["target_touch_n"] += 1
                status = _string(outcome.get("score_status")) or "unknown"
                quality = _string(outcome.get("data_quality_status")) or "unknown"
                cell["status_counts"][status] = cell["status_counts"].get(status, 0) + 1
                cell["data_quality_status_counts"][quality] = cell["data_quality_status_counts"].get(quality, 0) + 1
    rows: list[dict[str, Any]] = []
    for cell in cells.values():
        sample_n = int(cell["sample_n"])
        score_n = int(cell["score_n"])
        rows.append(
            {
                "dimension": cell["dimension"],
                "value": cell["value"],
                "sample_n": sample_n,
                "score_avg": round(cell["score_sum"] / score_n, 6) if score_n else None,
                "direction_hit_rate": round(cell["direction_hit_n"] / sample_n, 6) if sample_n else None,
                "target_touch_rate": round(cell["target_touch_n"] / sample_n, 6) if sample_n else None,
                "status_counts": cell["status_counts"],
                "data_quality_status_counts": cell["data_quality_status_counts"],
            }
        )
    rows.sort(key=lambda row: (row["dimension"], -int(row["sample_n"]), row["value"]))
    return rows


def collect_local_snapshot(shadow_db: Path, *, previous_contract_hashes: dict[str, str] | None = None) -> dict[str, Any]:
    if not shadow_db.exists():
        return {
            "object": "trade_prediction.shadow.local_snapshot",
            "status": "degraded",
            "reason": "shadow_db_missing",
            "shadow_db": str(shadow_db),
        }
    with closing(sqlite3.connect(f"file:{shadow_db.as_posix()}?mode=ro", uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        schema = check_shadow_schema(shadow_db)
        if schema["status"] != "ok":
            return {
                "object": "trade_prediction.shadow.local_snapshot",
                "status": "degraded",
                "reason": "schema_missing",
                "schema": schema,
                "shadow_db": str(shadow_db),
            }
        prediction_rows = [dict(row) for row in conn.execute("SELECT * FROM strategy_trade_predictions").fetchall()]
        outcome_rows = [
            dict(row)
            for row in conn.execute("SELECT * FROM strategy_trade_prediction_outcomes").fetchall()
        ]
        prediction_count = len(prediction_rows)
        outcome_count = len(outcome_rows)
        evaluated_count = int(
            _fetch_one(
                conn,
                "SELECT COUNT(DISTINCT prediction_id) FROM strategy_trade_prediction_outcomes",
            )
            or 0
        )
        duplicate_rows = [
            {
                "prediction_id": row[0],
                "score_version": row[1],
                "count": int(row[2]),
            }
            for row in conn.execute(
                """
                SELECT prediction_id, score_version, COUNT(*) AS n
                FROM strategy_trade_prediction_outcomes
                GROUP BY prediction_id, score_version
                HAVING COUNT(*) > 1
                ORDER BY n DESC
                LIMIT 50
                """
            ).fetchall()
        ]
        current_hashes = {
            _string(row.get("prediction_id")): _string(row.get("contract_hash"))
            for row in prediction_rows
            if _string(row.get("prediction_id"))
        }
        hash_mutations: list[dict[str, str]] = []
        for prediction_id, old_hash in (previous_contract_hashes or {}).items():
            new_hash = current_hashes.get(prediction_id)
            if new_hash and old_hash and new_hash != old_hash:
                hash_mutations.append(
                    {
                        "prediction_id": prediction_id,
                        "previous_contract_hash": old_hash,
                        "current_contract_hash": new_hash,
                    }
                )
        intraday_count = int(_fetch_one(conn, "SELECT COUNT(*) FROM kline_intraday") or 0)
        intraday_quality = _fetch_pairs(
            conn,
            """
            SELECT COALESCE(data_quality_status, 'unknown'), COUNT(*)
            FROM kline_intraday
            GROUP BY COALESCE(data_quality_status, 'unknown')
            """,
        )
    status_counts = _counter(row.get("score_status") for row in outcome_rows)
    quality_counts = _counter(row.get("data_quality_status") for row in outcome_rows)
    score_version_counts = _counter(row.get("score_version") for row in outcome_rows)
    prediction_status_counts = _counter(row.get("prediction_status") for row in prediction_rows)
    partial_count = sum(count for status, count in status_counts.items() if status in PARTIAL_STATUSES)
    data_gap_count = sum(count for status, count in quality_counts.items() if status in DATA_GAP_STATUSES)
    v2_ok_count = sum(
        1
        for row in outcome_rows
        if _string(row.get("score_version")) == "trade_prediction_score_v2"
        and _string(row.get("score_status")) == "ok"
    )
    return {
        "object": "trade_prediction.shadow.local_snapshot",
        "status": "ready",
        "generated_at": utc_now(),
        "shadow_db": str(shadow_db),
        "schema": schema,
        "prediction_count": prediction_count,
        "outcome_count": outcome_count,
        "sample_n": evaluated_count,
        "pending_count": max(0, prediction_count - evaluated_count),
        "evaluated_count": evaluated_count,
        "partial_count": partial_count,
        "data_gap_count": data_gap_count,
        "v2_ok_count": v2_ok_count,
        "prediction_status_counts": prediction_status_counts,
        "score_status_counts": status_counts,
        "score_version_counts": score_version_counts,
        "data_quality_status_counts": quality_counts,
        "intraday_bar_count": intraday_count,
        "intraday_data_quality_status_counts": intraday_quality,
        "duplicate_outcomes": duplicate_rows,
        "duplicate_outcome_count": len(duplicate_rows),
        "contract_hash_mutations": hash_mutations,
        "contract_hash_mutation_count": len(hash_mutations),
        "contract_hashes": current_hashes,
        "matrix": {
            "rows": _matrix_rows(prediction_rows, outcome_rows),
            "row_count": len(_matrix_rows(prediction_rows, outcome_rows)),
        },
    }


def _counter(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        token = _string(value) or "unknown"
        counts[token] = counts.get(token, 0) + 1
    return counts


def collect_agent_snapshot(
    base_url: str | None,
    *,
    token: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    if not base_url:
        return {
            "object": "trade_prediction.shadow.agent_snapshot",
            "status": "degraded",
            "reason": "agent_base_url_not_configured",
            "routes": {},
        }
    routes = {
        "status": "/v1/desktop/trade-predictions/status",
        "outcomes": "/v1/desktop/trade-predictions/outcomes",
        "matrix": "/v1/desktop/trade-predictions/matrix",
    }
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload: dict[str, Any] = {
        "object": "trade_prediction.shadow.agent_snapshot",
        "status": "ready",
        "generated_at": utc_now(),
        "base_url": base_url,
        "routes": {},
    }
    for name, route in routes.items():
        url = parse.urljoin(base_url.rstrip("/") + "/", route.lstrip("/"))
        try:
            req = request.Request(url, headers=headers, method="GET")
            with request.urlopen(req, timeout=timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                payload["routes"][name] = {
                    "status": "ok",
                    "http_status": resp.status,
                    "data": json.loads(body),
                }
        except error.HTTPError as exc:
            payload["status"] = "degraded"
            payload["routes"][name] = {
                "status": "error",
                "http_status": exc.code,
                "error": _redact(str(exc)),
            }
        except Exception as exc:
            payload["status"] = "degraded"
            payload["routes"][name] = {
                "status": "error",
                "error": _redact(str(exc)),
            }
    return payload
