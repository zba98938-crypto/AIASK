"""Validation handler for quant_manager factor candidates."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from ...services import get_artifact_async, get_factor_research_memory_service, register_artifact_async
from .quant_mgr_artifact_common import _payload_from_artifact_row
from .quant_mgr_helpers import _as_code_list


async def _resolve_candidate_for_validation(
    kw: dict[str, Any],
    *,
    get_artifact_async_fn: Callable[[str], Any],
) -> dict:
    raw_candidate = kw.get("candidate")
    if isinstance(raw_candidate, str) and raw_candidate.strip():
        try:
            raw_candidate = json.loads(raw_candidate)
        except Exception:
            raise ValueError("candidate 必须是 dict 或可解析的 JSON 字符串")
    if isinstance(raw_candidate, dict):
        return {
            "candidate": raw_candidate,
            "resolved_from": "inline_candidate",
            "artifact_id": None,
            "candidate_index": None,
            "artifact_payload": None,
        }

    artifact_id = str(kw.get("artifact_id") or "").strip()
    if not artifact_id:
        raise ValueError("需要提供 candidate 或 artifact_id")

    artifact = await get_artifact_async_fn(artifact_id)
    if not artifact:
        hint = ""
        if artifact_id.startswith("factor_llm_"):
            hint = (
                " (LLM mining artifacts created with dry_run=true or persist_artifact=false "
                "are not persisted; validate an inline candidate or rerun mining with dry_run=false)"
            )
        raise ValueError(f"artifact not found: {artifact_id}{hint}")

    artifact_payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else artifact
    if isinstance((artifact_payload or {}).get("candidate"), dict):
        artifact_strategy = str(artifact.get("strategy") or artifact_payload.get("strategy") or "").strip().lower()
        return {
            "candidate": dict((artifact_payload or {}).get("candidate") or {}),
            "resolved_from": "factor_candidate_memory" if artifact_strategy == "factor_candidate_memory" else "single_candidate_artifact",
            "artifact_id": artifact_id,
            "candidate_index": None,
            "artifact_payload": artifact_payload,
        }

    candidates = list((artifact_payload or {}).get("candidates") or [])
    if not candidates:
        raise ValueError(f"artifact {artifact_id} does not contain candidates")

    try:
        candidate_index = int(kw.get("candidate_index", 0) or 0)
    except Exception:
        raise ValueError("candidate_index 必须是整数")
    if candidate_index < 0 or candidate_index >= len(candidates):
        raise ValueError(f"candidate_index 越界: {candidate_index}, 可选范围 0..{len(candidates) - 1}")

    return {
        "candidate": dict(candidates[candidate_index] or {}),
        "resolved_from": "artifact_candidate",
        "artifact_id": artifact_id,
        "candidate_index": candidate_index,
        "artifact_payload": artifact_payload,
    }


async def handle_validate_factor_candidate(
    *,
    kw: dict[str, Any],
    code: str | None,
    db: Any,
    ok: Callable[..., dict],
    fail: Callable[..., dict],
    get_artifact_async_fn: Callable[[str], Any] = get_artifact_async,
    validate_factor_candidate_pipeline_fn: Callable[..., Any],
    register_artifact_async_fn: Callable[..., Any] = register_artifact_async,
    memory_service_factory: Callable[[], Any] = get_factor_research_memory_service,
) -> dict:
    resolved = await _resolve_candidate_for_validation(kw, get_artifact_async_fn=get_artifact_async_fn)
    candidate = dict(resolved.get("candidate") or {})
    artifact_payload = resolved.get("artifact_payload") if isinstance(resolved.get("artifact_payload"), dict) else {}
    memory_service = memory_service_factory()

    codes = _as_code_list(kw.get("codes"))
    if not codes and code:
        codes = [code]
    if not codes:
        codes = _as_code_list(artifact_payload.get("codes"))
    if len(codes) < 4:
        return fail("validate_factor_candidate 至少需要 4 个 codes 进行横截面验证和 multiple-testing 审计")

    lookback_bars = max(120, min(int(kw.get("lookback_bars", 220) or 220), 500))
    horizon_days = max(3, min(int(kw.get("horizon_days", 10) or 10), 30))
    max_dates = max(20, min(int(kw.get("max_dates", 60) or 60), 120))
    persist_artifact = bool(kw.get("persist_artifact", True))
    write_memory = bool(kw.get("write_memory", True))

    validation = await validate_factor_candidate_pipeline_fn(
        db,
        candidate,
        codes=codes,
        lookback_bars=lookback_bars,
        horizon_days=horizon_days,
        max_dates=max_dates,
    )
    source_external_evidence = [
        dict(item)
        for item in list(artifact_payload.get("external_evidence") or [])
        if isinstance(item, dict)
    ]
    if source_external_evidence:
        validation["external_evidence"] = source_external_evidence
    source_chain = list(
        validation.get("source_chain")
        or ["services.factor_candidate_compiler", "services.factor_validation_pipeline"]
    )

    if not validation.get("success"):
        memory_record = None
        if write_memory:
            try:
                memory_record = await memory_service.record_validation_outcome(
                    candidate=candidate,
                    validation={
                        "rating": {"grade": "D", "recommendation": "reject"},
                        "metrics": {},
                        "external_evidence": source_external_evidence,
                    },
                    codes=codes,
                    source_artifact_id=resolved.get("artifact_id"),
                    source_action="validate_factor_candidate",
                    explicit_status="fail",
                    tags=[str(validation.get("stage") or "validate_failed")],
                )
                source_chain.append("services.factor_research_memory")
            except Exception:
                memory_record = None
        resp = fail(validation.get("error") or "candidate validation failed")
        resp["data"] = validation
        if memory_record is not None:
            resp["data"]["memory_record"] = memory_record
        return resp

    artifact_id = str(kw.get("output_artifact_id") or f"factor_validation_{int(time.time())}_{uuid4().hex[:8]}")
    memory_record = None
    if write_memory:
        try:
            memory_record = await memory_service.record_validation_outcome(
                candidate=validation.get("compiled", {}).get("candidate") or candidate,
                validation=validation,
                codes=codes,
                source_artifact_id=resolved.get("artifact_id"),
                source_action="validate_factor_candidate",
                tags=[
                    str((validation.get("rating") or {}).get("grade") or "").strip(),
                    str((validation.get("rating") or {}).get("recommendation") or "").strip(),
                ],
            )
            source_chain.append("services.factor_research_memory")
        except Exception as exc:
            validation.setdefault("warnings", []).append(f"memory_write_failed: {exc}")

    rating = dict(validation.get("rating") or {})
    governance = dict(rating.get("governance") or {})
    source_generation_artifact_id = resolved.get("artifact_id")
    payload = {
        "artifact_id": artifact_id,
        "action": "validate_factor_candidate",
        "codes": codes,
        "candidate_resolution": {
            "resolved_from": resolved.get("resolved_from"),
            "artifact_id": resolved.get("artifact_id"),
            "candidate_index": resolved.get("candidate_index"),
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
        "memory_record": memory_record,
        "warnings": validation.get("warnings") or [],
        "params": {
            "lookback_bars": lookback_bars,
            "horizon_days": horizon_days,
            "max_dates": max_dates,
        },
        "stage": validation.get("stage", "validated"),
        "registry_stage": str(governance.get("registry_stage") or "validated"),
        "lineage": {
            "source_generation_artifact_id": source_generation_artifact_id,
            "source_validation_artifact_id": artifact_id,
            "memory_record_id": (memory_record or {}).get("artifact_id") if isinstance(memory_record, dict) else None,
            "resolved_from": resolved.get("resolved_from"),
            "candidate_index": resolved.get("candidate_index"),
            "external_evidence_ids": [
                str(item.get("evidence_id") or item.get("artifact_id") or "").strip()
                for item in source_external_evidence
                if str(item.get("evidence_id") or item.get("artifact_id") or "").strip()
            ],
        },
        "degraded": bool(validation.get("warnings")),
    }

    if persist_artifact:
        await register_artifact_async_fn(
            {
                "artifact_id": artifact_id,
                "strategy": "quant_factor_candidate_validation",
                "strategy_version": "p1.v1",
                "code": ",".join(codes[:5]),
                "payload": payload,
                "created_at": datetime.now().isoformat(),
            }
        )
        source_chain.append("services.artifact_registry")

    return ok(payload, source_chain=list(dict.fromkeys(source_chain)))
