"""Execution-mode and artifact helpers for strategy factory runs."""

from __future__ import annotations

import hashlib
import json
import os
from enum import Enum
from typing import Any, Iterable, Mapping

from .compact_contracts import COMPACT_ARTIFACT_MAX_BYTES, bounded_payload


FACTORY_ENGINE_VERSION = "strategy_factory.v2"
FACTORY_EXECUTION_MODE_ENV = "STRATEGY_FACTORY_EXECUTION_MODE"


class FactoryExecutionMode(str, Enum):
    LEGACY_PRIMARY = "legacy_primary"
    SHADOW_READONLY = "shadow_readonly"
    V2_PRIMARY = "v2_primary"
    STOCK_FIRST_OBSERVE_PRIMARY = "stock_first_observe_primary"
    STOCK_FIRST_OBSERVE_SHADOW = "stock_first_observe_shadow"


class FactoryArtifactType(str, Enum):
    RESEARCH_PLANE = "research_plane"
    GOVERNANCE_PLANE = "governance_plane"
    TASK_ARTIFACT = "task_artifact"
    CANDIDATE_ARTIFACT = "candidate_artifact"
    EVIDENCE_ARTIFACT = "evidence_artifact"
    QUALITY_GATE = "quality_gate"
    BACKTEST_REPORT = "backtest_report"
    SUBMISSION_AUDIT = "submission_audit"
    SHADOW_DIFF = "shadow_diff"


DEFAULT_FACTORY_EXECUTION_MODE = FactoryExecutionMode.STOCK_FIRST_OBSERVE_PRIMARY
_ARTIFACT_TYPE_ORDER = (
    FactoryArtifactType.RESEARCH_PLANE,
    FactoryArtifactType.GOVERNANCE_PLANE,
    FactoryArtifactType.TASK_ARTIFACT,
    FactoryArtifactType.CANDIDATE_ARTIFACT,
    FactoryArtifactType.EVIDENCE_ARTIFACT,
    FactoryArtifactType.QUALITY_GATE,
    FactoryArtifactType.BACKTEST_REPORT,
    FactoryArtifactType.SUBMISSION_AUDIT,
    FactoryArtifactType.SHADOW_DIFF,
)


def normalize_factory_execution_mode(
    value: Any,
    default: FactoryExecutionMode = DEFAULT_FACTORY_EXECUTION_MODE,
) -> FactoryExecutionMode:
    if isinstance(value, FactoryExecutionMode):
        return value
    token = str(value or "").strip().lower()
    for mode in FactoryExecutionMode:
        if token == mode.value:
            return mode
    return default


def resolve_factory_execution_mode(
    value: Any | None = None,
    *,
    default: FactoryExecutionMode = DEFAULT_FACTORY_EXECUTION_MODE,
) -> FactoryExecutionMode:
    if value in (None, "", [], {}):
        value = os.getenv(FACTORY_EXECUTION_MODE_ENV)
    return normalize_factory_execution_mode(value, default=default)


def is_shadow_readonly(mode: FactoryExecutionMode | str) -> bool:
    return normalize_factory_execution_mode(mode) in {
        FactoryExecutionMode.SHADOW_READONLY,
        FactoryExecutionMode.STOCK_FIRST_OBSERVE_SHADOW,
    }


def is_stock_first_observe_mode(mode: FactoryExecutionMode | str) -> bool:
    return normalize_factory_execution_mode(mode) in {
        FactoryExecutionMode.STOCK_FIRST_OBSERVE_PRIMARY,
        FactoryExecutionMode.STOCK_FIRST_OBSERVE_SHADOW,
        FactoryExecutionMode.V2_PRIMARY,
    }


def is_stock_first_observe_primary(mode: FactoryExecutionMode | str) -> bool:
    return normalize_factory_execution_mode(mode) in {
        FactoryExecutionMode.STOCK_FIRST_OBSERVE_PRIMARY,
        FactoryExecutionMode.V2_PRIMARY,
    }


def resolve_runtime_mode_flags(mode: FactoryExecutionMode | str) -> dict[str, bool]:
    stock_first_mode = is_stock_first_observe_mode(mode)
    return {
        "stock_first_observe_mode": stock_first_mode,
        "router_enabled": stock_first_mode,
        "router_strict": stock_first_mode,
        "observe_first_enabled": stock_first_mode,
    }


