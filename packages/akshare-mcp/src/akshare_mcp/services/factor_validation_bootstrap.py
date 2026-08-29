"""Batch bootstrap for local factor-candidate validation.

This service closes the local IC / RankIC / OOS validation loop for
``factor_candidate_memory`` records.  It deliberately uses only local market
data and the existing validation pipeline; external research evidence may be
attached to candidates, but never promotes a factor by itself.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re
from typing import Any

from .artifact_registry import get_artifact_async, register_artifact_async
from .factor_candidate_storage import (
    get_factor_candidate_record_async,
    list_factor_candidate_records_async,
    save_factor_candidate_record_async,
)
from .factor_validation_pipeline import validate_factor_candidate_pipeline


VALIDATION_ARTIFACT_STRATEGY = "quant_factor_candidate_validation"
VALIDATION_ARTIFACT_VERSION = "p1.v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    out = max(int(minimum), out)
    if maximum is not None:
        out = min(int(maximum), out)
    return out


def _normalize_codes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("|", ",").replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        code = str(item or "").strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _normalize_statuses(raw: Any) -> list[str]:
    if raw is None:
        return ["review"]
    if isinstance(raw, str):
        items = raw.replace("|", ",").replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    statuses = []
    for item in items:
        status = str(item or "").strip().lower()
        if status and status not in statuses:
            statuses.append(status)
    return statuses or ["review"]


def _normalize_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("|", ",").replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    out: list[str] = []
    for item in items:
        aid = str(item or "").strip()
        if aid and aid not in out:
            out.append(aid)
    return out


def _candidate_from_record(record: dict[str, Any]) -> dict[str, Any]:
    candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    return deepcopy(candidate)


def _slug(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(value or "").strip())
    return token[:96] or hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:16]


def _validation_artifact_id(source_artifact_id: str, *, horizon_days: int) -> str:
    return f"factor_validation_bootstrap_{_slug(source_artifact_id)}_h{int(horizon_days)}"


def _dedupe(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        token = str(value or "").strip()
        if token and token not in out:
            out.append(token)
    return out


async def _load_candidate_records(
    *,
    status: Any,
    max_candidates: int,
    candidate_ids: Any = None,
    family: str | None = None,
) -> list[dict[str, Any]]:
    ids = _normalize_ids(candidate_ids)
    if ids:
        records: list[dict[str, Any]] = []
        for artifact_id in ids[:max_candidates]:
            record = await get_factor_candidate_record_async(artifact_id)
            if record:
                records.append(record)
        return records

    records_by_id: dict[str, dict[str, Any]] = {}
    for one_status in _normalize_statuses(status):
        rows = await list_factor_candidate_records_async(
            limit=max_candidates,
            status=one_status,
            family=family,
        )
        for row in rows:
            artifact_id = str(row.get("artifact_id") or "").strip()
            if artifact_id and artifact_id not in records_by_id:
                records_by_id[artifact_id] = row
            if len(records_by_id) >= max_candidates:
                break
        if len(records_by_id) >= max_candidates:
            break
    return list(records_by_id.values())[:max_candidates]


async def _build_stock_universe(
    db,
    *,
    required_bars: int,
    universe_limit: int | None,
    codes: Any = None,
) -> tuple[list[str], dict[str, Any]]:
    explicit_codes = _normalize_codes(codes)
    limit = _as_int(universe_limit, 100000, minimum=1) if universe_limit is not None else 100000
    min_bars = max(1, int(required_bars or 1))

    async with db.acquire() as conn:
        if explicit_codes:
            rows = await conn.fetch(
                """
                SELECT code, COUNT(*) AS bars
                FROM kline_1d
                WHERE code IN ($1)
                GROUP BY code
                HAVING COUNT(*) >= $2
                ORDER BY code
                LIMIT $3
                """,
                explicit_codes,
                min_bars,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT k.code AS code, COUNT(*) AS bars
                FROM kline_1d k
                LEFT JOIN stocks s ON s.stock_code = k.code
                WHERE k.code IS NOT NULL AND k.code != ''
                GROUP BY k.code
                HAVING COUNT(*) >= $1
                ORDER BY COALESCE(s.stock_code, k.code)
                LIMIT $2
                """,
                min_bars,
                limit,
            )
    codes_out = [str(row.get("code") or "").strip() for row in rows if str(row.get("code") or "").strip()]
    bars = [int(row.get("bars") or 0) for row in rows]
    coverage = {
        "requested_codes": explicit_codes,
        "required_bars": min_bars,
        "universe_limit": limit if universe_limit is not None else None,
        "universe_count": len(codes_out),
        "min_bars": min(bars) if bars else 0,
        "max_bars": max(bars) if bars else 0,
    }
    return codes_out, coverage


