"""Mode presets and env wiring for Strategy Factory quality sessions."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from _quality_session_common import DEFAULT_EXECUTION_MODE


QUALITY_SESSION_MODES_ENV = "STRATEGY_QUALITY_SESSION_MODES"
QUALITY_SESSION_EXECUTION_MODE_ENV = "STRATEGY_QUALITY_SESSION_EXECUTION_MODE"
QUALITY_SESSION_OBSERVE_EXECUTION_MODE_ENV = "STRATEGY_QUALITY_SESSION_OBSERVE_EXECUTION_MODE"
QUALITY_SESSION_STRICT_EXECUTION_MODE_ENV = "STRATEGY_QUALITY_SESSION_STRICT_EXECUTION_MODE"
QUALITY_SESSION_MIN_GRADE_ENV = "STRATEGY_QUALITY_SESSION_MIN_VALIDATION_GRADE"
QUALITY_SESSION_GATE3_RECORD_ONLY_ENV = "STRATEGY_QUALITY_SESSION_GATE3_RECORD_ONLY_ENABLED"
QUALITY_SESSION_GATE3_RECORD_ONLY_INTAKE_ENV = "STRATEGY_QUALITY_SESSION_GATE3_RECORD_ONLY_INTAKE_ENABLED"

OBSERVE_FIRST_MODE_ID = "observe_first"
STRICT_GATED_MODE_ID = "strict_gated"
STRICT_GATED_EXECUTION_MODE = "legacy_primary"

_MODE_ALIASES = {
    "observe": OBSERVE_FIRST_MODE_ID,
    "observe-first": OBSERVE_FIRST_MODE_ID,
    "observe_first": OBSERVE_FIRST_MODE_ID,
    "stock-first-observe": OBSERVE_FIRST_MODE_ID,
    "stock_first_observe": OBSERVE_FIRST_MODE_ID,
    "current": OBSERVE_FIRST_MODE_ID,
    "strict": STRICT_GATED_MODE_ID,
    "strict-gated": STRICT_GATED_MODE_ID,
    "strict_gated": STRICT_GATED_MODE_ID,
    "gated": STRICT_GATED_MODE_ID,
    "legacy": STRICT_GATED_MODE_ID,
}


@dataclass(frozen=True)
class QualitySessionModeConfig:
    mode_id: str
    label: str
    execution_mode: str
    observe_first_enabled: bool
    wide_intake_observe_enabled: bool
    description: str

    def as_state_dict(self) -> dict[str, Any]:
        return {
            "mode_id": self.mode_id,
            "label": self.label,
            "execution_mode": self.execution_mode,
            "observe_first_enabled": self.observe_first_enabled,
            "wide_intake_observe_enabled": self.wide_intake_observe_enabled,
            "description": self.description,
        }


def _truthy_env(value: bool) -> str:
    return "1" if bool(value) else "0"


def _clean_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _split_modes(raw: Any) -> list[str]:
    return [item for item in re.split(r"[\s,]+", str(raw or "").strip()) if item]


def parse_boolish(raw: Any, *, default: bool = False) -> bool:
    token = _clean_token(raw)
    if not token:
        return bool(default)
    if token in {"1", "true", "yes", "on", "enabled"}:
        return True
    if token in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def normalize_quality_session_mode(raw: Any) -> list[str]:
    tokens = _split_modes(raw)
    if not tokens:
        tokens = [OBSERVE_FIRST_MODE_ID]
    resolved: list[str] = []
    for token in tokens:
        normalized = _clean_token(token).replace("/", "-")
        if normalized == "compare":
            candidates = [OBSERVE_FIRST_MODE_ID, STRICT_GATED_MODE_ID]
        else:
            mode_id = _MODE_ALIASES.get(normalized)
            if not mode_id:
                allowed = "observe-first, strict-gated, compare"
                raise ValueError(f"unknown quality session mode `{token}`; expected one of: {allowed}")
            candidates = [mode_id]
        for candidate in candidates:
            if candidate not in resolved:
                resolved.append(candidate)
    return resolved


def resolve_quality_session_modes(
    raw_modes: Any = None,
    *,
    execution_mode: str | None = None,
    observe_execution_mode: str | None = None,
    strict_execution_mode: str | None = None,
) -> list[QualitySessionModeConfig]:
    mode_ids = normalize_quality_session_mode(raw_modes or os.getenv(QUALITY_SESSION_MODES_ENV))
    base_execution_mode = (
        str(execution_mode or "").strip()
        or str(os.getenv(QUALITY_SESSION_EXECUTION_MODE_ENV) or "").strip()
    )
    observe_mode = (
        str(observe_execution_mode or "").strip()
        or str(os.getenv(QUALITY_SESSION_OBSERVE_EXECUTION_MODE_ENV) or "").strip()
        or base_execution_mode
        or DEFAULT_EXECUTION_MODE
    )
    strict_mode = (
        str(strict_execution_mode or "").strip()
        or str(os.getenv(QUALITY_SESSION_STRICT_EXECUTION_MODE_ENV) or "").strip()
        or (base_execution_mode if mode_ids == [STRICT_GATED_MODE_ID] else "")
        or STRICT_GATED_EXECUTION_MODE
    )

    configs: list[QualitySessionModeConfig] = []
    for mode_id in mode_ids:
        if mode_id == OBSERVE_FIRST_MODE_ID:
            configs.append(
                QualitySessionModeConfig(
                    mode_id=OBSERVE_FIRST_MODE_ID,
                    label="observe-first",
                    execution_mode=observe_mode,
                    observe_first_enabled=True,
                    wide_intake_observe_enabled=True,
                    description="current observe-first path with wide-intake observe admission enabled",
                )
            )
        elif mode_id == STRICT_GATED_MODE_ID:
            configs.append(
                QualitySessionModeConfig(
                    mode_id=STRICT_GATED_MODE_ID,
                    label="strict-gated",
                    execution_mode=strict_mode,
                    observe_first_enabled=False,
                    wide_intake_observe_enabled=False,
                    description="strict gated path with observe-first and forced wide-intake disabled",
                )
            )
    return configs


def resolve_session_runtime_controls(args: Any) -> dict[str, Any]:
    min_grade = (
        str(getattr(args, "min_validation_grade", None) or "").strip()
        or str(os.getenv(QUALITY_SESSION_MIN_GRADE_ENV) or "").strip()
        or str(os.getenv("STRATEGY_FACTORY_MIN_VALIDATION_GRADE") or "").strip()
        or "C"
    ).upper()
    return {
        "min_validation_grade": min_grade,
        "gate3_record_only_enabled": parse_boolish(
            getattr(args, "gate3_record_only", None)
            or os.getenv(QUALITY_SESSION_GATE3_RECORD_ONLY_ENV)
            or os.getenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED"),
            default=False,
        ),
        "gate3_record_only_intake_enabled": parse_boolish(
            getattr(args, "gate3_record_only_intake", None)
            or os.getenv(QUALITY_SESSION_GATE3_RECORD_ONLY_INTAKE_ENV)
            or os.getenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED"),
            default=False,
        ),
    }


def apply_quality_mode_env(
    mode: QualitySessionModeConfig,
    *,
    runtime_controls: dict[str, Any],
) -> dict[str, str]:
    env = {
        "STRATEGY_FACTORY_EXECUTION_MODE": str(mode.execution_mode),
        "STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED": _truthy_env(mode.observe_first_enabled),
        "STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED": _truthy_env(
            mode.wide_intake_observe_enabled
        ),
        "STRATEGY_FACTORY_MIN_VALIDATION_GRADE": str(
            runtime_controls.get("min_validation_grade") or "C"
        ).upper(),
        "STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED": _truthy_env(
            runtime_controls.get("gate3_record_only_enabled")
        ),
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED": _truthy_env(
            runtime_controls.get("gate3_record_only_intake_enabled")
        ),
    }
    os.environ.update(env)
    return env


def mode_config_from_state(raw: dict[str, Any]) -> QualitySessionModeConfig:
    mode_id = str(raw.get("mode_id") or raw.get("mode") or "").strip() or OBSERVE_FIRST_MODE_ID
    label = str(raw.get("label") or mode_id.replace("_", "-")).strip()
    return QualitySessionModeConfig(
        mode_id=mode_id,
        label=label,
        execution_mode=str(raw.get("execution_mode") or DEFAULT_EXECUTION_MODE).strip(),
        observe_first_enabled=parse_boolish(raw.get("observe_first_enabled"), default=False),
        wide_intake_observe_enabled=parse_boolish(
            raw.get("wide_intake_observe_enabled"),
            default=False,
        ),
        description=str(raw.get("description") or "").strip(),
    )


__all__ = [
    "OBSERVE_FIRST_MODE_ID",
    "STRICT_GATED_MODE_ID",
    "QualitySessionModeConfig",
    "apply_quality_mode_env",
    "mode_config_from_state",
    "normalize_quality_session_mode",
    "parse_boolish",
    "resolve_quality_session_modes",
    "resolve_session_runtime_controls",
]