def build_run_header(
    *,
    run_id: str,
    trace_id: str,
    started_at: str,
    execution_mode: FactoryExecutionMode | str,
    engine_version: str = FACTORY_ENGINE_VERSION,
    parity_role: str = "primary",
    read_only: bool = False,
) -> dict[str, Any]:
    resolved_mode = normalize_factory_execution_mode(execution_mode)
    return {
        "run_id": str(run_id or "").strip(),
        "trace_id": str(trace_id or "").strip(),
        "started_at": str(started_at or "").strip(),
        "execution_mode": resolved_mode.value,
        "engine_version": str(engine_version or FACTORY_ENGINE_VERSION).strip() or FACTORY_ENGINE_VERSION,
        "parity_role": str(parity_role or "primary").strip() or "primary",
        "read_only": bool(read_only),
    }


def build_artifact_refs(artifacts: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for artifact in list(artifacts or []):
        payload = dict(artifact or {})
        ref = {
            "artifact_type": str(payload.get("artifact_type") or "").strip(),
            "artifact_version": str(payload.get("artifact_version") or "").strip() or None,
            "payload_hash": str(payload.get("payload_hash") or "").strip() or None,
            "storage_mode": str(payload.get("storage_mode") or "inline_json").strip() or "inline_json",
        }
        if not ref["artifact_type"]:
            continue
        refs.append(ref)
    return refs


def _stable_hash(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_run_artifacts(result: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    raw = dict(result or {})
    research_plane = dict(raw.get("research_plane") or {})
    governance_plane = dict(raw.get("governance_plane") or {})
    artifacts: list[dict[str, Any]] = []

    def _append(artifact_type: FactoryArtifactType, payload: Any, *, version: Any = None) -> None:
        raw_data = dict(payload or {})
        data = bounded_payload(
            raw_data,
            field_name=f"strategy_factory_run_artifacts.{artifact_type.value}",
            max_bytes=COMPACT_ARTIFACT_MAX_BYTES,
        )
        if not data:
            return
        artifact_version = str(version or data.get("contract_version") or data.get("version") or "").strip() or "1"
        storage_mode = str(data.get("storage_mode") or "inline_compact_json").strip() or "inline_compact_json"
        artifacts.append(
            {
                "artifact_type": artifact_type.value,
                "artifact_version": artifact_version,
                "payload_json": data,
                "payload_hash": _stable_hash(data),
                "storage_mode": storage_mode,
            }
        )

    _append(FactoryArtifactType.RESEARCH_PLANE, research_plane)
    _append(FactoryArtifactType.GOVERNANCE_PLANE, governance_plane)
    _append(
        FactoryArtifactType.TASK_ARTIFACT,
        dict(research_plane.get("task_artifact") or {}),
    )
    _append(
        FactoryArtifactType.CANDIDATE_ARTIFACT,
        dict(research_plane.get("candidate_artifact") or {}),
    )
    _append(
        FactoryArtifactType.EVIDENCE_ARTIFACT,
        dict(research_plane.get("evidence_artifact") or {}),
    )
    _append(
        FactoryArtifactType.SUBMISSION_AUDIT,
        dict((governance_plane.get("submission_artifact") or {})),
    )
    _append(
        FactoryArtifactType.SHADOW_DIFF,
        dict(raw.get("parity_result") or {}),
    )

    ordered_types = {artifact_type.value: index for index, artifact_type in enumerate(_ARTIFACT_TYPE_ORDER)}
    artifacts.sort(key=lambda item: ordered_types.get(str(item.get("artifact_type") or ""), 999))
    return artifacts


def build_shadow_parity_result(
    primary_result: Mapping[str, Any] | None,
    shadow_result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    primary = dict(primary_result or {})
    shadow = dict(shadow_result or {})
    primary_summary = dict(primary.get("summary") or {})
    shadow_summary = dict(shadow.get("summary") or {})

    primary_submission = dict(
        ((primary.get("governance_plane") or {}) if isinstance(primary.get("governance_plane"), dict) else {}).get(
            "submission_artifact"
        )
        or {}
    )
    shadow_submission = dict(
        ((shadow.get("governance_plane") or {}) if isinstance(shadow.get("governance_plane"), dict) else {}).get(
            "submission_artifact"
        )
        or {}
    )

    def _lane_distribution(summary: Mapping[str, Any], submission: Mapping[str, Any]) -> dict[str, int]:
        return {
            "formal_incubation": int(summary.get("formal_incubation_count") or submission.get("formal_incubation_count") or 0),
            "observe_incubation": int(summary.get("observe_incubation_count") or submission.get("observe_incubation_count") or 0),
            "live_ready_review": int(summary.get("live_ready_review_count") or submission.get("live_ready_review_count") or 0),
            "deferred_submission": int(summary.get("deferred_submission_count") or submission.get("deferred_submission_count") or 0),
        }

    def _final_status_distribution(submission: Mapping[str, Any]) -> dict[str, int]:
        return {
            str(key or "").strip(): int(value or 0)
            for key, value in dict(submission.get("status_distribution") or {}).items()
            if str(key or "").strip()
        }

    comparisons = {
        "readiness_decision": {
            "primary": primary_summary.get("factory_readiness_decision"),
            "shadow": shadow_summary.get("factory_readiness_decision"),
        },
        "candidate_count": {
            "primary": int(primary_summary.get("candidates_spawned") or 0),
            "shadow": int(shadow_summary.get("candidates_spawned") or 0),
        },
        "gate_passed_count": {
            "primary": int(primary_summary.get("passed_quality_gate") or primary_summary.get("gate_3_passed") or 0),
            "shadow": int(shadow_summary.get("passed_quality_gate") or shadow_summary.get("gate_3_passed") or 0),
        },
        "dedup_count": {
            "primary": int(primary_summary.get("after_dedup") or primary_summary.get("governance_after_dedup") or 0),
            "shadow": int(shadow_summary.get("after_dedup") or shadow_summary.get("governance_after_dedup") or 0),
        },
        "submission_lane_distribution": {
            "primary": _lane_distribution(primary_summary, primary_submission),
            "shadow": _lane_distribution(shadow_summary, shadow_submission),
        },
        "final_status_distribution": {
            "primary": _final_status_distribution(primary_submission),
            "shadow": _final_status_distribution(shadow_submission),
        },
    }

    mismatches = [
        key
        for key, payload in comparisons.items()
        if payload.get("primary") != payload.get("shadow")
    ]
    return {
        "comparison_contract_version": "strategy_factory.shadow_parity.v1",
        "status": "matched" if not mismatches else "mismatch",
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "comparisons": comparisons,
    }


def resolve_factory_engine_version(
    execution_mode: FactoryExecutionMode | str,
    *,
    default: str = FACTORY_ENGINE_VERSION,
) -> str:
    resolved_mode = normalize_factory_execution_mode(execution_mode)
    if resolved_mode == FactoryExecutionMode.V2_PRIMARY:
        return "strategy_factory.v2.primary"
    if resolved_mode == FactoryExecutionMode.SHADOW_READONLY:
        return "strategy_factory.v2.shadow"
    if resolved_mode == FactoryExecutionMode.STOCK_FIRST_OBSERVE_PRIMARY:
        return "strategy_factory.stock_first_observe.primary"
    if resolved_mode == FactoryExecutionMode.STOCK_FIRST_OBSERVE_SHADOW:
        return "strategy_factory.stock_first_observe.shadow"
    return str(default or FACTORY_ENGINE_VERSION).strip() or FACTORY_ENGINE_VERSION


__all__ = [
    "DEFAULT_FACTORY_EXECUTION_MODE",
    "FACTORY_ENGINE_VERSION",
    "FACTORY_EXECUTION_MODE_ENV",
    "FactoryArtifactType",
    "FactoryExecutionMode",
    "build_artifact_refs",
    "build_run_artifacts",
    "build_run_header",
    "build_shadow_parity_result",
    "is_shadow_readonly",
    "is_stock_first_observe_mode",
    "is_stock_first_observe_primary",
    "normalize_factory_execution_mode",
    "resolve_runtime_mode_flags",
    "resolve_factory_engine_version",
    "resolve_factory_execution_mode",
]
