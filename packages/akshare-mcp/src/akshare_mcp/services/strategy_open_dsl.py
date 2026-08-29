"""Helpers for the L3 open-DSL candidate lane."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from .strategy_dsl import compile_strategy_blueprint

_EMPTY_VALUES = (None, "", [], {})
_OPEN_DSL_TAGS = {
    "open_dsl",
    "llm_defined",
    "llm_defined_dsl",
}
_OPEN_DSL_GENERATOR_MODES = {
    "open_dsl",
    "llm_defined",
    "llm_defined_dsl",
}
_OPEN_DSL_STRATEGY_TYPES = {
    "dsl_rule",
    "open_dsl",
    "llm_defined",
}
_OPEN_DSL_REQUIRED_FIELDS = (
    "holding_horizon",
    "trade_plan",
    "risk_rules",
    "position_sizing",
    "rebalance_rule",
    "portfolio_spec",
    "execution_assumptions",
    "validation_profile",
)
_OPEN_DSL_REQUIRED_ECONOMIC_FIELDS = (
    "holding_rationale",
    "cost_sensitivity_grid",
    "position_model",
    "capacity_assumption",
    "market_regime_assumption",
)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _candidate_field_or_hypothesis(payload: dict[str, Any], field: str) -> Any:
    direct = payload.get(field)
    if direct not in _EMPTY_VALUES:
        return direct
    hypothesis = _as_dict(payload.get("hypothesis_artifact"))
    return hypothesis.get(field)


def _normalize_tags(value: Any) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    for item in values:
        tag = _string(item).lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def _normalize_codes(value: Any, *, limit: int = 12) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if len(codes) >= limit or item in _EMPTY_VALUES:
            return
        if isinstance(item, dict):
            for key in ("code", "symbol", "stock_code"):
                visit(item.get(key))
            for key in ("symbols", "codes", "stock_codes", "target_symbols"):
                visit(item.get(key))
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
            return
        token = _string(item)
        if not token:
            return
        if any(sep in token for sep in (",", ";", "|", "\n", "\t", " ")):
            token = token.replace(";", ",").replace("|", ",").replace("\n", ",").replace("\t", ",").replace(" ", ",")
            for part in token.split(","):
                visit(part)
            return
        code = token.split(".")[0]
        if code and code not in seen:
            seen.add(code)
            codes.append(code)

    visit(value)
    return codes[:limit]


def open_dsl_max_candidates_per_run() -> int:
    raw = os.getenv("STRATEGY_FACTORY_L3_OPEN_DSL_MAX_CANDIDATES_PER_RUN", "2")
    try:
        value = int(raw or 2)
    except Exception:
        value = 2
    return max(0, min(value, 8))


def is_open_dsl_candidate(candidate: Optional[dict[str, Any]]) -> bool:
    payload = dict(candidate or {})
    has_dsl = bool(payload.get("dsl") or payload.get("entry"))
    if not has_dsl:
        return False
    tags = set(_normalize_tags(payload.get("tags")))
    strategy_type = _string(payload.get("strategy_type")).lower()
    generator_mode = _string(payload.get("generator_mode")).lower()
    return bool(
        tags.intersection(_OPEN_DSL_TAGS)
        or generator_mode in _OPEN_DSL_GENERATOR_MODES
        or strategy_type in {"open_dsl", "llm_defined"}
    )


def is_open_dsl_spec_metadata(metadata: Optional[dict[str, Any]]) -> bool:
    payload = dict(metadata or {})
    if _string(payload.get("candidate_lane")).lower() == "l3_open_dsl":
        return True
    tags = set(_normalize_tags(payload.get("open_dsl_tags")))
    return bool(tags.intersection(_OPEN_DSL_TAGS))


@dataclass
class OpenDslCompilationResult:
    accepted: bool
    attempted: bool = False
    compiled: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    reject_reasons: list[str] = field(default_factory=list)


def compile_open_dsl_candidate(
    candidate: Optional[dict[str, Any]],
    *,
    market_frame: Optional[pd.DataFrame] = None,
) -> OpenDslCompilationResult:
    payload = dict(candidate or {})
    if not is_open_dsl_candidate(payload):
        return OpenDslCompilationResult(accepted=False, attempted=False)

    reject_reasons: list[str] = []
    target_symbols = _normalize_codes(
        [
            payload.get("target_symbols"),
            payload.get("stock_pool"),
            _as_dict(payload.get("dsl")).get("metadata"),
        ],
        limit=12,
    )
    if not target_symbols:
        reject_reasons.append("open_dsl_missing:target_symbols")
    for field in _OPEN_DSL_REQUIRED_FIELDS:
        if payload.get(field) in _EMPTY_VALUES:
            reject_reasons.append(f"open_dsl_missing:{field}")
    for field in _OPEN_DSL_REQUIRED_ECONOMIC_FIELDS:
        if _candidate_field_or_hypothesis(payload, field) in _EMPTY_VALUES:
            reject_reasons.append(f"open_dsl_missing:{field}")
    if reject_reasons:
        return OpenDslCompilationResult(
            accepted=False,
            attempted=True,
            reject_reasons=reject_reasons,
            audit={
                "candidate_lane": "l3_open_dsl",
                "target_symbols": list(target_symbols),
                "missing_fields": [
                    reason.split(":", 1)[1]
                    for reason in reject_reasons
                    if reason.startswith("open_dsl_missing:")
                ],
            },
        )

    try:
        compiled = compile_strategy_blueprint(payload, market_frame=market_frame, tune_for_factory=True)
    except Exception as exc:
        return OpenDslCompilationResult(
            accepted=False,
            attempted=True,
            reject_reasons=[f"open_dsl_compile_failed:{exc}"],
            audit={
                "candidate_lane": "l3_open_dsl",
                "target_symbols": list(target_symbols),
            },
        )

    compiled_meta = dict(compiled.get("metadata") or {})
    dsl_activity = dict(compiled_meta.get("dsl_activity") or {})
    entry_count = int(dsl_activity.get("entry_count") or 0)
    exit_count = int(dsl_activity.get("exit_count") or 0)
    has_market_frame = bool(market_frame is not None and not market_frame.empty)
    if has_market_frame and (entry_count <= 0 or exit_count <= 0):
        return OpenDslCompilationResult(
            accepted=False,
            attempted=True,
            reject_reasons=["open_dsl_non_executable_activity"],
            audit={
                "candidate_lane": "l3_open_dsl",
                "target_symbols": list(target_symbols),
                "dsl_activity": dsl_activity,
            },
        )

    return OpenDslCompilationResult(
        accepted=True,
        attempted=True,
        compiled=compiled,
        audit={
            "candidate_lane": "l3_open_dsl",
            "target_symbols": list(target_symbols),
            "dsl_activity": dsl_activity,
            "open_dsl_tags": list(_normalize_tags(payload.get("tags"))),
            "open_dsl_generator_mode": _string(payload.get("generator_mode")).lower() or "llm_defined",
        },
    )


__all__ = [
    "OpenDslCompilationResult",
    "compile_open_dsl_candidate",
    "is_open_dsl_candidate",
    "is_open_dsl_spec_metadata",
    "open_dsl_max_candidates_per_run",
]
