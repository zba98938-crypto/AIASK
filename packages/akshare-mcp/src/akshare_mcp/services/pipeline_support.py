"""多阶段 AI 策略生成 — Pipeline 编排器。

``MultiStageStrategyPipeline`` 按顺序执行 5 个 Stage：
  event_recognition → theme_propagation → exposure_mapping
  → market_confirmation → strategy_generation

每阶段:
1. 尝试通过 ``StrategyLLMProvider.call_stage()`` 调用外部 LLM
2. 如果 LLM 失败或输出不合法，自动 fallback 到本地规则引擎
3. 验证输出 → 作为下一阶段的输入
4. 记录完整 provenance（prompt / response / 耗时 / 是否 fallback）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .strategy_stages import (
    EXTENDED_THEME_LIBRARY,
    STAGE_ORDER,
    StageDefinition,
    StageResult,
    get_stage_registry,
    validate_stage_output,
)
from .strategy_llm_provider import (
    StrategyLLMProvider,
    StrategyLLMRequestError,
    get_strategy_llm_provider,
)

logger = logging.getLogger(__name__)

def _get_pipeline_constants():
    from strategy_factory.api.constants import PIPELINE_STAGE_TIMEOUT_SEC, PIPELINE_STAGE_TIMEOUTS
    return PIPELINE_STAGE_TIMEOUT_SEC, PIPELINE_STAGE_TIMEOUTS



def _normalize_hint_text(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def _extract_named_market_hints(snapshot: dict[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(value: Any):
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        names.append(text)

    for item in list(snapshot.get("hot_sectors") or snapshot.get("sectors") or [])[:12]:
        if isinstance(item, dict):
            for key in ("group", "name", "sector", "theme"):
                add(item.get(key))
        else:
            add(item)
    for item in list(snapshot.get("dragon_tiger") or [])[:10]:
        if isinstance(item, dict):
            add(item.get("name"))
            add(item.get("industry"))
    return names


def _match_theme_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    hint_names = _extract_named_market_hints(snapshot)
    for hint in hint_names:
        normalized_hint = _normalize_hint_text(hint)
        if not normalized_hint:
            continue
        for theme in EXTENDED_THEME_LIBRARY:
            aliases = [str(alias or "").strip() for alias in list(theme.get("aliases") or []) if str(alias or "").strip()]
            alias_tokens = [theme.get("name"), theme.get("theme_code"), *aliases]
            normalized_tokens = [_normalize_hint_text(token) for token in alias_tokens if _normalize_hint_text(token)]
            if not any(
                normalized_hint in token or token in normalized_hint
                for token in normalized_tokens
            ):
                continue
            theme_code = str(theme.get("theme_code") or "").strip()
            if not theme_code or theme_code in seen_codes:
                break
            seen_codes.add(theme_code)
            candidates.append(
                {
                    "source_hint": hint,
                    "theme_code": theme_code,
                    "theme_name": theme.get("name"),
                    "aliases": aliases[:4],
                    "parent": theme.get("parent"),
                }
            )
            break
    return candidates



def _stage_output_list_counts(output: Any) -> dict[str, int]:
    if not isinstance(output, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in output.items():
        if isinstance(value, list):
            counts[str(key)] = len(value)
    return counts


def _stage_validation_failure_reason(stage_id: str, output: Any) -> str:
    if not isinstance(output, dict):
        return "output_not_object"
    counts = _stage_output_list_counts(output)
    if stage_id == "market_confirmation" and "confirmations" in output and counts.get("confirmations", 0) <= 0:
        return "empty_confirmations"
    if stage_id == "strategy_generation" and "candidates" in output and counts.get("candidates", 0) <= 0:
        return "empty_candidates"
    if stage_id == "event_recognition" and "events" in output and counts.get("events", 0) <= 0:
        return "empty_events"
    if stage_id == "theme_propagation" and "themes" in output and counts.get("themes", 0) <= 0:
        return "empty_themes"
    if stage_id == "exposure_mapping" and "exposures" in output and counts.get("exposures", 0) <= 0:
        return "empty_exposures"
    return "schema_validation_failed"


def _stage_result_fallback_reason(stage_result: StageResult) -> str:
    metrics = dict(stage_result.llm_error_metrics or {})
    if metrics.get("validation_failure_reason"):
        return str(metrics.get("validation_failure_reason"))
    if metrics.get("status"):
        return str(metrics.get("status"))
    if stage_result.llm_error_type:
        return str(stage_result.llm_error_type)
    if stage_result.error:
        return str(stage_result.error)
    if stage_result.used_fallback and not stage_result.llm_attempted:
        return "local_fallback_preferred_or_skip"
    return "fallback"


_PROVIDER_OUTPUT_FORMAT_ERROR_TYPES = {
    "jsondecodeerror",
    "providercompatibilityerror",
    "strategyllmresponseparseerror",
}


def _is_provider_output_format_failure(
    *,
    error_type: Optional[str],
    error_text: Optional[str],
    metrics: Optional[dict[str, Any]],
) -> bool:
    payload = dict(metrics or {})
    tokens = {
        str(error_type or "").strip().lower(),
        str(payload.get("last_error_type") or "").strip().lower(),
        str(payload.get("error_type") or "").strip().lower(),
    }
    for attempt in list(payload.get("attempts") or []):
        if isinstance(attempt, dict):
            tokens.add(str(attempt.get("error_type") or "").strip().lower())
            tokens.add(str(attempt.get("last_error_type") or "").strip().lower())
    if any(token in _PROVIDER_OUTPUT_FORMAT_ERROR_TYPES for token in tokens if token):
        return True
    text = " ".join(
        str(value or "")
        for value in (
            error_text,
            payload.get("last_error"),
            payload.get("error"),
            payload.get("content_preview"),
            payload.get("raw_text_preview"),
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "response body is not valid json",
            "response content is not valid json",
            "response content missing json payload",
            "response missing extractable content",
            "content-type=text/html",
        )
    )


def _provider_output_format_failure_result(
    *,
    stage_id: str,
    started: float,
    prompt_chars: int,
    llm_error: str,
    llm_error_type: Optional[str],
    llm_error_metrics: dict[str, Any],
) -> StageResult:
    metrics = {
        "stage_id": stage_id,
        "status": llm_error_metrics.get("status") or "failed",
        **dict(llm_error_metrics or {}),
        "local_fallback_suppressed": True,
        "suppression_reason": "provider_output_format_failure",
    }
    return StageResult(
        stage_id=stage_id,
        output={},
        used_fallback=False,
        llm_attempted=True,
        prompt_chars=prompt_chars,
        elapsed_sec=time.perf_counter() - started,
        error=f"llm output format failed for {stage_id}",
        llm_error=llm_error,
        llm_error_type=llm_error_type,
        llm_error_metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """完整 pipeline 执行结果。"""

    stages: dict[str, StageResult] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    elapsed_sec: float = 0.0
    error: Optional[str] = None

    @property
    def provenance(self) -> dict[str, Any]:
        """返回可序列化的 provenance 摘要。"""
        fallback_stage_ids = [sid for sid, sr in self.stages.items() if sr.used_fallback]
        invalid_output_stage_ids = [
            sid
            for sid, sr in self.stages.items()
            if dict(sr.llm_error_metrics or {}).get("status") == "invalid_output"
        ]
        stage_fallback_reasons = {
            sid: _stage_result_fallback_reason(sr)
            for sid, sr in self.stages.items()
            if sr.used_fallback
        }
        return {
            "pipeline_elapsed_sec": round(self.elapsed_sec, 4),
            "stage_count": len(self.stages),
            "candidate_count": len(self.candidates),
            "error": self.error,
            "fallback_stage_count": len(fallback_stage_ids),
            "fallback_stage_ids": fallback_stage_ids,
            "invalid_output_stage_ids": invalid_output_stage_ids,
            "stage_fallback_reasons": stage_fallback_reasons,
            "stages": {
                sid: {
                    "used_fallback": sr.used_fallback,
                    "llm_attempted": sr.llm_attempted,
                    "elapsed_sec": round(sr.elapsed_sec, 4),
                    "prompt_chars": sr.prompt_chars,
                    "response_chars": sr.response_chars,
                    "error": sr.error,
                    "llm_error": sr.llm_error,
                    "llm_error_type": sr.llm_error_type,
                    "llm_error_metrics": dict(sr.llm_error_metrics or {}),
                    "fallback_reason": stage_fallback_reasons.get(sid),
                }
                for sid, sr in self.stages.items()
            },
        }


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------
