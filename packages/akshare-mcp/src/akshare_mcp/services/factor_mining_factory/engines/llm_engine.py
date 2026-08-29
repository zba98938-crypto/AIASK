"""LLM 搜索引擎 — 双链架构（Generation Chain + Optimization Chain）。

参考：Chain-of-Alpha (arXiv:2508.06312)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .base import EngineStatus, FactorCandidate, SearchBudget

logger = logging.getLogger(__name__)


class LLMSearchEngine:
    """LLM 双链搜索引擎。

    Phase 1 (Generation Chain): 高 temperature 广泛探索
    Phase 2 (Optimization Chain): 低 temperature 精炼优化
    """

    engine_id: str
    engine_type: str = "llm"

    def __init__(self, engine_id: str = "llm_primary"):
        self.engine_id = engine_id
        self._success_count = 0
        self._failure_count = 0
        self._last_run_at: str | None = None
        self._last_error: str | None = None

    async def generate(
        self,
        context: Any,
        budget: SearchBudget,
    ) -> list[FactorCandidate]:
        """双链生成候选因子。"""
        try:
            # Phase 1: Generation Chain — 调用现有 LLM provider
            raw_candidates = await self._generation_chain(context, budget)

            # Phase 2: Optimization Chain — 快速验证 + 反馈精炼
            refined = await self._optimization_chain(raw_candidates, context)

            self._success_count += 1
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            return refined[:budget.candidate_count]

        except Exception as exc:
            self._failure_count += 1
            self._last_error = str(exc)
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            logger.warning("LLMSearchEngine: generation failed: %s", exc)
            return []

    async def _generation_chain(self, context: Any, budget: SearchBudget) -> list[FactorCandidate]:
        """生成链：利用现有 factor_prompt_builder + factor_llm_provider。"""
        from ...factor_prompt_builder import build_factor_mining_prompt
        from ...factor_llm_provider import get_factor_llm_provider
        from ....storage import get_db

        db = get_db()
        provider = get_factor_llm_provider()

        if not provider.is_enabled():
            logger.info("LLMSearchEngine: LLM provider not enabled, skipping")
            return []

        # 构建 prompt
        # prompt 规模治本:codes 默认 4(原8)、lookback 默认 120(原180)。
        # 实测原配置真实 prompt≈9644 tokens,构建32s+LLM45s,工厂运行偶发破 180s engine budget。
        # 减半 codes + 缩 lookback 直接砍 prompt 构建时间与 token 数。可 env 覆盖回大值。
        def _env_int(name: str, default: int) -> int:
            try:
                return max(1, int(str(os.getenv(name, default)).strip()))
            except Exception:
                return default

        max_codes = _env_int("FACTOR_LLM_PROMPT_MAX_CODES", 4)
        lookback_bars = _env_int("FACTOR_LLM_PROMPT_LOOKBACK_BARS", 120)
        codes = list(getattr(context, "codes", [])[:max_codes]) if hasattr(context, "codes") else []
        if not codes:
            codes = list(getattr(context, "validation_codes", [])[:max_codes])
        prompt = None
        try:
            prompt = await build_factor_mining_prompt(
                db,
                codes,
                candidate_count=budget.candidate_count,
                lookback_bars=lookback_bars,
                memory_context=self._memory_context(context),
            )
        except Exception as exc:
            logger.info("LLMSearchEngine: using blueprint fallback prompt: %s", exc)
        prompt = self._with_blueprint_context(prompt, context, budget, codes)

        # 调用 LLM
        result = await provider.generate_candidates(
            prompt,
            candidate_count=budget.candidate_count,
        )

        # 转换为 FactorCandidate
        candidates = []
        for item in result.get("candidates", []):
            candidates.append(FactorCandidate(
                name=item.get("name", ""),
                hypothesis=item.get("hypothesis", ""),
                economic_hypothesis=item.get("economic_hypothesis", item.get("hypothesis", "")),
                family=item.get("family", "custom"),
                factor_family=item.get("factor_family", item.get("family", "custom")),
                inputs=item.get("inputs", []),
                expression_dsl=item.get("expression_dsl", ""),
                expected_holding_period=item.get("expected_holding_period", 10),
                expected_horizon=item.get("expected_horizon", item.get("expected_holding_period", 10)),
                expected_regime=item.get("expected_regime", []),
                complexity_hint=item.get("complexity_hint", "medium"),
                novelty_rationale=item.get("novelty_rationale", ""),
                generation_engine=self.engine_id,
                blueprint_id=item.get("blueprint_id", ""),
                risk_exposure_hint=(
                    dict(item.get("risk_exposure_hint") or {})
                    if isinstance(item.get("risk_exposure_hint"), dict)
                    else {"raw": str(item.get("risk_exposure_hint") or "")}
                ),
                generation_trace={
                    "mode": "llm_generation_chain",
                    "provider": result.get("provider", ""),
                    "model": result.get("model", ""),
                },
            ))

        return candidates

    def _with_blueprint_context(
        self,
        prompt: Any,
        context: Any,
        budget: SearchBudget,
        codes: list[str],
    ) -> Any:
        blueprints = list(getattr(context, "alpha_blueprints", []) or [])[:10]
        if not getattr(prompt, "system_prompt", "") or not getattr(prompt, "user_prompt", ""):
            prompt = self._build_blueprint_fallback_prompt(
                context=context,
                budget=budget,
                codes=codes,
                blueprints=blueprints,
            )

        if blueprints:
            blueprint_block = json.dumps(blueprints, ensure_ascii=False, indent=2, default=str)
            prompt.user_prompt = (
                f"{prompt.user_prompt}\n\n"
                "Alpha blueprint seeds for this generation round:\n"
                f"{blueprint_block}\n\n"
                "Use these blueprints as finance-prior seeds. Mutate, combine, or refine them "
                "instead of starting from raw fields. Every candidate must include "
                "economic_hypothesis, factor_family, expected_horizon, risk_exposure_hint, "
                "and blueprint_id when derived from a seed."
            )
            request_payload = dict(getattr(prompt, "request_payload", None) or {})
            request_payload["alpha_blueprints"] = blueprints
            output_contract = dict(request_payload.get("output_contract") or {})
            fields = list(output_contract.get("candidate_fields") or [])
            fields.extend(
                [
                    "economic_hypothesis",
                    "factor_family",
                    "expected_horizon",
                    "risk_exposure_hint",
                    "blueprint_id",
                ]
            )
            output_contract["candidate_fields"] = list(dict.fromkeys(fields))
            request_payload["output_contract"] = output_contract
            prompt.request_payload = request_payload
            context_summary = dict(getattr(prompt, "context_summary", None) or {})
            context_summary["alpha_blueprints"] = blueprints
            context_summary["memory_context"] = self._memory_context(context)
            prompt.context_summary = context_summary
            source_chain = list(getattr(prompt, "source_chain", None) or [])
            source_chain.append("factor_mining_factory.alpha_blueprints")
            prompt.source_chain = list(dict.fromkeys(source_chain))
        return prompt

    def _build_blueprint_fallback_prompt(
        self,
        *,
        context: Any,
        budget: SearchBudget,
        codes: list[str],
        blueprints: list[dict[str, Any]],
    ) -> Any:
        from ...factor_candidate_compiler import (
            SUPPORTED_FACTOR_FIELDS,
            SUPPORTED_FACTOR_FUNCTIONS,
        )
        from ...factor_llm_provider import get_factor_candidate_schema_path
        from ...factor_prompt_builder import FactorMiningPrompt

        request_payload = {
            "task": "factor_candidate_generation_from_alpha_blueprints",
            "candidate_count": max(1, min(int(budget.candidate_count or 1), 16)),
            "codes": list(codes or [])[:8],
            "allowed_operators": sorted(SUPPORTED_FACTOR_FUNCTIONS),
            "field_hints": sorted(SUPPORTED_FACTOR_FIELDS),
            "alpha_blueprints": blueprints,
            "memory_context": self._memory_context(context),
            "dsl_contract": {
                "evaluation_semantics": [
                    "expression_dsl is evaluated on a single-stock daily time-series frame",
                    "rank/zscore/ts_* are time-series operators over one stock history",
                    "use only field_hints and allowed_operators",
                    "do not use future returns or lookahead fields",
                ],
                "quality_policy": [
                    "prefer candidates with clear economic hypotheses",
                    "prefer robust, testable mutations of the supplied blueprints",
                    "avoid one-field raw expressions and avoid redundant variants",
                    "optimize for strict admission, not quick IC only",
                    "avoid blueprint mutations likely to fail PBO, White Reality Check, or Hansen SPA",
                    "do not repeat memory_context failed patterns; use them as negative examples",
                ],
                "strict_admission_policy": {
                    "minimum_evidence": {
                        "sample_dates": 60,
                        "avg_cross_section_n": 80,
                        "ic_history_rows": 60,
                        "abs_rank_ic_mean": 0.025,
                        "rank_ic_ir": 0.25,
                        "positive_ratio": 0.52,
                    },
                    "governance": "multiple_testing risk must be low; PBO high or weak reality checks block admission",
                },
            },
            "output_contract": {
                "root_fields": ["candidates", "analysis", "warnings"],
                "candidate_fields": [
                    "name",
                    "hypothesis",
                    "economic_hypothesis",
                    "family",
                    "factor_family",
                    "inputs",
                    "expression_dsl",
                    "expected_holding_period",
                    "expected_horizon",
                    "expected_regime",
                    "complexity_hint",
                    "novelty_rationale",
                    "risk_exposure_hint",
                    "blueprint_id",
                ],
            },
        }
        system_prompt = (
            "You are a quantitative equity factor researcher. Return only JSON with "
            "root fields candidates, analysis, warnings. Generate factor candidates "
            "by mutating, combining, and refining the provided alpha_blueprints. "
            "Use only the DSL fields and functions in the request payload. "
            "Optimize for formal strict admission with low multiple-testing risk, not quick IC only."
        )
        user_prompt = (
            "Generate finance-prior factor candidates from this payload:\n"
            f"{json.dumps(request_payload, ensure_ascii=False, indent=2, default=str)}\n\n"
            "Do not include markdown or prose outside JSON. Before finalizing, verify that "
            "every expression_dsl is point-in-time safe and uses only allowed fields/functions. "
            "Avoid candidates that are merely close variants of common basis factors or failed memory patterns."
        )
        return FactorMiningPrompt(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_summary={
                "codes": list(codes or [])[:8],
                "candidate_count": request_payload["candidate_count"],
                "alpha_blueprints": blueprints,
                "memory_context": request_payload["memory_context"],
            },
            request_payload=request_payload,
            source_chain=["factor_mining_factory.llm_blueprint_fallback"],
            schema_path=get_factor_candidate_schema_path(),
        )

    @staticmethod
    def _memory_context(context: Any) -> dict[str, Any]:
        return {
            "successful_pattern_memory": list(
                getattr(context, "successful_pattern_memory", []) or []
            )[:20],
            "failed_pattern_memory": list(
                getattr(context, "failed_pattern_memory", []) or []
            )[:20],
        }

    async def _optimization_chain(
        self,
        candidates: list[FactorCandidate],
        context: Any,
    ) -> list[FactorCandidate]:
        """优化链：快速编译验证 + 沙箱前视检查，过滤无效候选。

        Wraps the core compile_factor_candidate with the FactorSandbox layer
        so LLM-generated expressions go through both AST whitelisting and
        the static lookahead detector. The sandbox accepts and produces the
        same dict shape, so no other engine layer needs to change. Failed
        candidates are dropped silently (per existing behavior); the sandbox
        warnings are stored on candidate.generation_trace for downstream
        diagnostics.
        """
        from ..sandbox.evaluator import FactorSandbox

        sandbox = FactorSandbox()
        valid = []
        for candidate in candidates:
            if not candidate.expression_dsl:
                continue
            try:
                compiled = sandbox.compile(candidate.to_validation_dict())
                if not compiled.get("valid"):
                    continue
                lookahead = sandbox.validate_no_lookahead(compiled)
                if lookahead.get("risk_level") == "high":
                    # high-risk lookahead patterns: drop the candidate to keep
                    # PIT-correctness guarantees and surface why in the trace.
                    continue
                candidate.generation_trace["compiled"] = True
                candidate.generation_trace["complexity_score"] = compiled.get("complexity", {}).get("score", 0)
                candidate.generation_trace["lookahead_risk"] = lookahead.get("risk_level")
                if lookahead.get("warnings"):
                    candidate.generation_trace["lookahead_warnings"] = lookahead["warnings"]
                valid.append(candidate)
            except Exception:
                continue

        return valid

    def get_status(self) -> EngineStatus:
        return EngineStatus(
            engine_id=self.engine_id,
            engine_type=self.engine_type,
            enabled=True,
            ready=True,
            last_run_at=self._last_run_at,
            last_error=self._last_error,
            success_count=self._success_count,
            failure_count=self._failure_count,
        )
