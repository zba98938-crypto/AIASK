"""Economic alpha blueprints for quality-first factor generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .engines.base import FactorCandidate


@dataclass(frozen=True)
class AlphaBlueprint:
    """A finance-aware seed expression plus diagnostics metadata."""

    blueprint_id: str
    factor_family: str
    expression_dsl: str
    inputs: list[str]
    economic_hypothesis: str
    expected_horizon: int = 10
    risk_exposure_hint: dict[str, Any] = field(default_factory=dict)
    complexity_hint: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return {
            "blueprint_id": self.blueprint_id,
            "factor_family": self.factor_family,
            "expression_dsl": self.expression_dsl,
            "inputs": list(self.inputs),
            "economic_hypothesis": self.economic_hypothesis,
            "expected_horizon": int(self.expected_horizon),
            "risk_exposure_hint": dict(self.risk_exposure_hint),
            "complexity_hint": self.complexity_hint,
        }


class AlphaBlueprintLibrary:
    """Curated financial priors used as seeds for all search engines."""

    def __init__(self) -> None:
        self._blueprints = [
            AlphaBlueprint(
                blueprint_id="vol_adjusted_momentum_20d",
                factor_family="vol_adjusted_momentum",
                expression_dsl="zscore(momentum_20d, 20) - zscore(volatility_20d, 20)",
                inputs=["momentum_20d", "volatility_20d"],
                economic_hypothesis=(
                    "Medium-term winners with lower realized volatility may carry "
                    "cleaner risk-adjusted continuation."
                ),
                expected_horizon=10,
                risk_exposure_hint={"style": ["momentum"], "risk": ["low_volatility"]},
            ),
            AlphaBlueprint(
                blueprint_id="short_reversal_5d",
                factor_family="reversal",
                expression_dsl="-zscore(return_5d, 20)",
                inputs=["return_5d"],
                economic_hypothesis=(
                    "Short-horizon overreaction can mean-revert after abrupt five-day moves."
                ),
                expected_horizon=5,
                risk_exposure_hint={"style": ["reversal"], "risk": ["crowding"]},
            ),
            AlphaBlueprint(
                blueprint_id="volume_price_confirmation",
                factor_family="volume_price",
                expression_dsl="zscore(volume_ratio_5_20, 20) * zscore(return_5d, 20)",
                inputs=["volume_ratio_5_20", "return_5d"],
                economic_hypothesis=(
                    "Recent price moves confirmed by unusual volume are more likely "
                    "to persist than unconfirmed moves."
                ),
                expected_horizon=10,
                risk_exposure_hint={"style": ["momentum"], "risk": ["liquidity"]},
            ),
            AlphaBlueprint(
                blueprint_id="liquidity_pressure",
                factor_family="liquidity",
                expression_dsl="ts_rank(amount, 20) - ts_rank(volume, 20)",
                inputs=["amount", "volume"],
                economic_hypothesis=(
                    "A divergence between traded value rank and volume rank may capture "
                    "capital intensity beyond raw turnover."
                ),
                expected_horizon=10,
                risk_exposure_hint={"style": ["liquidity"], "risk": ["size"]},
            ),
            AlphaBlueprint(
                blueprint_id="momentum_slope_60_20",
                factor_family="momentum",
                expression_dsl="zscore(momentum_60d, 60) - zscore(momentum_20d, 20)",
                inputs=["momentum_60d", "momentum_20d"],
                economic_hypothesis=(
                    "A stable long trend with less crowded recent acceleration can have "
                    "better continuation quality."
                ),
                expected_horizon=20,
                risk_exposure_hint={"style": ["momentum"], "risk": ["trend_crowding"]},
            ),
            AlphaBlueprint(
                blueprint_id="range_reversal",
                factor_family="volatility_reversal",
                expression_dsl="zscore((high - low) / close, 20) * -zscore(return_5d, 20)",
                inputs=["high", "low", "close", "return_5d"],
                economic_hypothesis=(
                    "Large intraday range after a short-term move can indicate exhaustion "
                    "and a higher reversal chance."
                ),
                expected_horizon=5,
                risk_exposure_hint={"style": ["reversal"], "risk": ["volatility"]},
            ),
            AlphaBlueprint(
                blueprint_id="price_volume_divergence",
                factor_family="volume_price",
                expression_dsl="zscore(close / delay(close, 20), 20) - zscore(volume_ratio_5_20, 20)",
                inputs=["close", "volume_ratio_5_20"],
                economic_hypothesis=(
                    "Price strength unsupported by relative volume may be more fragile "
                    "than broad participation trends."
                ),
                expected_horizon=10,
                risk_exposure_hint={"style": ["volume_price"], "risk": ["liquidity"]},
            ),
            AlphaBlueprint(
                blueprint_id="risk_adjusted_rank_momentum",
                factor_family="vol_adjusted_momentum",
                expression_dsl="ts_rank(return_20d, 60) - ts_rank(volatility_20d, 60)",
                inputs=["return_20d", "volatility_20d"],
                economic_hypothesis=(
                    "Stocks with stronger medium-term return rank and lower volatility "
                    "rank may offer better risk-adjusted alpha."
                ),
                expected_horizon=20,
                risk_exposure_hint={"style": ["momentum"], "risk": ["low_volatility"]},
            ),
            AlphaBlueprint(
                blueprint_id="capital_flow_divergence",
                factor_family="liquidity",
                expression_dsl="zscore(delta(amount, 5), 20) - zscore(delta(close, 5), 20)",
                inputs=["amount", "close"],
                economic_hypothesis=(
                    "Capital flow acceleration not yet reflected in price acceleration "
                    "can precede delayed repricing."
                ),
                expected_horizon=10,
                risk_exposure_hint={"style": ["liquidity"], "risk": ["turnover"]},
            ),
            AlphaBlueprint(
                blueprint_id="ranked_vol_adjusted_momentum",
                factor_family="vol_adjusted_momentum",
                expression_dsl="rank(zscore(momentum_20d, 20) - zscore(volatility_20d, 20))",
                inputs=["momentum_20d", "volatility_20d"],
                economic_hypothesis=(
                    "Ranking a volatility-adjusted momentum spread can reduce the impact "
                    "of outliers while preserving the direction signal."
                ),
                expected_horizon=10,
                risk_exposure_hint={"style": ["momentum"], "risk": ["outlier_control"]},
            ),
        ]

    def build_context_blueprints(
        self,
        *,
        failed_pattern_memory: list[dict[str, Any]] | None = None,
        successful_pattern_memory: list[dict[str, Any]] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return blueprints ordered by recent success/failure memory."""

        failed_weights = self._memory_weights(failed_pattern_memory or [])
        success_weights = self._memory_weights(successful_pattern_memory or [])

        ranked: list[tuple[float, AlphaBlueprint]] = []
        for blueprint in self._blueprints:
            penalty = failed_weights.get(blueprint.blueprint_id, 0) * 2.0
            reward = success_weights.get(blueprint.blueprint_id, 0) * 3.0
            family_penalty = failed_weights.get(blueprint.factor_family, 0) * 0.5
            family_reward = success_weights.get(blueprint.factor_family, 0) * 1.0
            score = 10.0 + reward + family_reward - penalty - family_penalty
            ranked.append((score, blueprint))

        ranked.sort(key=lambda item: (-item[0], item[1].blueprint_id))
        selected = [blueprint.to_dict() for score, blueprint in ranked if score > 0.0]
        if limit is not None:
            selected = selected[: max(0, int(limit))]
        return selected

    @staticmethod
    def _memory_weights(rows: list[dict[str, Any]]) -> dict[str, int]:
        weights: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in (
                str(row.get("blueprint_id") or "").strip(),
                str(row.get("factor_family") or row.get("family") or "").strip(),
                str(row.get("pattern") or "").strip(),
            ):
                if key:
                    weights[key] = weights.get(key, 0) + int(row.get("count") or 1)
        return weights


