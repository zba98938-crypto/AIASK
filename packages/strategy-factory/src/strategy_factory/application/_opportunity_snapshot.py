"""Snapshot-driven task builders for market opportunity scanning."""

from __future__ import annotations

from typing import Any, Dict, List

from ..domain.constants import AUTONOMY_CANDIDATES_PER_TASK, OPPORTUNITY_TARGET_SYMBOLS_PER_TASK, preferred_strategy_types_for_factor


class _MarketOpportunityScannerSnapshotMixin:
    _CONSERVATIVE_MEAN_REVERSION_TOKENS = (
        "closelocation",
        "intradayresilience",
        "trendefficiency",
        "pullback",
        "quality",
        "stability",
        "quiet",
        "resilience",
        "repair",
        "reversion",
        "defensive",
    )
    _CONSERVATIVE_FLOW_TOKENS = (
        "capitalflow",
        "northcapital",
        "northbound",
        "fundflow",
        "liquidity",
        "turnover",
    )
    _CONSERVATIVE_ROTATION_TOKENS = (
        "rotation",
        "sector",
        "cycle",
        "divergence",
        "breadth",
    )
    _CONSERVATIVE_BREAKOUT_TOKENS = (
        "momentum",
        "macross",
        "cross",
        "trend",
        "breakout",
        "gapcontinuation",
        "expansion",
        "acceleration",
        "volatility",
    )
    _PIPELINE_TEMPLATE_ALLOWED_TYPES = frozenset(
        {
            "ma_cross",
            "rsi",
            "gap_fill",
            "mean_reversion_short",
            "volatility_breakout",
            "event_structure_breakout",
            "sector_rotation",
            "north_capital_track",
            "margin_divergence",
        }
    )
    _HIGH_VOL_GROWTH_TYPES = (
        "volatility_breakout",
        "event_structure_breakout",
        "gap_fill",
        "mean_reversion_short",
        "quality_factor",
    )
    _LOW_VOL_DEFENSIVE_TYPES = (
        "quality_factor",
        "north_capital_track",
        "sector_rotation",
        "ma_cross",
    )
    _CYCLE_RESOURCE_TYPES = (
        "sector_rotation",
        "north_capital_track",
        "ma_cross",
        "quality_factor",
    )
    _DEFENSIVE_POOL_TOKENS = (
        "银行",
        "保险",
        "电力",
        "核电",
        "公用",
        "运营商",
        "高股息",
        "红利",
        "消费",
    )
    _CYCLE_POOL_TOKENS = (
        "油气",
        "煤炭",
        "有色",
        "钢铁",
        "化工",
        "资源",
        "矿业",
        "黄金",
        "铜",
        "铝",
        "上游",
        "周期",
    )
    _GROWTH_POOL_TOKENS = (
        "电子",
        "通信",
        "计算机",
        "软件",
        "半导体",
        "算力",
        "ai",
        "新能源",
        "医药",
        "元器件",
    )

    @classmethod
    def _governed_regime_preferences(cls, regime_name: str, *, fear_greed: float) -> List[str]:
        lowered = str(regime_name or "").strip().lower()
        if any(token in lowered for token in ["trend", "breakout", "momentum", "risk_on"]):
            return ["momentum", "ma_cross", "growth_factor"]
        if any(token in lowered for token in ["mean_reversion", "reversal", "oversold", "risk_off", "defensive"]):
            return ["rsi", "value_factor", "quality_factor"]
        if any(token in lowered for token in ["event", "news", "announcement"]):
            return ["macro_timing", "momentum", "multi_factor"]
        if any(token in lowered for token in ["volatility", "shock"]):
            return ["macro_timing", "rsi", "value_factor"]
        if any(token in lowered for token in ["rotation", "range", "sideways", "neutral"]):
            return ["ma_cross", "quality_factor", "multi_factor"]
        if fear_greed >= 60:
            return ["momentum", "ma_cross", "growth_factor"]
        if fear_greed <= 40:
            return ["rsi", "value_factor", "quality_factor"]
        return ["ma_cross", "quality_factor", "growth_factor"]

    @staticmethod
    def _normalized_family_hint(value: Any) -> str:
        return (
            str(value or "")
            .strip()
            .lower()
            .replace(" ", "")
            .replace("_", "")
            .replace("-", "")
        )

    @classmethod
    def _governed_family_generation_contract(
        cls,
        family_name: str,
        *,
        base_preferences: List[str],
    ) -> dict[str, Any]:
        normalized = cls._normalized_family_hint(family_name)
        if not normalized:
            return {
                "preferred_strategy_types": list(base_preferences),
                "allowed_strategy_types": [],
                "template_generation_profile": None,
            }

        if any(token in normalized for token in cls._CONSERVATIVE_MEAN_REVERSION_TOKENS):
            preferred = ["rsi", "gap_fill", "ma_cross"]
            profile = "conservative_mean_reversion"
        elif any(token in normalized for token in cls._CONSERVATIVE_FLOW_TOKENS):
            preferred = ["north_capital_track", "sector_rotation", "ma_cross"]
            profile = "conservative_flow"
        elif any(token in normalized for token in cls._CONSERVATIVE_ROTATION_TOKENS):
            preferred = ["sector_rotation", "margin_divergence", "ma_cross"]
            profile = "conservative_rotation"
        elif any(token in normalized for token in cls._CONSERVATIVE_BREAKOUT_TOKENS):
            preferred = ["event_structure_breakout", "volatility_breakout", "ma_cross", "sector_rotation"]
            profile = "conservative_breakout"
        else:
            return {
                "preferred_strategy_types": list(base_preferences),
                "allowed_strategy_types": [],
                "template_generation_profile": None,
            }

        ordered = list(dict.fromkeys([*preferred, *list(base_preferences or [])]))
        ordered = [item for item in ordered if item != "momentum"]
        allowed = [item for item in ordered if item in cls._PIPELINE_TEMPLATE_ALLOWED_TYPES]
        return {
            "preferred_strategy_types": ordered[:5],
            "allowed_strategy_types": allowed[:5],
            "template_generation_profile": profile,
        }

    @classmethod
    def _rows_for_task_targets(
        cls,
        task: dict[str, Any],
        *,
        rows_by_code: Dict[str, dict[str, Any]],
        fallback_rows: List[dict[str, Any]],
    ) -> List[dict[str, Any]]:
        target_rows: List[dict[str, Any]] = []
        for code in list(task.get("target_symbols") or []):
            row = dict(rows_by_code.get(str(code).strip()) or {})
            if row:
                target_rows.append(row)
        return target_rows or list(fallback_rows or [])

    @classmethod
    def _mean_metric(
        cls,
        rows: List[dict[str, Any]],
        *keys: str,
    ) -> float | None:
        values: List[float] = []
        for row in list(rows or []):
            for key in keys:
                value = row.get(key)
                if value in (None, "", [], {}):
                    continue
                metric = cls._safe_float(value)
                if metric > 0:
                    values.append(metric)
                    break
        if not values:
            return None
        return round(sum(values) / len(values), 4)

    @classmethod
    def _infer_pool_profile(
        cls,
        task: dict[str, Any],
        *,
        task_rows: List[dict[str, Any]],
        hot_sectors: List[str],
        cold_sectors: List[str],
    ) -> dict[str, Any]:
        industries = [
            str(item).strip()
            for item in list(task.get("focus_industries") or [])
            if str(item).strip()
        ]
        if not industries:
            industries = cls._focus_industries_from_rows(task_rows, limit=3)
        haystack = " ".join(
            [
                *industries,
                str(task.get("title") or ""),
                str(task.get("theme") or ""),
                str(task.get("candidate_family") or ""),
                str(task.get("factor_name") or ""),
                str(task.get("candidate_name") or ""),
                str(task.get("rationale") or ""),
            ]
        ).strip().lower()
        avg_atr = cls._mean_metric(task_rows, "atr60_pct", "atr20_pct", "atr14_pct", "atr_pct")
        avg_turnover = cls._mean_metric(task_rows, "turnover_rate", "turnover")
        avg_volume_ratio = cls._mean_metric(task_rows, "volume_ratio", "vol_ratio", "量比")
        profile = "high_vol_growth"
        reason = "growth_style_default"
        if any(token.lower() in haystack for token in cls._DEFENSIVE_POOL_TOKENS):
            profile = "low_vol_defensive"
            reason = "defensive_industry_hint"
        elif any(token.lower() in haystack for token in cls._CYCLE_POOL_TOKENS):
            profile = "cycle_resource"
            reason = "cycle_industry_hint"
        elif avg_atr is not None and avg_atr >= 0.045:
            profile = "high_vol_growth"
            reason = "atr_high"
        elif avg_atr is not None and avg_atr <= 0.025:
            profile = "low_vol_defensive"
            reason = "atr_low"
        elif avg_turnover is not None and avg_turnover <= 1.2 and not any(
            token.lower() in haystack for token in cls._GROWTH_POOL_TOKENS
        ):
            profile = "low_vol_defensive"
            reason = "turnover_low"
        elif any(token.lower() in haystack for token in cls._GROWTH_POOL_TOKENS):
            profile = "high_vol_growth"
            reason = "growth_industry_hint"

        volatility_bucket = (
            "high"
            if profile == "high_vol_growth"
            else "low"
            if profile == "low_vol_defensive"
            else "medium_high"
        )
        liquidity_bucket = (
            "stable_low_velocity"
            if profile == "low_vol_defensive"
            else "high_liquidity"
            if (avg_turnover or 0.0) >= 1.0
            else "selective_liquidity"
        )
        breakout_allowed = profile == "high_vol_growth" or (
            profile == "cycle_resource" and (avg_volume_ratio or 0.0) >= 1.05
        )
        if profile == "high_vol_growth":
            preferred = list(cls._HIGH_VOL_GROWTH_TYPES)
            allowed = list(cls._HIGH_VOL_GROWTH_TYPES)
        elif profile == "low_vol_defensive":
            preferred = list(cls._LOW_VOL_DEFENSIVE_TYPES)
            allowed = list(cls._LOW_VOL_DEFENSIVE_TYPES)
        else:
            preferred = list(cls._CYCLE_RESOURCE_TYPES)
            allowed = list(cls._CYCLE_RESOURCE_TYPES)
            if breakout_allowed:
                preferred.extend(["event_structure_breakout", "volatility_breakout"])
                allowed.extend(["event_structure_breakout", "volatility_breakout"])
        family_mix_constraints = {
            "trend_cluster_types": ["momentum", "ma_cross", "volatility_breakout", "event_structure_breakout", "sector_rotation"],
            "trend_cluster_max_per_task": 2,
            "require_coverage": ["quality_defensive", "mean_reversion", "flow_rotation"],
            "breakout_requires_volume_expansion": profile == "cycle_resource",
            "quota_fill_momentum_allowed": False,
        }
        return {
            "pool_profile": profile,
            "pool_profile_reason": reason,
            "volatility_bucket": volatility_bucket,
            "liquidity_bucket": liquidity_bucket,
            "preferred_strategy_types": preferred,
            "allowed_strategy_types": allowed,
            "breakout_allowed": breakout_allowed,
            "family_mix_constraints": family_mix_constraints,
        }

    @classmethod
    def _apply_pool_profile_contract(
        cls,
        task: dict[str, Any],
        *,
        task_rows: List[dict[str, Any]],
        hot_sectors: List[str],
        cold_sectors: List[str],
    ) -> dict[str, Any]:
        normalized = dict(cls._finalize_task(task))
        profile_contract = cls._infer_pool_profile(
            normalized,
            task_rows=task_rows,
            hot_sectors=hot_sectors,
            cold_sectors=cold_sectors,
        )
        preferred = list(
            dict.fromkeys(
                [
                    *list(profile_contract.get("preferred_strategy_types") or []),
                    *list(normalized.get("preferred_strategy_types") or []),
                ]
            )
        )
        allowed = list(
            dict.fromkeys(
                [
                    *list(normalized.get("allowed_strategy_types") or []),
                    *list(profile_contract.get("allowed_strategy_types") or []),
                ]
            )
        )
        pool_profile = str(profile_contract.get("pool_profile") or "high_vol_growth")
        if pool_profile == "high_vol_growth":
            preferred = [item for item in preferred if item not in {"momentum", "ma_cross"}]
            allowed = [item for item in allowed if item not in {"momentum", "ma_cross"}]
        elif pool_profile in {"low_vol_defensive", "cycle_resource"}:
            if "ma_cross" not in preferred:
                preferred.append("ma_cross")
            if "ma_cross" not in allowed:
                allowed.append("ma_cross")
        normalized["preferred_strategy_types"] = preferred[:6]
        normalized["strategy_preferences"] = list(normalized["preferred_strategy_types"])
        normalized["allowed_strategy_types"] = [
            item for item in allowed[:8]
            if item in cls._PIPELINE_TEMPLATE_ALLOWED_TYPES or item in {"quality_factor", "growth_factor", "value_factor"}
        ]
        normalized["pool_profile"] = pool_profile
        normalized["volatility_bucket"] = profile_contract.get("volatility_bucket")
        normalized["liquidity_bucket"] = profile_contract.get("liquidity_bucket")
        normalized["family_mix_constraints"] = dict(profile_contract.get("family_mix_constraints") or {})
        normalized["pool_profile_reason"] = profile_contract.get("pool_profile_reason")
        normalized["pool_profile_industries"] = industries = cls._focus_industries_from_rows(task_rows, limit=3)
        if industries and not normalized.get("focus_industries"):
            normalized["focus_industries"] = industries
        return normalized

    @classmethod
    def _build_snapshot_tasks(cls, snapshot: dict[str, Any], rows: List[dict]) -> List[dict]:
        tasks: List[dict] = []
        target_selection_pressure: Dict[str, float] = {}
        fg = float(snapshot.get("fear_greed_index") or 50.0)
        factor_research = dict(snapshot.get("factor_research") or {})
        active_candidate_pool = dict(factor_research.get("active_candidate_pool") or {})
        governed_family_summary = [
            dict(item or {})
            for item in list(
                factor_research.get("active_family_summary")
                or active_candidate_pool.get("family_summary")
                or []
            )
            if isinstance(item, dict)
        ]
        governed_regime_summary = [
            dict(item or {})
            for item in list(
                factor_research.get("active_regime_summary")
                or active_candidate_pool.get("regime_summary")
                or []
            )
            if isinstance(item, dict)
        ]
        governed_top_candidates = [
            dict(item or {})
            for item in list(
                factor_research.get("governed_candidates")
                or active_candidate_pool.get("top_candidates")
                or []
            )
            if isinstance(item, dict)
        ]
        hot_sectors = [str(item).strip() for item in list(snapshot.get("hot_sectors") or []) if str(item).strip()]
        cold_sectors = [str(item).strip() for item in list(snapshot.get("cold_sectors") or []) if str(item).strip()]
        rows_by_code = cls._rows_by_code(rows)

        regime_preferences = (
            ["momentum", "ma_cross", "growth_factor"]
            if fg >= 60
            else (
                ["rsi", "value_factor", "quality_factor"]
                if fg <= 40
                else ["ma_cross", "quality_factor", "growth_factor"]
            )
        )
        regime_type = "trend_expansion" if fg >= 60 else ("mean_reversion" if fg <= 40 else "rotation_balanced")
        primary_governed_regime = dict(governed_regime_summary[0] or {}) if governed_regime_summary else {}
        primary_governed_regime_name = str(primary_governed_regime.get("regime") or "").strip()
        if primary_governed_regime_name:
            regime_preferences = list(
                dict.fromkeys(
                    [
                        *cls._governed_regime_preferences(primary_governed_regime_name, fear_greed=fg),
                        *regime_preferences,
                    ]
                )
            )[:4]
        regime_rationale = f"恐贪指数 {fg:.1f} 对应 {regime_type}，自动寻找适配交易结构。"
        if primary_governed_regime_name:
            regime_rationale = (
                f"{regime_rationale[:-1]}，治理后候选池主导 regime="
                f"{primary_governed_regime_name} (count={int(primary_governed_regime.get('count') or 0)})。"
            )

        def _select_snapshot_targets(
            task_key: str,
            task_rows: List[dict],
            *,
            hot: List[str] | None = None,
            cold: List[str] | None = None,
        ) -> List[str]:
            selected = cls._factor_scored_top_codes(
                task_rows,
                limit=OPPORTUNITY_TARGET_SYMBOLS_PER_TASK,
                snapshot=snapshot,
                hot_sectors=hot or hot_sectors,
                cold_sectors=cold or cold_sectors,
                selection_pressure=target_selection_pressure,
                task_seed=task_key,
                candidate_pool_limit=max(OPPORTUNITY_TARGET_SYMBOLS_PER_TASK * 4, 24),
            )
            increment = 1.0 if len(selected) <= 1 else (0.75 if len(selected) <= 3 else 0.5)
            for code in selected:
                target_selection_pressure[code] = round(
                    float(target_selection_pressure.get(code, 0.0)) + increment,
                    4,
                )
            return selected

        def _snapshot_task(task_payload: dict[str, Any], task_rows: List[dict[str, Any]]) -> dict[str, Any]:
            provisional = dict(task_payload)
            finalized = cls._finalize_task(provisional)
            focus_rows = cls._rows_for_task_targets(
                finalized,
                rows_by_code=rows_by_code,
                fallback_rows=task_rows,
            )
            return cls._apply_pool_profile_contract(
                finalized,
                task_rows=focus_rows,
                hot_sectors=hot_sectors,
                cold_sectors=cold_sectors,
            )

        tasks.append(
            _snapshot_task(
                {
                    "task_id": f"regime_{snapshot.get('date')}_{int(fg)}",
                    "task_key": f"regime:{snapshot.get('date')}:{regime_type}",
                    "task_source": "snapshot",
                    "theme": f"market_regime_{regime_type}",
                    "title": f"市场状态任务·{regime_type}",
                    "opportunity_type": regime_type,
                    "rationale": regime_rationale,
                    "preferred_strategy_types": regime_preferences,
                    "strategy_preferences": regime_preferences,
                    "target_symbol_policy": "prefer_intersection",
                    "universe_expansion_policy": "allow_market_fallback",
                    "preference_strength": "soft",
                    "preference_reason": (
                        f"snapshot_regime:fear_greed={fg:.1f};regime={regime_type}"
                        + (
                            f";governed={primary_governed_regime_name}"
                            if primary_governed_regime_name
                            else ""
                        )
                    ),
                    "validation_focus": "target_plus_representative",
                    "target_symbols": _select_snapshot_targets(
                        f"regime:{snapshot.get('date')}:{regime_type}",
                        rows,
                    ),
                    "focus_industries": hot_sectors[:2] if hot_sectors else [],
                    "focus_markets": [],
                    "priority": 100,
                    "generation_limit": cls._snapshot_generation_limit(AUTONOMY_CANDIDATES_PER_TASK, priority=100),
                    "source_snapshot": {
                        "fear_greed_index": fg,
                        "fg_level": snapshot.get("fg_level"),
                        "governed_regime": primary_governed_regime_name or None,
                        "governed_regime_count": int(primary_governed_regime.get("count") or 0),
                    },
                },
                rows,
            )
        )

        for idx, sector in enumerate(hot_sectors[:3], 1):
            matched = cls._match_rows(rows, [sector])
            priority = 90 - idx
            tasks.append(
                _snapshot_task(
                    {
                        "task_id": f"hot_{idx}_{sector}",
                        "task_key": f"hot_sector:{snapshot.get('date')}:{sector}",
                        "task_source": "snapshot",
                        "theme": f"hot_sector_{sector}",
                        "title": f"热点行业任务·{sector}",
                        "opportunity_type": "sector_breakout",
                        "rationale": f"热点行业 {sector} 在快照中活跃，自动生成行业内趋势/回踩/龙头轮动策略。",
                        "preferred_strategy_types": ["momentum", "ma_cross", "growth_factor"],
                        "strategy_preferences": ["momentum", "ma_cross", "growth_factor"],
                        "target_symbol_policy": "prefer_intersection",
                        "universe_expansion_policy": "allow_market_fallback",
                        "preference_strength": "soft",
                        "preference_reason": f"sector_heat:hot={','.join(hot_sectors[:2]) or 'none'}",
                        "validation_focus": "target_plus_representative",
                        "focus_industries": [sector],
                        "target_symbols": _select_snapshot_targets(
                            f"hot_sector:{snapshot.get('date')}:{sector}",
                            matched or rows,
                            hot=hot_sectors,
                            cold=cold_sectors,
                        ),
                        "focus_markets": [],
                        "priority": priority,
                        "generation_limit": cls._snapshot_generation_limit(AUTONOMY_CANDIDATES_PER_TASK, priority=priority),
                    },
                    matched or rows,
                )
            )

        for idx, sector in enumerate(cold_sectors[:2], 1):
            matched = cls._match_rows(rows, [sector])
            priority = 72 - idx
            tasks.append(
                _snapshot_task(
                    {
                        "task_id": f"cold_{idx}_{sector}",
                        "task_key": f"cold_sector:{snapshot.get('date')}:{sector}",
                        "task_source": "snapshot",
                        "theme": f"cold_sector_{sector}",
                        "title": f"冷门修复任务·{sector}",
                        "opportunity_type": "oversold_repair",
                        "rationale": f"冷门行业 {sector} 进入观察名单，自动寻找超跌修复与估值修复机会。",
                        "preferred_strategy_types": ["value_factor", "quality_factor", "rsi"],
                        "strategy_preferences": ["value_factor", "quality_factor", "rsi"],
                        "target_symbol_policy": "prefer_intersection",
                        "universe_expansion_policy": "allow_market_fallback",
                        "preference_strength": "soft",
                        "preference_reason": f"sector_cold:cold={','.join(cold_sectors[:2]) or 'none'}",
                        "validation_focus": "target_plus_representative",
                        "focus_industries": [sector],
                        "target_symbols": _select_snapshot_targets(
                            f"cold_sector:{snapshot.get('date')}:{sector}",
                            matched or rows,
                            hot=hot_sectors,
                            cold=[sector],
                        ),
                        "focus_markets": [],
                        "priority": priority,
                        "generation_limit": cls._snapshot_generation_limit(AUTONOMY_CANDIDATES_PER_TASK, priority=priority),
                    },
                    matched or rows,
                )
            )

        for idx, family_item in enumerate(governed_family_summary[:2], 1):
            family_name = str(family_item.get("family") or "").strip()
            if not family_name:
                continue
            family_lineage = [
                {
                    "artifact_id": str(item.get("artifact_id") or "").strip() or None,
                    "name": str(item.get("name") or "").strip() or None,
                    "registry_stage": str(item.get("registry_stage") or "").strip() or None,
                    "expected_regime": [
                        str(value).strip()
                        for value in list(item.get("expected_regime") or [])
                        if str(value).strip()
                    ],
                    "expected_holding_period": item.get("expected_holding_period"),
                    "source_generation_artifact_id": str(item.get("source_generation_artifact_id") or "").strip() or None,
                    "source_validation_artifact_id": str(item.get("source_validation_artifact_id") or item.get("artifact_id") or "").strip() or None,
                    "latest_validation_at": item.get("latest_validation_at"),
                    "latest_validation_age_days": item.get("latest_validation_age_days"),
                }
                for item in governed_top_candidates
                if str(item.get("family") or "").strip() == family_name
            ][:3]
            family_base_preferences = preferred_strategy_types_for_factor(
                family_name,
                default=regime_preferences or ["ma_cross", "momentum"],
            )
            family_contract = cls._governed_family_generation_contract(
                family_name,
                base_preferences=family_base_preferences,
            )
            family_preferences = list(family_contract.get("preferred_strategy_types") or family_base_preferences)
            family_rows = cls._focus_rows_for_family(
                rows,
                family_name=family_name,
                hot_sectors=hot_sectors,
                cold_sectors=cold_sectors,
            )
            focus_industries = cls._focus_industries_from_rows(family_rows) or hot_sectors[:1]
            avg_total_score = cls._safe_float(family_item.get("avg_total_score"))
            count = int(family_item.get("count") or 0)
            priority = min(
                98,
                max(
                    81,
                    int(round(78 + avg_total_score * 0.14 + min(count, 5) * 1.5)) - idx,
                ),
            )
            tasks.append(
                _snapshot_task(
                    {
                        "task_id": f"governed_family_{idx}_{family_name}",
                        "task_key": f"governed_family:{snapshot.get('date')}:{family_name}",
                        "task_source": "snapshot",
                        "theme": f"factor_family_{family_name}",
                        "title": f"候选因子族任务·{family_name}",
                        "opportunity_type": "candidate_family_activation",
                        "rationale": (
                            f"治理后候选池显示 family={family_name} 活跃，"
                            f"count={count}，avg_score={avg_total_score:.1f}，"
                            "优先围绕该 family 组织策略生成。"
                        ),
                        "preferred_strategy_types": family_preferences,
                        "strategy_preferences": family_preferences,
                        "allowed_strategy_types": list(family_contract.get("allowed_strategy_types") or []),
                        "target_symbol_policy": "prefer_intersection",
                        "universe_expansion_policy": "allow_market_fallback",
                        "preference_strength": "medium",
                        "preference_reason": (
                            f"governed_family:{family_name};avg_score={avg_total_score:.1f};count={count}"
                            + (
                                f";template_profile={family_contract.get('template_generation_profile')}"
                                if family_contract.get("template_generation_profile")
                                else ""
                            )
                        ),
                        "validation_focus": "candidate_target_only",
                        "focus_industries": focus_industries,
                        "target_symbols": _select_snapshot_targets(
                            f"governed_family:{snapshot.get('date')}:{family_name}",
                            family_rows,
                            hot=hot_sectors,
                            cold=cold_sectors,
                        ),
                        "focus_markets": [],
                        "priority": priority,
                        "generation_limit": cls._snapshot_generation_limit(
                            AUTONOMY_CANDIDATES_PER_TASK,
                            priority=priority,
                        ),
                        "factor_name": family_name,
                        "candidate_family": family_name,
                        "template_generation_profile": family_contract.get("template_generation_profile"),
                        "validation_score": round(avg_total_score, 4),
                        "expected_regime": [
                            str(item.get("regime") or "").strip()
                            for item in governed_regime_summary[:2]
                            if str(item.get("regime") or "").strip()
                        ],
                        "evidence_bundle": {
                            "governed_family": family_name,
                            "family_summary": family_item,
                            "family_candidate_lineage": family_lineage,
                            "candidate_pool_count": int(active_candidate_pool.get("count") or 0),
                        },
                    },
                    family_rows,
                )
            )

        for idx, candidate in enumerate(governed_top_candidates[:2], 1):
            artifact_id = str(candidate.get("artifact_id") or "").strip()
            candidate_name = str(candidate.get("name") or artifact_id or f"candidate_{idx}").strip()
            family_name = str(candidate.get("family") or "").strip()
            expected_regime = [
                str(item).strip()
                for item in list(candidate.get("expected_regime") or [])
                if str(item).strip()
            ]
            candidate_base_preferences = preferred_strategy_types_for_factor(
                family_name or candidate_name,
                default=regime_preferences or ["ma_cross", "momentum"],
            )
            candidate_contract = cls._governed_family_generation_contract(
                family_name or candidate_name,
                base_preferences=candidate_base_preferences,
            )
            candidate_preferences = list(candidate_contract.get("preferred_strategy_types") or candidate_base_preferences)
            family_rows = cls._focus_rows_for_family(
                rows,
                family_name=family_name or candidate_name,
                hot_sectors=hot_sectors,
                cold_sectors=cold_sectors,
            )
            focus_industries = cls._focus_industries_from_rows(family_rows) or hot_sectors[:1]
            score = cls._safe_float(candidate.get("total_score"))
            source_generation_artifact_id = str(candidate.get("source_generation_artifact_id") or "").strip() or None
            source_validation_artifact_id = (
                str(candidate.get("source_validation_artifact_id") or artifact_id or "").strip() or None
            )
            candidate_registry_stage = str(candidate.get("registry_stage") or "").strip() or None
            memory_record_id = str(candidate.get("memory_record_id") or "").strip() or None
            expected_holding_period = candidate.get("expected_holding_period")
            latest_validation_at = candidate.get("latest_validation_at") or candidate.get("updated_at") or candidate.get("created_at")
            latest_validation_age_days = candidate.get("latest_validation_age_days")
            candidate_evidence_status = dict(candidate.get("evidence_status") or {})
            candidate_lineage = {
                "source_generation_artifact_id": source_generation_artifact_id,
                "source_validation_artifact_id": source_validation_artifact_id,
                "memory_record_id": memory_record_id,
                "registry_stage": candidate_registry_stage,
                "expected_regime": expected_regime,
                "expected_holding_period": expected_holding_period,
                "latest_validation_at": latest_validation_at,
                "latest_validation_age_days": latest_validation_age_days,
                "admission_blocked": bool(candidate.get("admission_blocked")),
                "admission_block_reasons": list(candidate.get("admission_block_reasons") or []),
                "evidence_status": candidate_evidence_status,
            }
            priority = min(99, max(84, int(round(84 + score * 0.12)) - idx))
            tasks.append(
                _snapshot_task(
                    {
                        "task_id": f"governed_candidate_{idx}_{artifact_id or family_name or idx}",
                        "task_key": f"governed_candidate:{snapshot.get('date')}:{artifact_id or candidate_name}",
                        "task_source": "snapshot",
                        "theme": f"factor_candidate_{artifact_id or family_name or idx}",
                        "title": f"候选因子任务·{candidate_name}",
                        "opportunity_type": "candidate_factor_activation",
                        "rationale": (
                            f"治理后候选因子 {candidate_name} 已进入 active pool，"
                            f"family={family_name or 'unknown'}，score={score:.1f}，"
                            f"expected_regime={','.join(expected_regime) or 'n/a'}，"
                            f"holding={expected_holding_period or 'n/a'}d，"
                            f"validation_age={latest_validation_age_days if latest_validation_age_days is not None else 'n/a'}d。"
                        ),
                        "preferred_strategy_types": candidate_preferences,
                        "strategy_preferences": candidate_preferences,
                        "allowed_strategy_types": list(candidate_contract.get("allowed_strategy_types") or []),
                        "target_symbol_policy": "prefer_intersection",
                        "universe_expansion_policy": "allow_market_fallback",
                        "preference_strength": "medium",
                        "preference_reason": (
                            f"governed_candidate:{artifact_id or candidate_name};"
                            f"family={family_name or 'unknown'};score={score:.1f}"
                            + (
                                f";template_profile={candidate_contract.get('template_generation_profile')}"
                                if candidate_contract.get("template_generation_profile")
                                else ""
                            )
                        ),
                        "validation_focus": "candidate_target_only",
                        "focus_industries": focus_industries,
                        "target_symbols": _select_snapshot_targets(
                            f"governed_candidate:{snapshot.get('date')}:{artifact_id or candidate_name}",
                            family_rows,
                            hot=hot_sectors,
                            cold=cold_sectors,
                        ),
                        "focus_markets": [],
                        "priority": priority,
                        "generation_limit": cls._snapshot_generation_limit(
                            AUTONOMY_CANDIDATES_PER_TASK,
                            priority=priority,
                        ),
                        "factor_name": family_name or candidate_name,
                        "candidate_family": family_name,
                        "source_candidate_artifact_id": artifact_id or None,
                        "source_generation_artifact_id": source_generation_artifact_id,
                        "source_validation_artifact_id": source_validation_artifact_id,
                        "memory_record_id": memory_record_id,
                        "candidate_registry_stage": candidate_registry_stage,
                        "candidate_name": candidate_name,
                        "template_generation_profile": candidate_contract.get("template_generation_profile"),
                        "candidate_grade": candidate.get("grade"),
                        "validation_score": round(score, 4),
                        "expected_regime": expected_regime,
                        "expected_holding_period": expected_holding_period,
                        "latest_validation_at": latest_validation_at,
                        "latest_validation_age_days": latest_validation_age_days,
                        "candidate_evidence_status": candidate_evidence_status,
                        "evidence_bundle": {
                            "governed_candidate": candidate,
                            "candidate_lineage": candidate_lineage,
                            "candidate_pool_count": int(active_candidate_pool.get("count") or 0),
                        },
                    },
                    family_rows,
                )
            )

        rising_factors = [
            str(item).strip()
            for item in list(
                [item.get("family") for item in governed_family_summary]
                or factor_research.get("active_factors")
                or factor_research.get("positive_rising_factors")
                or [name for name, trend in dict(snapshot.get("factor_ic_trend") or {}).items() if str(trend) == "rising"]
            )
            if str(item).strip()
        ]
        for idx, factor_name in enumerate(rising_factors[:3], 1):
            factor_base_preferences = preferred_strategy_types_for_factor(str(factor_name), default=["ma_cross", "momentum"])
            factor_contract = cls._governed_family_generation_contract(
                str(factor_name),
                base_preferences=factor_base_preferences,
            )
            preferences = list(factor_contract.get("preferred_strategy_types") or factor_base_preferences)
            priority = 80 - idx
            tasks.append(
                _snapshot_task(
                    {
                        "task_id": f"factor_{idx}_{factor_name}",
                        "task_key": f"factor:{snapshot.get('date')}:{factor_name}",
                        "task_source": "snapshot",
                        "theme": f"factor_rotation_{factor_name}",
                        "title": f"因子加速任务·{factor_name}",
                        "opportunity_type": "factor_acceleration",
                        "rationale": f"因子 {factor_name} 在快照中呈上升趋势，自动生成围绕该因子的选股与择时策略。",
                        "preferred_strategy_types": preferences,
                        "strategy_preferences": preferences,
                        "allowed_strategy_types": list(factor_contract.get("allowed_strategy_types") or []),
                        "target_symbol_policy": "prefer_intersection",
                        "universe_expansion_policy": "allow_market_fallback",
                        "preference_strength": "soft",
                        "preference_reason": (
                            f"factor_research:{factor_name}"
                            + (
                                f";template_profile={factor_contract.get('template_generation_profile')}"
                                if factor_contract.get("template_generation_profile")
                                else ""
                            )
                        ),
                        "validation_focus": "target_plus_representative",
                        "focus_industries": hot_sectors[:1],
                        "target_symbols": _select_snapshot_targets(
                            f"factor:{snapshot.get('date')}:{factor_name}",
                            rows,
                            hot=hot_sectors,
                            cold=cold_sectors,
                        ),
                        "focus_markets": [],
                        "priority": priority,
                        "generation_limit": cls._snapshot_generation_limit(AUTONOMY_CANDIDATES_PER_TASK, priority=priority),
                        "factor_name": factor_name,
                        "template_generation_profile": factor_contract.get("template_generation_profile"),
                    },
                    rows,
                )
            )

        if rows:
            top_industries: Dict[str, int] = {}
            for row in rows:
                industry = str((row or {}).get("industry") or (row or {}).get("sector") or "未分类").strip() or "未分类"
                top_industries[industry] = top_industries.get(industry, 0) + 1
            ranked_industries = [item[0] for item in sorted(top_industries.items(), key=lambda item: item[1], reverse=True)]
            for idx, best_industry in enumerate(ranked_industries[:2], 1):
                matched = cls._match_rows(rows, [best_industry])
                priority = 68 - idx
                tasks.append(
                    _snapshot_task(
                        {
                            "task_id": f"industry_{idx}_{best_industry}",
                            "task_key": f"industry:{snapshot.get('date')}:{best_industry}",
                            "task_source": "snapshot",
                            "theme": f"industry_leadership_{best_industry}",
                            "title": f"行业龙头任务·{best_industry}",
                            "opportunity_type": "industry_leadership",
                            "rationale": f"股票池中 {best_industry} 权重较高，自动生成龙头趋势与轮动策略。",
                            "preferred_strategy_types": ["ma_cross", "momentum", "quality_factor"],
                            "strategy_preferences": ["ma_cross", "momentum", "quality_factor"],
                            "target_symbol_policy": "prefer_intersection",
                            "universe_expansion_policy": "allow_market_fallback",
                            "preference_strength": "soft",
                            "preference_reason": "breadth_rotation_mix",
                            "validation_focus": "target_plus_representative",
                            "focus_industries": [best_industry],
                            "target_symbols": _select_snapshot_targets(
                                f"industry:{snapshot.get('date')}:{best_industry}",
                                matched or rows,
                                hot=[best_industry],
                                cold=cold_sectors,
                            ),
                            "focus_markets": [],
                            "priority": priority,
                            "generation_limit": cls._snapshot_generation_limit(
                                AUTONOMY_CANDIDATES_PER_TASK,
                                priority=priority,
                            ),
                        },
                        matched or rows,
                    )
                )
        return tasks
