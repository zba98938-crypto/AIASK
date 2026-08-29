"""Public quality reporting and panel helpers."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ..application.quality_reporting import (
    PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED,
    build_quality_report,
    has_only_statistical_gate_failures,
    is_factory_ai_prototype_strategy,
    maybe_grant_provisional_incubation,
    normalize_quality_gate_result,
    quality_gate_reason_code,
    safe_metric_value,
)

_QUALITY_GATE_EXPORTS = {
    "GateResult",
    "build_completed_gate_3_report",
    "build_legacy_gate_report",
    "build_pending_gate_3_report",
    "finalize_gate_report",
    "gate_0_structural",
    "gate_1_fast_screen",
    "run_gated_filter",
    "run_gated_submission_pipeline",
}


def _quality_gate_export(name: str) -> Any:
    module = import_module("..application.quality_gates", __package__)
    return getattr(module, name)


def build_completed_gate_3_report(*args, **kwargs):
    return _quality_gate_export("build_completed_gate_3_report")(*args, **kwargs)


def build_legacy_gate_report(*args, **kwargs):
    return _quality_gate_export("build_legacy_gate_report")(*args, **kwargs)


def build_pending_gate_3_report(*args, **kwargs):
    return _quality_gate_export("build_pending_gate_3_report")(*args, **kwargs)


def finalize_gate_report(*args, **kwargs):
    return _quality_gate_export("finalize_gate_report")(*args, **kwargs)


def gate_0_structural(*args, **kwargs):
    return _quality_gate_export("gate_0_structural")(*args, **kwargs)


async def gate_1_fast_screen(*args, **kwargs):
    return await _quality_gate_export("gate_1_fast_screen")(*args, **kwargs)


async def run_gated_filter(*args, **kwargs):
    return await _quality_gate_export("run_gated_filter")(*args, **kwargs)


async def run_gated_submission_pipeline(*args, **kwargs):
    return await _quality_gate_export("run_gated_submission_pipeline")(*args, **kwargs)


async def _build_strategy_panels(*args, **kwargs):
    from ..application.panels import _build_strategy_panels as target

    return await target(*args, **kwargs)


async def build_strategy_panels(*args, **kwargs):
    return await _build_strategy_panels(*args, **kwargs)


async def _run_validation_report(*args, **kwargs):
    from ..application.panels import _run_validation_report as target

    return await target(*args, **kwargs)


async def run_validation_report(*args, **kwargs):
    return await _run_validation_report(*args, **kwargs)


async def _run_risk_report(*args, **kwargs):
    from ..application.panels import _run_risk_report as target

    return await target(*args, **kwargs)


async def run_risk_report(*args, **kwargs):
    return await _run_risk_report(*args, **kwargs)


__all__ = [
    "GateResult",
    "PROVISIONAL_MIN_STATISTICAL_CHECKS_PASSED",
    "_build_strategy_panels",
    "_run_risk_report",
    "_run_validation_report",
    "build_strategy_panels",
    "build_completed_gate_3_report",
    "build_legacy_gate_report",
    "build_pending_gate_3_report",
    "build_quality_report",
    "finalize_gate_report",
    "gate_0_structural",
    "gate_1_fast_screen",
    "has_only_statistical_gate_failures",
    "is_factory_ai_prototype_strategy",
    "maybe_grant_provisional_incubation",
    "normalize_quality_gate_result",
    "quality_gate_reason_code",
    "run_risk_report",
    "run_gated_filter",
    "run_gated_submission_pipeline",
    "run_validation_report",
    "safe_metric_value",
]


def __getattr__(name: str) -> Any:
    if name in _QUALITY_GATE_EXPORTS:
        value = _quality_gate_export(name)
        globals()[name] = value
        return value
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
