"""规则种子引擎 — 包装现有 llm_alpha.py + factor_candidate_seed.py。"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from .base import EngineStatus, FactorCandidate, SearchBudget

logger = logging.getLogger(__name__)


class RuleSeedEngine:
    """规则种子引擎 — 从预定义模板和本地规则生成候选因子。

    复用现有：
    - factor_candidate_seed._EXPRESSION_BY_FACTOR
    - llm_alpha.LLMAlphaMiner._build_local_candidate_pool()
    """

    engine_id: str
    engine_type: str = "rule"

    def __init__(self, engine_id: str = "rule_seed"):
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
        """从种子库和本地规则生成候选。"""
        try:
            candidates = []
            candidates.extend(
                self._from_blueprints(context, max(1, budget.candidate_count // 2))
            )

            # 1. 从种子库生成（稳定族优先：volume / reversal / medium-long momentum）
            seed_budget = max(1, int(budget.candidate_count or 1) * 2 // 3)
            seed_candidates = self._from_seed_library(seed_budget)
            candidates.extend(seed_candidates)

            # 2. 从本地规则生成（组合变体）
            variant_candidates = self._generate_variants(budget.candidate_count - len(candidates))
            candidates.extend(variant_candidates)
            # ATR-inverse short-window family is optional near-miss exploration only.
            # Default OFF under stable-family bias / lite quality work.
            allow_atr_inverse = str(
                os.getenv("FACTOR_MINING_ALLOW_ATR_INVERSE_VARIANTS", "0")
            ).strip().lower() in {"1", "true", "yes", "on"}
            if getattr(context, "quick_evidence_evaluator", None) is not None:
                if allow_atr_inverse:
                    candidates.extend(self._targeted_low_volatility_variants())
                candidates.extend(
                    self._from_seed_library_excluding(
                        max(int(budget.candidate_count or 0) * 4, 12),
                        context,
                    )
                )
                # Prefer non-inverse directional probes for stable families first.
                if allow_atr_inverse:
                    candidates.extend(self._directional_inversions(candidates))
                else:
                    candidates.extend(
                        self._directional_inversions(
                            [
                                c
                                for c in candidates
                                if str(getattr(c, "family", "") or "").lower()
                                in {"volume", "reversal", "momentum", "trend", "liquidity"}
                            ],
                            limit=8,
                        )
                    )
            candidates = self._filter_failed_patterns(candidates, context)
            if not candidates:
                candidates = self._from_seed_library_excluding(
                    budget.candidate_count,
                    context,
                )
            candidates = self._rank_candidates(candidates)

            self._success_count += 1
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            return self._dedupe(candidates)[:budget.candidate_count]

        except Exception as exc:
            self._failure_count += 1
            self._last_error = str(exc)
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            logger.warning("RuleSeedEngine: failed: %s", exc)
            return []

    def _from_blueprints(self, context: Any, count: int) -> list[FactorCandidate]:
        from ..blueprints import candidate_from_blueprint

        candidates: list[FactorCandidate] = []
        for index, blueprint in enumerate(
            list(getattr(context, "alpha_blueprints", []) or [])[: max(0, count)]
        ):
            candidate = candidate_from_blueprint(
                blueprint,
                engine_id=self.engine_id,
                index=index,
                mode="rule_blueprint_seed",
            )
            candidate.generation_trace["source"] = "alpha_blueprint_library"
            candidates.append(candidate)
        return candidates

    def _from_seed_library(self, count: int) -> list[FactorCandidate]:
        """从种子因子库生成。"""
        from ...factor_candidate_seed import _EXPRESSION_BY_FACTOR

        candidates = []
        for name, (expr, inputs) in list(_EXPRESSION_BY_FACTOR.items())[:count]:
            candidates.append(FactorCandidate(
                name=f"seed_{name}",
                hypothesis=f"Seed factor from established library: {name}",
                family=name.split("_")[0] if "_" in name else name,
                inputs=inputs,
                expression_dsl=expr,
                expected_holding_period=10,
                complexity_hint="low",
                novelty_rationale="Established factor from seed library",
                generation_engine=self.engine_id,
                generation_trace={"mode": "seed_library", "seed_name": name},
            ))

        return candidates

    def _from_seed_library_excluding(self, count: int, context: Any) -> list[FactorCandidate]:
        failed = self._failed_patterns(context)
        if not failed:
            return self._from_seed_library(count)

        from ...factor_candidate_seed import _EXPRESSION_BY_FACTOR

        candidates: list[FactorCandidate] = []
        for name, (expr, inputs) in list(_EXPRESSION_BY_FACTOR.items()):
            candidate = FactorCandidate(
                name=f"seed_{name}",
                hypothesis=f"Seed factor from established library: {name}",
                family=name.split("_")[0] if "_" in name else name,
                inputs=inputs,
                expression_dsl=expr,
                expected_holding_period=10,
                complexity_hint="low",
                novelty_rationale="Established factor from seed library",
                generation_engine=self.engine_id,
                generation_trace={"mode": "seed_library_memory_fallback", "seed_name": name},
            )
            if self._candidate_pattern_keys(candidate).intersection(failed):
                continue
            candidates.append(candidate)
            if len(candidates) >= max(0, int(count)):
                break
        return candidates

    def _generate_variants(self, count: int) -> list[FactorCandidate]:
        """生成种子因子的组合变体。"""
        import random

        from ...factor_candidate_seed import _EXPRESSION_BY_FACTOR

        templates = list(_EXPRESSION_BY_FACTOR.items())
        candidates = []

        # 组合模板：A + B, A - B, A * sign(B)
        combinators = [
            ("{a} + {b}", "combined_sum"),
            ("{a} - {b}", "combined_diff"),
            ("({a}) * sign({b})", "conditional_sign"),
            ("zscore({a}, 20) + zscore({b}, 20)", "dual_zscore"),
        ]

        for _ in range(count):
            if len(templates) < 2:
                break
            (name_a, (expr_a, inputs_a)), (name_b, (expr_b, inputs_b)) = random.sample(templates, 2)
            combinator, combo_type = random.choice(combinators)

            combined_expr = combinator.format(a=expr_a, b=expr_b)
            combined_inputs = list(set(inputs_a + inputs_b))

            candidates.append(FactorCandidate(
                name=f"rule_combo_{name_a}_{name_b}_{combo_type}",
                hypothesis=f"Rule-based combination of {name_a} and {name_b}",
                family="custom",
                inputs=combined_inputs,
                expression_dsl=combined_expr,
                expected_holding_period=10,
                complexity_hint="medium",
                novelty_rationale=f"Combinatorial variant: {combo_type}({name_a}, {name_b})",
                generation_engine=self.engine_id,
                generation_trace={
                    "mode": "rule_variant",
                    "parent_a": name_a,
                    "parent_b": name_b,
                    "combinator": combo_type,
                },
            ))

        return candidates

    def _targeted_low_volatility_variants(self) -> list[FactorCandidate]:
        """Add deterministic variants around the strongest recent near-miss family."""

        templates = [
            (
                "atr_range_inverse_mean_3",
                "-ts_mean((high - low) / close, 3)",
                ["high", "low", "close"],
                "Very short-window low intraday range continuation variant",
            ),
            (
                "atr_range_inverse_mean_5",
                "-ts_mean((high - low) / close, 5)",
                ["high", "low", "close"],
                "Short-window low intraday range continuation variant",
            ),
            (
                "atr_range_inverse_mean_10",
                "-ts_mean((high - low) / close, 10)",
                ["high", "low", "close"],
                "Medium-window low intraday range continuation variant",
            ),
            (
                "atr_range_inverse_zscore_10",
                "-zscore((high - low) / close, 10)",
                ["high", "low", "close"],
                "Cross-time standardized low range variant",
            ),
            (
                "atr_range_inverse_zscore_20",
                "-zscore((high - low) / close, 20)",
                ["high", "low", "close"],
                "Stable standardized low range variant",
            ),
            (
                "atr_range_inverse_rank_20",
                "-ts_rank((high - low) / close, 20)",
                ["high", "low", "close"],
                "Time-series rank low range variant",
            ),
            (
                "realized_vol_inverse_std_10",
                "-ts_std(returns_1d, 10)",
                ["returns_1d"],
                "Short-window realized low volatility variant",
            ),
        ]
        candidates: list[FactorCandidate] = []
        for name, expr, inputs, hypothesis in templates:
            candidates.append(
                FactorCandidate(
                    name=f"rule_{name}",
                    hypothesis=hypothesis,
                    economic_hypothesis=hypothesis,
                    family="volatility",
                    factor_family="volatility",
                    inputs=list(inputs),
                    expression_dsl=expr,
                    expected_holding_period=10,
                    expected_horizon=10,
                    expected_regime=["low_volatility", "range_compression"],
                    complexity_hint="low",
                    novelty_rationale=(
                        "Deterministic local variant around ATR inverse quick-evidence near misses."
                    ),
                    generation_engine=self.engine_id,
                    generation_trace={
                        "mode": "targeted_low_volatility_variant",
                        "source": "quick_failure_memory",
                        "parent": "seed_atr_14_inverse",
                    },
                )
            )
        return candidates

    def _directional_inversions(
        self,
        candidates: list[FactorCandidate],
        *,
        limit: int = 24,
    ) -> list[FactorCandidate]:
        inverted: list[FactorCandidate] = []
        for candidate in candidates:
            expr = str(candidate.expression_dsl or "").strip()
            if not expr or expr.startswith("-("):
                continue
            trace = dict(candidate.generation_trace or {})
            parent = str(candidate.name or "").strip() or "candidate"
            inverted.append(
                FactorCandidate(
                    name=f"{parent}_inverse",
                    hypothesis=f"Directional inversion of {parent}",
                    economic_hypothesis=(
                        getattr(candidate, "economic_hypothesis", "")
                        or candidate.hypothesis
                    ),
                    family=candidate.family,
                    factor_family=getattr(candidate, "factor_family", "") or candidate.family,
                    inputs=list(candidate.inputs or []),
                    expression_dsl=f"-({expr})",
                    expected_holding_period=candidate.expected_holding_period,
                    expected_horizon=getattr(candidate, "expected_horizon", candidate.expected_holding_period),
                    expected_regime=list(candidate.expected_regime or []),
                    complexity_hint=candidate.complexity_hint,
                    novelty_rationale=f"Test opposite IC direction for {parent}",
                    generation_engine=self.engine_id,
                    blueprint_id=getattr(candidate, "blueprint_id", ""),
                    risk_exposure_hint=dict(getattr(candidate, "risk_exposure_hint", None) or {}),
                    generation_trace={
                        **trace,
                        "mode": "directional_inversion",
                        "parent": parent,
                    },
                )
            )
            if len(inverted) >= max(1, int(limit)):
                break
        return inverted

    def _filter_failed_patterns(
        self,
        candidates: list[FactorCandidate],
        context: Any,
    ) -> list[FactorCandidate]:
        failed = self._failed_patterns(context)
        if not failed:
            return candidates
        kept: list[FactorCandidate] = []
        for candidate in candidates:
            if self._candidate_pattern_keys(candidate).intersection(failed):
                continue
            kept.append(candidate)
        return kept

    def _rank_candidates(self, candidates: list[FactorCandidate]) -> list[FactorCandidate]:
        unique = self._dedupe(candidates)

        def _float(value: Any) -> float:
            try:
                return float(value or 0.0)
            except Exception:
                return 0.0

        def _family_rank(candidate: FactorCandidate) -> int:
            family = str(
                getattr(candidate, "factor_family", "")
                or getattr(candidate, "family", "")
                or ""
            ).strip().lower()
            seed_name = str((candidate.generation_trace or {}).get("seed_name") or "").strip().lower()
            # Lower is better. Prefer medium/long stable families over short ATR inverse.
            order = {
                "volume": 0,
                "reversal": 1,
                "liquidity": 2,
                "momentum": 3,
                "trend": 4,
                "sentiment": 5,
                "event": 6,
                "risk_adjusted": 7,
                "volatility": 8,
                "custom": 9,
            }
            if seed_name in {
                "volume_ratio",
                "turnover_20d",
                "reversal",
                "mom_10d",
                "mom_60d",
                "momentum",
                "trend",
            }:
                return 0
            if "atr_range_inverse" in str(getattr(candidate, "name", "") or "").lower():
                return 9
            return order.get(family, 6)

        def _score(candidate: FactorCandidate) -> tuple[int, int, int, float, float, int, float, float]:
            quick = dict(candidate.quick_evidence or {})
            summary = dict(quick.get("evidence_summary") or {})
            trace = dict(candidate.generation_trace or {})
            mode = str(trace.get("mode") or "").strip().lower()
            # Prefer established seed/blueprint families before speculative inversions.
            # Ranking often runs before quick evidence is available.
            mode_rank = {
                "seed_library": 5,
                "seed_library_memory_fallback": 5,
                "rule_blueprint_seed": 4,
                "stable_family_seed": 5,
                "alpha_blueprint_library": 4,
                "directional_inversion": 1,
                "targeted_low_volatility_variant": 0,
            }.get(mode, 2)
            # An injected blueprint is explicit caller intent, so it outranks the
            # generic stable-family preference once quick evidence is tied.
            return (
                1 if quick.get("passed") else 0,
                1 if str(getattr(candidate, "blueprint_id", "") or "").strip() else 0,
                mode_rank,
                _float(quick.get("quality_score")),
                abs(_float(summary.get("rank_ic_mean") or quick.get("rank_ic_mean"))),
                1 if mode not in {"targeted_low_volatility_variant", "directional_inversion"} else 0,
                _float(candidate.fitness),
                -float(_family_rank(candidate)),
            )

        return sorted(unique, key=_score, reverse=True)

    @staticmethod
    def _failed_patterns(context: Any) -> set[str]:
        try:
            min_count = max(1, int(str(os.getenv("FACTOR_MINING_PATTERN_FILTER_MIN_COUNT", "3")).strip()))
        except Exception:
            min_count = 3
        failed: set[str] = set()
        for row in list(getattr(context, "failed_pattern_memory", []) or []):
            if not isinstance(row, dict):
                continue
            pattern = str(row.get("pattern") or "").strip()
            if not pattern:
                continue
            try:
                count = int(row.get("count") or 1)
            except Exception:
                count = 1
            if count >= min_count:
                failed.add(pattern)
        return failed

    @staticmethod
    def _candidate_pattern_keys(candidate: FactorCandidate) -> set[str]:
        trace = dict(getattr(candidate, "generation_trace", None) or {})
        if trace.get("mode") == "targeted_low_volatility_variant":
            keys = {
                str(getattr(candidate, "name", "") or "").strip(),
                str(trace.get("mode") or "").strip(),
                str(trace.get("parent") or "").strip(),
            }
            return {key for key in keys if key}
        keys = {
            str(getattr(candidate, "blueprint_id", "") or "").strip(),
            str(getattr(candidate, "factor_family", "") or "").strip(),
            str(getattr(candidate, "family", "") or "").strip(),
            str(trace.get("seed_name") or "").strip(),
            str(trace.get("parent_a") or "").strip(),
            str(trace.get("parent_b") or "").strip(),
            str(trace.get("blueprint_id") or "").strip(),
        }
        return {key for key in keys if key}

    @staticmethod
    def _dedupe(candidates: list[FactorCandidate]) -> list[FactorCandidate]:
        seen: set[str] = set()
        unique: list[FactorCandidate] = []
        for candidate in candidates:
            key = str(candidate.expression_dsl or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

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