def _promotion_block_reasons(validation: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not bool(validation.get("success")):
        reasons.append(str(validation.get("stage") or "validation_failed"))
        return reasons

    rating = validation.get("rating") if isinstance(validation.get("rating"), dict) else {}
    governance = rating.get("governance") if isinstance(rating.get("governance"), dict) else {}
    if str(rating.get("recommendation") or "").strip().lower() != "promote":
        reasons.append("rating_not_promote")
    if bool(governance.get("admission_blocked")):
        reasons.append("governance_admission_blocked")
    for reason in list(governance.get("admission_block_reasons") or []):
        if str(reason or "").strip():
            reasons.append(str(reason).strip())

    required_sections = {
        "oos_validation": validation.get("oos_validation"),
        "robustness": validation.get("robustness"),
        "lookahead_audit": validation.get("lookahead_audit"),
        "multiple_testing": validation.get("multiple_testing"),
    }
    for name, payload in required_sections.items():
        if not isinstance(payload, dict) or not bool(payload.get("available")):
            reasons.append(f"{name}_unavailable")

    lookahead = validation.get("lookahead_audit") if isinstance(validation.get("lookahead_audit"), dict) else {}
    multiple = validation.get("multiple_testing") if isinstance(validation.get("multiple_testing"), dict) else {}
    if str(lookahead.get("risk_level") or "").strip().lower() == "high":
        reasons.append("lookahead_risk_high")
    if str(multiple.get("risk_level") or "").strip().lower() == "high":
        reasons.append("multiple_testing_risk_high")

    # P1-3.6 fix: governance online_offline:inconsistent 接入 promotion hard gate
    # 历史问题(诊断报告 §3.6):backtest slippage=0bps vs execution 5bps,因子在 8bps gap 下表现差,
    # governance_check_workflow 检测到但只 emit warning,promotion 仍通过,导致 cohort 全 D 级
    # 修复:online_offline 状态为 inconsistent 时强制阻塞 promotion
    consistency = (
        validation.get("online_offline_consistency")
        if isinstance(validation.get("online_offline_consistency"), dict)
        else (validation.get("governance") or {}).get("online_offline_consistency")
        if isinstance((validation.get("governance") or {}).get("online_offline_consistency"), dict)
        else {}
    )
    if isinstance(consistency, dict):
        consistency_status = str(consistency.get("consistency_status") or "").strip().lower()
        if consistency_status == "inconsistent":
            reasons.append("online_offline_inconsistent")
        # 即使没显式状态,直接看 issues 列表
    governance_issues = validation.get("governance_issues") or []
    if isinstance(governance_issues, list) and any(
        "online_offline:inconsistent" in str(i) for i in governance_issues
    ):
        if "online_offline_inconsistent" not in reasons:
            reasons.append("online_offline_inconsistent")

    warnings = [str(item or "").strip().lower() for item in list(validation.get("warnings") or [])]
    high_risk_warning_tokens = (
        "lookahead_audit_failed",
        "multiple_testing_failed",
        "oos_validation_unavailable",
        "robustness_unavailable",
        "lookahead_audit_unavailable",
        "multiple_testing_unavailable",
    )
    for token in high_risk_warning_tokens:
        if any(token in warning for warning in warnings):
            reasons.append(token)
    return _dedupe(reasons)


def _can_promote(validation: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = _promotion_block_reasons(validation)
    return not reasons, reasons


def _build_validation_payload(
    *,
    artifact_id: str,
    source_record: dict[str, Any],
    candidate: dict[str, Any],
    validation: dict[str, Any],
    codes: list[str],
    params: dict[str, Any],
    run_id: str,
    promotion_block_reasons: list[str],
) -> dict[str, Any]:
    rating = dict(validation.get("rating") or {})
    governance = dict(rating.get("governance") or {})
    source_artifact_id = str(source_record.get("artifact_id") or "").strip()
    source_external_evidence = [
        deepcopy(item)
        for item in list(source_record.get("external_evidence") or [])
        if isinstance(item, dict)
    ]
    payload = {
        "artifact_id": artifact_id,
        "action": "factor_validation_bootstrap",
        "codes": codes,
        "candidate_resolution": {
            "resolved_from": "factor_candidate_memory",
            "artifact_id": source_artifact_id,
            "candidate_index": None,
        },
        "candidate": validation.get("compiled", {}).get("candidate") or candidate,
        "compiled": validation.get("compiled") or {},
        "metrics": validation.get("metrics") or {},
        "coverage": validation.get("coverage") or {},
        "latest_snapshot": validation.get("latest_snapshot") or {},
        "cross_section_dates": validation.get("cross_section_dates") or [],
        "lookahead_audit": validation.get("lookahead_audit") or {},
        "multiple_testing": validation.get("multiple_testing") or {},
        "oos_validation": validation.get("oos_validation") or {},
        "robustness": validation.get("robustness") or {},
        "similarity": validation.get("similarity") or {},
        "turnover": validation.get("turnover") or {},
        "cost_capacity": validation.get("cost_capacity") or {},
        "rating": rating,
        "governance": governance,
        "external_evidence": source_external_evidence,
        "validation_report": validation.get("validation_report") or {},
        "factor_validation_report": validation.get("factor_validation_report") or {},
        "warnings": validation.get("warnings") or [],
        "params": dict(params),
        "stage": validation.get("stage", "validated"),
        "registry_stage": str(governance.get("registry_stage") or "validated"),
        "lineage": {
            "source_generation_artifact_id": source_artifact_id,
            "source_validation_artifact_id": artifact_id,
            "memory_record_id": source_artifact_id,
            "resolved_from": "factor_candidate_memory",
            "candidate_index": None,
            "external_evidence_ids": [
                str(item.get("evidence_id") or item.get("artifact_id") or "").strip()
                for item in source_external_evidence
                if str(item.get("evidence_id") or item.get("artifact_id") or "").strip()
            ],
        },
        "bootstrap": {
            "run_id": run_id,
            "factor_key": (validation.get("persisted_outputs") or {}).get("factor_key"),
            "promotion_block_reasons": promotion_block_reasons,
        },
        "persisted_outputs": validation.get("persisted_outputs") or {},
        "degraded": bool(validation.get("warnings") or promotion_block_reasons),
    }
    if not validation.get("success"):
        payload["error"] = validation.get("error")
    return payload


async def _register_validation_artifact(payload: dict[str, Any]) -> None:
    await register_artifact_async(
        {
            "artifact_id": payload["artifact_id"],
            "strategy": VALIDATION_ARTIFACT_STRATEGY,
            "strategy_version": VALIDATION_ARTIFACT_VERSION,
            "code": ",".join(list(payload.get("codes") or [])[:5]),
            "payload": payload,
            "created_at": _now_iso(),
        }
    )


async def _update_candidate_record(
    source_record: dict[str, Any],
    *,
    validation_artifact_id: str,
    validation: dict[str, Any],
    status: str,
    promotion_block_reasons: list[str],
    source_action: str,
) -> dict[str, Any]:
    artifact_id = str(source_record.get("artifact_id") or "").strip()
    if not artifact_id:
        return {}
    updated = deepcopy(source_record)
    updated["status"] = status
    updated["rating"] = deepcopy(validation.get("rating") or updated.get("rating") or {})
    updated["metrics"] = deepcopy(validation.get("metrics") or updated.get("metrics") or {})
    updated["source_action"] = source_action
    updated["source_validation_artifact_id"] = validation_artifact_id
    updated["latest_validation"] = {
        "artifact_id": validation_artifact_id,
        "stage": validation.get("stage"),
        "success": bool(validation.get("success")),
        "rating": deepcopy(validation.get("rating") or {}),
        "metrics": deepcopy(validation.get("metrics") or {}),
        "persisted_outputs": deepcopy(validation.get("persisted_outputs") or {}),
        "promotion_block_reasons": list(promotion_block_reasons),
        "updated_at": _now_iso(),
    }
    if promotion_block_reasons:
        updated["fail_reason"] = ";".join(promotion_block_reasons)
        updated["promotion_block_reasons"] = list(promotion_block_reasons)
    else:
        updated.pop("fail_reason", None)
        updated["promotion_block_reasons"] = []

    tags = list(updated.get("tags") or [])
    tags.extend(["local_validation", status])
    if status == "success":
        tags.extend(["validated", "active_pool_candidate"])
        tags = [tag for tag in tags if str(tag).strip() != "requires_validation"]
    elif status == "fail":
        tags.append("validation_failed")
    else:
        tags.append("requires_validation")
    updated["tags"] = _dedupe(tags)

    flags = dict(updated.get("memory_flags") or {})
    flags["validated_locally"] = bool(validation.get("success"))
    flags["validation_artifact_id"] = validation_artifact_id
    flags["active_pool_eligible"] = status == "success"
    flags["active_pool_block_reasons"] = [] if status == "success" else list(promotion_block_reasons or ["requires_local_validation"])
    flags["requires_validation"] = status != "success"
    updated["memory_flags"] = flags
    updated["updated_at"] = _now_iso()
    return await save_factor_candidate_record_async(updated, artifact_id=artifact_id)


def _candidate_plan(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plan = []
    for record in records:
        candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
        plan.append(
            {
                "artifact_id": record.get("artifact_id"),
                "status": record.get("status"),
                "name": candidate.get("name"),
                "family": candidate.get("family") or record.get("family"),
                "expression_dsl": candidate.get("expression_dsl"),
            }
        )
    return plan


async def run_factor_validation_bootstrap(
    db,
    *,
    status: Any = "review",
    max_candidates: Any = 50,
    horizon_days: Any = 10,
    max_dates: Any = 60,
    lookback_bars: Any = 220,
    min_cross_section: Any = 100,
    promote: Any = True,
    resume: Any = True,
    dry_run: Any = False,
    universe_limit: Any = None,
    codes: Any = None,
    stock_codes: Any = None,
    candidate_ids: Any = None,
    family: str | None = None,
    persist_outputs: Any = True,
) -> dict[str, Any]:
    """Run local validation for factor-candidate memory records."""

    run_id = f"factor_validation_bootstrap_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{hashlib.sha1(str(datetime.now().timestamp()).encode()).hexdigest()[:8]}"
    resolved_max_candidates = _as_int(max_candidates, 50, minimum=1, maximum=500)
    resolved_horizon_days = _as_int(horizon_days, 10, minimum=1, maximum=60)
    resolved_max_dates = _as_int(max_dates, 60, minimum=5, maximum=252)
    resolved_lookback_bars = _as_int(lookback_bars, 220, minimum=80, maximum=800)
    resolved_min_cross_section = _as_int(min_cross_section, 100, minimum=3, maximum=10000)
    resolved_promote = _as_bool(promote, True)
    resolved_resume = _as_bool(resume, True)
    resolved_dry_run = _as_bool(dry_run, False)
    resolved_persist_outputs = _as_bool(persist_outputs, True)
    universe_codes, universe_coverage = await _build_stock_universe(
        db,
        required_bars=resolved_lookback_bars + resolved_horizon_days + 40,
        universe_limit=universe_limit,
        codes=stock_codes or codes,
    )
    records = await _load_candidate_records(
        status=status,
        max_candidates=resolved_max_candidates,
        candidate_ids=candidate_ids,
        family=family,
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "status": "planned" if resolved_dry_run else "running",
        "dry_run": resolved_dry_run,
        "resume": resolved_resume,
        "promote": resolved_promote,
        "params": {
            "status": _normalize_statuses(status),
            "max_candidates": resolved_max_candidates,
            "horizon_days": resolved_horizon_days,
            "max_dates": resolved_max_dates,
            "lookback_bars": resolved_lookback_bars,
            "min_cross_section": resolved_min_cross_section,
            "universe_limit": universe_limit,
            "persist_outputs": resolved_persist_outputs,
        },
        "universe": universe_coverage,
        "candidate_count": len(records),
        "candidate_plan": _candidate_plan(records),
        "processed": 0,
        "saved": 0,
        "skipped": 0,
        "failed": 0,
        "validation_artifacts": 0,
        "promoted": 0,
        "factor_value_rows": 0,
        "ic_history_rows": 0,
        "failure_reasons": {},
        "promotion_block_reasons": {},
        "candidates": [],
        "errors": [],
    }

    if resolved_dry_run:
        result["status"] = "planned"
        return result

    if not records:
        result["status"] = "completed"
        result["message"] = "no factor_candidate_memory records matched"
        return result
    if len(universe_codes) < resolved_min_cross_section:
        result["status"] = "failed"
        result["failed"] = len(records)
        reason = "universe_below_min_cross_section"
        result["failure_reasons"] = {reason: len(records)}
        result["errors"].append(
            f"{reason}: universe={len(universe_codes)} min_cross_section={resolved_min_cross_section}"
        )
        return result

    failure_counter: Counter[str] = Counter()
    promotion_counter: Counter[str] = Counter()
    for record in records:
        source_artifact_id = str(record.get("artifact_id") or "").strip()
        candidate = _candidate_from_record(record)
        item = {
            "artifact_id": source_artifact_id,
            "name": candidate.get("name"),
            "status_before": record.get("status"),
            "status_after": record.get("status"),
            "validation_artifact_id": None,
            "success": False,
            "promoted": False,
            "factor_value_rows": 0,
            "ic_history_rows": 0,
            "promotion_block_reasons": [],
            "error": None,
        }
        result["processed"] += 1
        if not source_artifact_id or not candidate:
            result["skipped"] += 1
            item["error"] = "missing_candidate"
            failure_counter["missing_candidate"] += 1
            result["candidates"].append(item)
            continue

        artifact_id = _validation_artifact_id(source_artifact_id, horizon_days=resolved_horizon_days)
        item["validation_artifact_id"] = artifact_id
        factor_key = f"factor_candidate:{source_artifact_id}"
        try:
            existing = await get_artifact_async(artifact_id) if resolved_resume else None
            if existing and isinstance((existing.get("payload") or existing), dict):
                existing_payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else existing
                persisted = existing_payload.get("persisted_outputs") if isinstance(existing_payload.get("persisted_outputs"), dict) else {}
                if persisted.get("factor_value_rows") or persisted.get("ic_history_rows"):
                    result["skipped"] += 1
                    item["success"] = True
                    item["status_after"] = record.get("status")
                    item["factor_value_rows"] = int(persisted.get("factor_value_rows") or 0)
                    item["ic_history_rows"] = int(persisted.get("ic_history_rows") or 0)
                    item["promotion_block_reasons"] = list(
                        ((existing_payload.get("bootstrap") or {}).get("promotion_block_reasons") or [])
                    )
                    result["candidates"].append(item)
                    continue

            validation = await validate_factor_candidate_pipeline(
                db,
                candidate,
                codes=universe_codes,
                lookback_bars=resolved_lookback_bars,
                horizon_days=resolved_horizon_days,
                max_dates=resolved_max_dates,
                min_cross_section=resolved_min_cross_section,
                persist_outputs=resolved_persist_outputs,
                factor_key=factor_key,
                persist_ic_history=True,
            )
            if isinstance(record.get("external_evidence"), list) and record.get("external_evidence"):
                validation["external_evidence"] = [
                    deepcopy(evidence)
                    for evidence in record.get("external_evidence")
                    if isinstance(evidence, dict)
                ]
            can_promote, block_reasons = _can_promote(validation)
            status_after = "review"
            if not validation.get("success"):
                status_after = "fail"
                block_reasons = block_reasons or [str(validation.get("stage") or "validation_failed")]
            elif resolved_promote and can_promote:
                status_after = "success"
            else:
                status_after = "review"
                if can_promote and not resolved_promote:
                    block_reasons = ["promotion_disabled"]

            payload = _build_validation_payload(
                artifact_id=artifact_id,
                source_record=record,
                candidate=candidate,
                validation=validation,
                codes=universe_codes,
                params=result["params"],
                run_id=run_id,
                promotion_block_reasons=block_reasons,
            )
            await _register_validation_artifact(payload)
            result["validation_artifacts"] += 1

            updated_record = await _update_candidate_record(
                record,
                validation_artifact_id=artifact_id,
                validation=validation,
                status=status_after,
                promotion_block_reasons=block_reasons,
                source_action="factor_validation_bootstrap",
            )
            item["status_after"] = updated_record.get("status") or status_after
            item["success"] = bool(validation.get("success"))
            item["promoted"] = item["status_after"] == "success"
            item["promotion_block_reasons"] = block_reasons
            persisted_outputs = validation.get("persisted_outputs") if isinstance(validation.get("persisted_outputs"), dict) else {}
            item["factor_value_rows"] = int(persisted_outputs.get("factor_value_rows") or 0)
            item["ic_history_rows"] = int(persisted_outputs.get("ic_history_rows") or 0)
            result["factor_value_rows"] += item["factor_value_rows"]
            result["ic_history_rows"] += item["ic_history_rows"]
            if item["success"]:
                result["saved"] += 1
            else:
                result["failed"] += 1
                failure_counter[str(validation.get("stage") or "validation_failed")] += 1
            if item["promoted"]:
                result["promoted"] += 1
            for reason in block_reasons:
                promotion_counter[reason] += 1
        except Exception as exc:
            result["failed"] += 1
            reason = type(exc).__name__
            failure_counter[reason] += 1
            item["error"] = f"{reason}: {exc}"
            result["errors"].append(f"{source_artifact_id}:{item['error']}")
        result["candidates"].append(item)

    result["failure_reasons"] = dict(failure_counter)
    result["promotion_block_reasons"] = dict(promotion_counter)
    result["status"] = "failed" if result["failed"] and not result["saved"] else "completed"
    result["message"] = (
        f"factor_validation_bootstrap processed={result['processed']} "
        f"saved={result['saved']} failed={result['failed']} "
        f"factor_values={result['factor_value_rows']} ic_rows={result['ic_history_rows']} "
        f"promoted={result['promoted']}"
    )
    return result