def candidate_from_blueprint(
    blueprint: dict[str, Any],
    *,
    engine_id: str,
    index: int,
    mode: str = "blueprint_seed",
) -> FactorCandidate:
    """Build a FactorCandidate with the required blueprint diagnostics."""

    blueprint_id = str(blueprint.get("blueprint_id") or f"blueprint_{index + 1}")
    family = str(blueprint.get("factor_family") or blueprint.get("family") or "custom")
    hypothesis = str(
        blueprint.get("economic_hypothesis")
        or blueprint.get("hypothesis")
        or "Finance-prior alpha blueprint."
    )
    risk_hint = dict(blueprint.get("risk_exposure_hint") or {})
    return FactorCandidate(
        name=f"{engine_id}_{blueprint_id}_{index + 1}",
        hypothesis=hypothesis,
        economic_hypothesis=hypothesis,
        family=family,
        factor_family=family,
        inputs=list(blueprint.get("inputs") or []),
        expression_dsl=str(blueprint.get("expression_dsl") or ""),
        expected_holding_period=int(blueprint.get("expected_horizon") or 10),
        expected_horizon=int(blueprint.get("expected_horizon") or 10),
        complexity_hint=str(blueprint.get("complexity_hint") or "medium"),
        novelty_rationale=f"Seeded from alpha blueprint {blueprint_id}",
        generation_engine=engine_id,
        blueprint_id=blueprint_id,
        risk_exposure_hint=risk_hint,
        generation_trace={
            "mode": mode,
            "blueprint_id": blueprint_id,
            "factor_family": family,
            "economic_hypothesis": hypothesis,
            "risk_exposure_hint": risk_hint,
        },
    )
