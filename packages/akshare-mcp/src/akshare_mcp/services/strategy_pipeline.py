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

from .pipeline_support import (
    PipelineResult,
    _PROVIDER_OUTPUT_FORMAT_ERROR_TYPES,
    _extract_named_market_hints,
    _get_pipeline_constants,
    _is_provider_output_format_failure,
    _match_theme_candidates,
    _normalize_hint_text,
    _provider_output_format_failure_result,
    _stage_output_list_counts,
    _stage_result_fallback_reason,
    _stage_validation_failure_reason,
)
from .pipeline_stages import _PipelineStageMixin


def _is_real_llm_error_fallback(stage_result: Any) -> bool:
    """判定一个 fallback 是否源于"真实 LLM 错误"(timeout/connectivity/parse/overload 等),
    用于 skip_llm 熔断计数。

    P0-B:原 skip_llm 由 `used_fallback` 计数触发,但 used_fallback 混杂了:
      - prefer_fallback / provider 未启用 / skip_llm 透传 → 非错误性 fallback(llm_attempted=False)
      - P0-A 之前的合法空数组误判(现已修:合法空 used_fallback=False)
    只有"尝试过 LLM 且带真实错误类型"才算真错。非错误 fallback 不应点燃熔断,
    否则 LLM 正常工作却被误判放弃(病灶 B)。
    """
    if not getattr(stage_result, "used_fallback", False):
        return False
    if not getattr(stage_result, "llm_attempted", False):
        return False
    return bool(getattr(stage_result, "llm_error_type", None))


