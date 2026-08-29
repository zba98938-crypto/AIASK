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

class _PipelineStageMixin:
    async def _call_llm_stage(
        self,
        stage_def: StageDefinition,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        """调用 LLM provider 执行单阶段。"""
        _timeout_sec, _timeouts = _get_pipeline_constants()
        stage_timeout = _timeouts.get(stage_def.stage_id, _timeout_sec)
        prepared_input = self._prepare_stage_input(stage_def.stage_id, input_data)
        return await self.provider.call_stage(
            stage_id=stage_def.stage_id,
            input_data=prepared_input,
            system_prompt=stage_def.system_prompt,
            max_tokens=stage_def.max_tokens,
            temperature=stage_def.temperature,
            timeout_sec=stage_timeout,
        )

    async def _call_fallback_stage(
        self,
        stage_def: StageDefinition,
        db: Any,
        input_data: dict[str, Any],
        snapshot: dict[str, Any],
        started: float,
        *,
        llm_attempted: bool,
        prompt_chars: int = 0,
        llm_error: Optional[str] = None,
        llm_error_type: Optional[str] = None,
        llm_error_metrics: Optional[dict[str, Any]] = None,
    ) -> StageResult:
        """执行 fallback 函数。"""
        if stage_def.fallback_fn is None:
            return StageResult(
                stage_id=stage_def.stage_id,
                output={},
                used_fallback=True,
                llm_attempted=llm_attempted,
                prompt_chars=prompt_chars,
                elapsed_sec=time.perf_counter() - started,
                error=f"no fallback for stage {stage_def.stage_id}",
                llm_error=llm_error,
                llm_error_type=llm_error_type,
                llm_error_metrics=dict(llm_error_metrics or {}),
            )
        try:
            output = await stage_def.fallback_fn(db, input_data, snapshot)
            elapsed = time.perf_counter() - started
            valid = validate_stage_output(stage_def.stage_id, output)
            fallback_metrics = dict(llm_error_metrics or {})
            if not valid:
                fallback_metrics.update(
                    {
                        "fallback_validation_failure_reason": _stage_validation_failure_reason(
                            stage_def.stage_id,
                            output,
                        ),
                        "fallback_output_keys": list(output.keys()) if isinstance(output, dict) else [],
                        "fallback_output_list_counts": _stage_output_list_counts(output),
                    }
                )
            return StageResult(
                stage_id=stage_def.stage_id,
                output=output if valid else {},
                used_fallback=True,
                llm_attempted=llm_attempted,
                prompt_chars=prompt_chars,
                elapsed_sec=elapsed,
                error=None if valid else f"fallback output failed validation for {stage_def.stage_id}",
                llm_error=llm_error,
                llm_error_type=llm_error_type,
                llm_error_metrics=fallback_metrics,
            )
        except Exception as exc:
            logger.error("Stage %s fallback failed: %s", stage_def.stage_id, exc)
            return StageResult(
                stage_id=stage_def.stage_id,
                output={},
                used_fallback=True,
                llm_attempted=llm_attempted,
                prompt_chars=prompt_chars,
                elapsed_sec=time.perf_counter() - started,
                error=f"fallback failed: {exc}",
                llm_error=llm_error,
                llm_error_type=llm_error_type,
                llm_error_metrics=dict(llm_error_metrics or {}),
            )

    @staticmethod
    async def _enrich_with_stock_profiles(
        db: Any,
        prepared_input: dict[str, Any],
    ) -> dict[str, Any]:
        """PR-AI4: 从 stock_profile_embeddings 取真实基本面数据注入 LLM 输入。

        让 LLM 看到目标股票的真实 PE/PB/ROE/营收增速/动量/波动率，
        而不是凭训练记忆编造。
        """
        # 收集所有 target_symbols
        target_symbols: list[str] = []
        for source in (
            prepared_input.get("exposures") or [],
            prepared_input.get("confirmations") or [],
        ):
            for item in list(source or []):
                if isinstance(item, dict):
                    syms = item.get("target_symbols") or []
                    if isinstance(syms, list):
                        target_symbols.extend(str(s).strip() for s in syms if str(s).strip())
                    sym = str(item.get("symbol") or "").strip()
                    if sym:
                        target_symbols.append(sym)
        research_task = dict(prepared_input.get("research_task") or {})
        for s in list(research_task.get("target_symbols") or []):
            code = str(s).strip()
            if code:
                target_symbols.append(code)
        # 去重保序
        seen: set[str] = set()
        unique_symbols: list[str] = []
        for s in target_symbols:
            if s not in seen:
                seen.add(s)
                unique_symbols.append(s)
        if not unique_symbols:
            return prepared_input

        list_vector_profiles = getattr(db, "list_vector_profiles", None)
        if not callable(list_vector_profiles):
            return prepared_input

        stock_fundamentals: list[dict[str, Any]] = []
        for code in unique_symbols[:6]:
            try:
                rows = await list_vector_profiles(
                    collection_name="stock_profile_embeddings",
                    stock_code=code,
                    profile_type="fundamental",
                    limit=1,
                )
                if rows:
                    import json as _json
                    meta = _json.loads(rows[0].get("metadata") or "{}")
                    raw = dict(meta.get("raw_features") or {})
                    stock_fundamentals.append({
                        "code": code,
                        "name": meta.get("stock_name") or "",
                        "industry": meta.get("industry") or "",
                        "pe": round(float(raw.get("pe_ratio") or 0), 2),
                        "pb": round(float(raw.get("pb_ratio") or 0), 2),
                        "roe": round(float(raw.get("roe") or 0), 4),
                        "revenue_growth": round(float(raw.get("revenue_growth") or 0), 4),
                        "profit_growth": round(float(raw.get("profit_growth") or 0), 4),
                        "momentum_20d": round(float(raw.get("momentum_20d") or 0), 4),
                        "volatility_20d": round(float(raw.get("volatility_20d") or 0), 4),
                    })
            except Exception:
                continue

        if stock_fundamentals:
            prepared_input["stock_fundamentals"] = stock_fundamentals
        return prepared_input

    @staticmethod
    def _build_initial_input(
        snapshot: dict[str, Any],
        research_task: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """构建 Stage 1 (event_recognition) 的输入。"""
        market_snapshot: dict[str, Any] = {}
        snapshot_date = snapshot.get("date") or snapshot.get("snapshot_date")
        if snapshot_date is not None:
            market_snapshot["date"] = snapshot_date

        fear_greed = snapshot.get("fear_greed")
        if fear_greed is None:
            fear_greed = snapshot.get("fear_greed_index")
        if fear_greed is not None:
            market_snapshot["fear_greed"] = fear_greed
            market_snapshot["fear_greed_index"] = snapshot.get("fear_greed_index", fear_greed)

        sentiment = snapshot.get("sentiment") or snapshot.get("fg_level")
        if sentiment is not None:
            market_snapshot["sentiment"] = sentiment

        sector_data = snapshot.get("hot_sectors") or snapshot.get("sectors") or []
        if isinstance(sector_data, list):
            market_snapshot["sectors"] = sector_data[:20]

        north_fund = snapshot.get("north_fund")
        if not north_fund and snapshot.get("north_fund_3d_net") is not None:
            north_fund = {"net_3d": snapshot.get("north_fund_3d_net")}
        if not north_fund:
            north_fund = snapshot.get("capital_flow") or {}
        if north_fund:
            market_snapshot["north_fund"] = north_fund

        dragon_tiger = snapshot.get("dragon_tiger") or []
        if isinstance(dragon_tiger, list):
            market_snapshot["dragon_tiger_summary"] = dragon_tiger[:10]

        theme_directory = [
            {
                "theme_code": t["theme_code"],
                "name": t["name"],
                "aliases": list(t.get("aliases") or [])[:6],
                "parent": t.get("parent"),
            }
            for t in EXTENDED_THEME_LIBRARY
        ]
        matched_theme_candidates = _match_theme_candidates(snapshot)

        initial: dict[str, Any] = {
            "market_snapshot": market_snapshot,
            "theme_library": theme_directory,
        }
        if matched_theme_candidates:
            initial["matched_theme_candidates"] = matched_theme_candidates[:8]
        event_detection_hints = {
            "hot_sector_names": _extract_named_market_hints(snapshot)[:8],
            "north_fund_bias": (
                "inflow"
                if float((((market_snapshot.get("north_fund") or {}).get("net_inflow") or 0) or 0)) > 0
                else "neutral_or_outflow"
            ) if market_snapshot.get("north_fund") else None,
        }
        if any(value for value in event_detection_hints.values()):
            initial["event_detection_hints"] = event_detection_hints

        factor_research = dict(snapshot.get("factor_research") or {})
        if factor_research:
            initial["factor_research"] = {
                "active_factors": list(factor_research.get("active_factors") or [])[:3],
                "top_factor_names": list(((factor_research.get("summary") or {}).get("top_factor_names") or []))[:3],
                "preferred_strategy_types": list(factor_research.get("preferred_strategy_types") or [])[:4],
                "degraded": bool(factor_research.get("degraded")),
            }

        if research_task:
            initial["research_task"] = {
                k: research_task[k]
                for k in (
                    "task_id",
                    "task_key",
                    "task_source",
                    "theme_code",
                    "direction",
                    "opportunity_type",
                    "target_symbols",
                    "strategy_preferences",
                    "preferred_strategy_types",
                    "allowed_strategy_types",
                    "target_symbol_policy",
                    "universe_expansion_policy",
                    "preference_strength",
                    "preference_reason",
                    "validation_focus",
                    "factor_name",
                    "candidate_family",
                    "candidate_name",
                    "rationale",
                    "expected_regime",
                    "validation_score",
                    "template_generation_profile",
                    "generation_limit",
                )
                if k in research_task
            }

        return initial

    @staticmethod
    def _prepare_stage_input(stage_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """按阶段压缩输入，降低 LLM 延迟和超时概率。"""
        if stage_id == "event_recognition":
            prepared: dict[str, Any] = {}

            market_snapshot = dict(input_data.get("market_snapshot") or {})
            if market_snapshot:
                prepared["market_snapshot"] = {
                    key: market_snapshot.get(key)
                    for key in (
                        "date",
                        "fear_greed",
                        "fear_greed_index",
                        "sentiment",
                        "sectors",
                        "north_fund",
                        "dragon_tiger_summary",
                    )
                    if key in market_snapshot
                }
                if isinstance(prepared["market_snapshot"].get("sectors"), list):
                    compact_sectors: list[str] = []
                    for item in prepared["market_snapshot"]["sectors"][:6]:
                        if isinstance(item, dict):
                            compact_sectors.append(str(item.get("group") or item.get("name") or "").strip())
                        else:
                            compact_sectors.append(str(item).strip())
                    prepared["market_snapshot"]["sectors"] = [item for item in compact_sectors if item]
                if isinstance(prepared["market_snapshot"].get("north_fund"), dict):
                    prepared["market_snapshot"]["north_fund"] = {
                        key: prepared["market_snapshot"]["north_fund"].get(key)
                        for key in ("net_inflow", "net_3d")
                        if prepared["market_snapshot"]["north_fund"].get(key) is not None
                    }
                if isinstance(prepared["market_snapshot"].get("dragon_tiger_summary"), list):
                    prepared["market_snapshot"]["dragon_tiger_summary"] = [
                        str(item.get("name") or item.get("code") or "").strip()
                        for item in prepared["market_snapshot"]["dragon_tiger_summary"][:4]
                        if isinstance(item, dict)
                    ]
                    prepared["market_snapshot"]["dragon_tiger_summary"] = [
                        item for item in prepared["market_snapshot"]["dragon_tiger_summary"] if item
                    ]

            hints = dict(input_data.get("event_detection_hints") or {})
            if hints:
                prepared["event_detection_hints"] = hints

            matched_theme_candidates = list(input_data.get("matched_theme_candidates") or [])
            if matched_theme_candidates:
                prepared["matched_theme_candidates"] = matched_theme_candidates[:6]

            research_task = dict(input_data.get("research_task") or {})
            if research_task:
                prepared["research_task"] = {
                    key: research_task.get(key)
                    for key in (
                        "task_id",
                        "task_key",
                        "task_source",
                        "theme_code",
                        "direction",
                        "opportunity_type",
                        "target_symbols",
                        "preferred_strategy_types",
                        "allowed_strategy_types",
                        "preference_reason",
                        "validation_focus",
                        "factor_name",
                        "candidate_family",
                        "candidate_name",
                        "expected_regime",
                        "validation_score",
                        "template_generation_profile",
                    )
                    if key in research_task
                }

            theme_library = list(input_data.get("theme_library") or [])
            prioritized_codes = [
                str(item.get("theme_code") or "").strip()
                for item in matched_theme_candidates[:6]
                if str(item.get("theme_code") or "").strip()
            ]
            selected_theme_library: list[dict[str, Any]] = []
            seen_theme_codes: set[str] = set()
            for code in prioritized_codes:
                for item in theme_library:
                    item_code = str((item or {}).get("theme_code") or "").strip()
                    if item_code != code or item_code in seen_theme_codes:
                        continue
                    seen_theme_codes.add(item_code)
                    selected_theme_library.append(item)
                    break
            if not prioritized_codes:
                for item in theme_library:
                    item_code = str((item or {}).get("theme_code") or "").strip()
                    if not item_code or item_code in seen_theme_codes:
                        continue
                    seen_theme_codes.add(item_code)
                    selected_theme_library.append(item)
                    if len(selected_theme_library) >= 4:
                        break
            if selected_theme_library:
                prepared["theme_library"] = selected_theme_library[:6]
            return prepared

        if stage_id != "strategy_generation":
            return input_data

        prepared: dict[str, Any] = {}

        market_snapshot = dict(input_data.get("market_snapshot") or {})
        if market_snapshot:
            prepared["market_snapshot"] = {
                key: market_snapshot.get(key)
                for key in ("date", "fear_greed", "sentiment", "north_fund")
                if key in market_snapshot
            }

        research_task = dict(input_data.get("research_task") or {})
        if research_task:
            prepared["research_task"] = {
                key: research_task.get(key)
                for key in (
                    "task_id",
                    "task_key",
                    "task_source",
                    "theme_code",
                    "direction",
                    "target_symbols",
                    "strategy_preferences",
                    "preferred_strategy_types",
                    "allowed_strategy_types",
                    "target_symbol_policy",
                    "universe_expansion_policy",
                    "preference_strength",
                    "preference_reason",
                    "validation_focus",
                    "opportunity_type",
                    "factor_name",
                    "candidate_family",
                    "candidate_name",
                    "rationale",
                    "expected_regime",
                    "validation_score",
                    "template_generation_profile",
                    "generation_limit",
                    "horizon",
                )
                if key in research_task
            }

        themes = list(input_data.get("themes") or [])
        if themes:
            prepared["themes"] = [
                {
                    "theme_code": item.get("theme_code"),
                    "theme_name": item.get("theme_name"),
                    "direction": item.get("direction"),
                    "confidence": item.get("confidence"),
                }
                for item in themes[:3]
            ]

        exposures = list(input_data.get("exposures") or [])
        if exposures:
            prepared["exposures"] = [
                {
                    "theme_code": item.get("theme_code"),
                    "target_symbols": list(item.get("target_symbols") or [])[:6],
                    "sector": item.get("sector"),
                    "exposure_type": item.get("exposure_type"),
                    "weight": item.get("weight"),
                }
                for item in exposures[:4]
            ]

        confirmations = list(input_data.get("confirmations") or [])
        if confirmations:
            prepared["confirmations"] = [
                {
                    "theme_code": item.get("theme_code"),
                    "symbol": item.get("symbol"),
                    "confirmed": item.get("confirmed"),
                    "signal_strength": item.get("signal_strength"),
                    "entry_timing": item.get("entry_timing"),
                    "risk_level": item.get("risk_level"),
                }
                for item in confirmations[:8]
            ]

        return prepared or input_data