class MultiStageStrategyPipeline(_PipelineStageMixin):
    """链式执行 5 个 Stage 的编排器。"""

    def __init__(self, provider: Optional[StrategyLLMProvider] = None):
        self.provider = provider or get_strategy_llm_provider()
        self.registry = get_stage_registry()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_pipeline(
        self,
        db: Any,
        snapshot: Optional[dict[str, Any]] = None,
        research_task: Optional[dict[str, Any]] = None,
    ) -> PipelineResult:
        """两阶段并行编排。

        Phase-1（并行）：event_recognition + theme_propagation + exposure_mapping
          三路同时从初始 snapshot 出发，互不依赖，节省约 2 个 LLM 往返延迟。
          theme_propagation / exposure_mapping 的 fallback 已增强，可在无上游
          输出时从 snapshot 热门板块推断，保证输出非空。

        Phase-2（串行）：market_confirmation → strategy_generation
          依赖 Phase-1 合并后的上下文，必须顺序执行。
        """
        snapshot = snapshot or {}
        pipeline_started = time.perf_counter()
        result = PipelineResult()
        PIPELINE_STAGE_TIMEOUT_SEC, PIPELINE_STAGE_TIMEOUTS = _get_pipeline_constants()

        stage_input = self._build_initial_input(snapshot, research_task)
        skip_llm = False

        # ── Phase 1：三路并行 ────────────────────────────────────────────────
        PARALLEL_STAGES = ["event_recognition", "theme_propagation", "exposure_mapping"]
        parallel_results: list[Any] = await asyncio.gather(
            *[
                self.run_stage(
                    db=db,
                    stage_id=sid,
                    input_data=stage_input,
                    snapshot=snapshot,
                    stage_def=self.registry.get(sid),
                    skip_llm=skip_llm,
                )
                for sid in PARALLEL_STAGES
            ],
            return_exceptions=True,
        )

        timeout_hit = False
        parallel_llm_failures = 0
        fatal_error: Optional[str] = None

        for sid, sr in zip(PARALLEL_STAGES, parallel_results):
            if isinstance(sr, BaseException):
                logger.warning("Pipeline stage %s raised exception: %s", sid, sr)
                from .strategy_stages import StageResult as _SR
                sr = _SR(stage_id=sid, output={}, used_fallback=True, error=str(sr))
            result.stages[sid] = sr
            if sr.error and not sr.output:
                fatal_error = f"stage {sid} failed: {sr.error}"
                break
            if not skip_llm and _is_real_llm_error_fallback(sr):
                parallel_llm_failures += 1
                _limit = PIPELINE_STAGE_TIMEOUTS.get(sid, PIPELINE_STAGE_TIMEOUT_SEC)
                if sr.elapsed_sec >= _limit * 0.8:
                    timeout_hit = True
            stage_input = {**stage_input, **sr.output}

        if fatal_error:
            result.error = fatal_error
            result.candidates = list(stage_input.get("candidates") or [])
            result.elapsed_sec = time.perf_counter() - pipeline_started
            return result

        if timeout_hit or parallel_llm_failures >= 2:
            skip_llm = True
            logger.info(
                "Pipeline Phase-1: %d LLM failure(s), timeout_hit=%s → skip_llm for Phase-2",
                parallel_llm_failures, timeout_hit,
            )

        # ── Phase 2：串行（依赖 Phase-1 合并上下文）────────────────────────
        consecutive_llm_failures = 0
        for stage_id in ["market_confirmation", "strategy_generation"]:
            stage_def = self.registry.get(stage_id)
            if stage_def is None:
                logger.error("Pipeline stage %s not found in registry", stage_id)
                continue

            stage_result = await self.run_stage(
                db=db,
                stage_id=stage_id,
                input_data=stage_input,
                snapshot=snapshot,
                stage_def=stage_def,
                skip_llm=skip_llm,
            )
            result.stages[stage_id] = stage_result

            if not skip_llm:
                if _is_real_llm_error_fallback(stage_result):
                    if not stage_def.prefer_fallback:
                        consecutive_llm_failures += 1
                    _stage_limit = PIPELINE_STAGE_TIMEOUTS.get(stage_id, PIPELINE_STAGE_TIMEOUT_SEC)
                    if stage_result.elapsed_sec >= _stage_limit * 0.8:
                        logger.info(
                            "Stage %s took %.1fs (LLM timeout), skipping LLM for remaining stages",
                            stage_id, stage_result.elapsed_sec,
                        )
                        skip_llm = True
                    elif consecutive_llm_failures >= 2:
                        logger.info(
                            "LLM failed %d consecutive stages in Phase-2, skipping LLM",
                            consecutive_llm_failures,
                        )
                        skip_llm = True
                else:
                    consecutive_llm_failures = 0

            if stage_result.error and not stage_result.output:
                result.error = f"stage {stage_id} failed: {stage_result.error}"
                break

            stage_input = {**stage_input, **stage_result.output}

        result.candidates = list(stage_input.get("candidates") or [])
        result.elapsed_sec = time.perf_counter() - pipeline_started
        return result

    async def run_stage(
        self,
        db: Any,
        stage_id: str,
        input_data: dict[str, Any],
        snapshot: Optional[dict[str, Any]] = None,
        stage_def: Optional[StageDefinition] = None,
        skip_llm: bool = False,
    ) -> StageResult:
        """执行单个 Stage（支持外部按需调用）。"""
        snapshot = snapshot or {}
        if stage_def is None:
            stage_def = self.registry.get(stage_id)
        if stage_def is None:
            return StageResult(
                stage_id=stage_id,
                output={},
                error=f"stage {stage_id} not registered",
            )

        started = time.perf_counter()
        llm_attempted = False
        prepared_input = dict(input_data or {})
        prompt_chars = 0
        llm_error = None
        llm_error_type = None
        llm_error_metrics: dict[str, Any] = {}

        if stage_def.prefer_fallback:
            logger.info("Stage %s using local fallback (prefer_fallback)", stage_id)
            return await self._call_fallback_stage(
                stage_def,
                db,
                input_data,
                snapshot,
                started,
                llm_attempted=False,
                prompt_chars=prompt_chars,
                llm_error=llm_error,
                llm_error_type=llm_error_type,
                llm_error_metrics=llm_error_metrics,
            )

        # 1) 尝试 LLM 调用（跳过条件：skip_llm 标记或 provider 不可用）
        if not skip_llm and self.provider.is_enabled():
            llm_attempted = True
            prepared_input = self._prepare_stage_input(stage_id, input_data)
            # PR-AI4: 向量检索注入 — 为 strategy_generation 阶段注入真实基本面数据
            if stage_id == "strategy_generation" and db is not None:
                prepared_input = await self._enrich_with_stock_profiles(
                    db, prepared_input
                )
            # PR-AI2: Look-ahead Bias 防护 — 注入时间约束
            _snapshot_date = str(
                (input_data.get("market_snapshot") or {}).get("date")
                or (snapshot or {}).get("date")
                or (snapshot or {}).get("snapshot_date")
                or ""
            ).strip()
            _lookahead_guard = ""
            if _snapshot_date:
                _lookahead_guard = (
                    f"重要约束：当前业务时点={_snapshot_date}。"
                    f"你只能引用早于或等于{_snapshot_date}的事件和数据，"
                    f"严禁引用此日期之后的任何信息。\n\n"
                )
            _effective_system_prompt = _lookahead_guard + stage_def.system_prompt
            prompt_chars = len(_effective_system_prompt) + len(json.dumps(prepared_input, ensure_ascii=False, default=str))
            pipeline_stage_timeout_sec, pipeline_stage_timeouts = _get_pipeline_constants()
            try:
                output = await self.provider.call_stage(
                    stage_id=stage_def.stage_id,
                    input_data=prepared_input,
                    system_prompt=_effective_system_prompt,
                    max_tokens=stage_def.max_tokens,
                    temperature=stage_def.temperature,
                    timeout_sec=pipeline_stage_timeouts.get(stage_def.stage_id, pipeline_stage_timeout_sec),
                )
                if validate_stage_output(stage_id, output):
                    elapsed = time.perf_counter() - started
                    return StageResult(
                        stage_id=stage_id,
                        output=output,
                        used_fallback=False,
                        llm_attempted=True,
                        prompt_chars=prompt_chars,
                        response_chars=len(json.dumps(output, ensure_ascii=False, default=str)),
                        elapsed_sec=elapsed,
                    )
                else:
                    llm_error = f"llm output failed validation for {stage_id}"
                    llm_error_type = "ValidationError"
                    validation_failure_reason = _stage_validation_failure_reason(stage_id, output)
                    llm_error_metrics = {
                        "stage_id": stage_id,
                        "status": "invalid_output",
                        "validation_failure_reason": validation_failure_reason,
                        "output_keys": list(output.keys()) if isinstance(output, dict) else [],
                        "output_list_counts": _stage_output_list_counts(output),
                        "output_sample": json.dumps(output, ensure_ascii=False, default=str)[:200],
                    }
                    logger.warning(
                        "Stage %s LLM output failed validation, falling back. "
                        "reason=%s, output keys=%s, sample=%.200s",
                        stage_id,
                        validation_failure_reason,
                        list(output.keys()) if isinstance(output, dict) else [],
                        json.dumps(output, ensure_ascii=False, default=str)[:200],
                    )
            except StrategyLLMRequestError as exc:
                llm_error = str(exc)
                llm_error_metrics = dict(getattr(exc, "metrics", {}) or {})
                llm_error_type = str(llm_error_metrics.get("last_error_type") or exc.__class__.__name__)
                if llm_error_metrics.get("status") == "cooldown_skip" and llm_error_type == exc.__class__.__name__:
                    llm_error_type = (
                        "RecentOverloadCooldown"
                        if llm_error_metrics.get("cooldown_reason") == "recent_overload"
                        else "RecentTimeoutCooldown"
                    )
                log_fn = logger.info if llm_error_metrics.get("status") == "cooldown_skip" else logger.warning
                if _is_provider_output_format_failure(
                    error_type=llm_error_type,
                    error_text=llm_error,
                    metrics=llm_error_metrics,
                ):
                    logger.warning(
                        "Stage %s LLM output format failed after repair/retry; suppressing local fallback: %s",
                        stage_id,
                        exc,
                    )
                    return _provider_output_format_failure_result(
                        stage_id=stage_id,
                        started=started,
                        prompt_chars=prompt_chars,
                        llm_error=llm_error,
                        llm_error_type=llm_error_type,
                        llm_error_metrics=llm_error_metrics,
                    )
                log_fn("Stage %s LLM call failed: %s, falling back", stage_id, exc)
            except Exception as exc:
                llm_error = str(exc)
                llm_error_metrics = dict(getattr(exc, "metrics", {}) or {})
                llm_error_type = str(llm_error_metrics.get("last_error_type") or exc.__class__.__name__)
                if llm_error_metrics.get("status") == "cooldown_skip" and llm_error_type == exc.__class__.__name__:
                    llm_error_type = (
                        "RecentOverloadCooldown"
                        if llm_error_metrics.get("cooldown_reason") == "recent_overload"
                        else "RecentTimeoutCooldown"
                    )
                llm_error_metrics = {
                    "stage_id": stage_id,
                    "status": llm_error_metrics.get("status") or "failed",
                    "last_error_type": llm_error_type,
                    **llm_error_metrics,
                }
                log_fn = logger.info if llm_error_metrics.get("status") == "cooldown_skip" else logger.warning
                if _is_provider_output_format_failure(
                    error_type=llm_error_type,
                    error_text=llm_error,
                    metrics=llm_error_metrics,
                ):
                    logger.warning(
                        "Stage %s LLM output format failed after repair/retry; suppressing local fallback: %s",
                        stage_id,
                        exc,
                    )
                    return _provider_output_format_failure_result(
                        stage_id=stage_id,
                        started=started,
                        prompt_chars=prompt_chars,
                        llm_error=llm_error,
                        llm_error_type=llm_error_type,
                        llm_error_metrics=llm_error_metrics,
                    )
                log_fn("Stage %s LLM call failed: %s, falling back", stage_id, exc)
        elif skip_llm:
            logger.info("Stage %s skipping LLM (prior stage timed out)", stage_id)

        # 2) Fallback 到本地规则引擎
        return await self._call_fallback_stage(
            stage_def,
            db,
            input_data,
            snapshot,
            started,
            llm_attempted=llm_attempted,
            prompt_chars=prompt_chars,
            llm_error=llm_error,
            llm_error_type=llm_error_type,
            llm_error_metrics=llm_error_metrics,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------


_pipeline: Optional[MultiStageStrategyPipeline] = None


def get_strategy_pipeline() -> MultiStageStrategyPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = MultiStageStrategyPipeline()
    return _pipeline
