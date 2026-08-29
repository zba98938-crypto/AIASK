"""AI Validation Orchestrator — the main entry point.

Replaces the fixed G0-G3 pipeline with AI-driven validation.
Supports shadow mode (run alongside legacy, compare results) and
active mode (replace legacy pipeline).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .config import AI_VALIDATION_ENABLED, AI_VALIDATION_MODE, AI_VALIDATION_CONFIG
from .hard_constraints import HardConstraints
from .layer_a_structural import LLMStructuralReviewer
from .layer_b_quality import StrategyQualityPredictor
from .layer_c_committee import StrategyReviewCommittee
from .decision_fusion import DecisionFusion

logger = logging.getLogger(__name__)


class AIValidator:
    """Main orchestrator for AI-driven strategy validation.

    Usage:
        validator = AIValidator(llm_gateway=autonomy_gateway)
        result = await validator.validate(candidate, backtest_result)
        # result["decision"] in {"approve", "observe", "reject"}
    """

    def __init__(self, *, llm_gateway=None):
        self._hard_constraints = HardConstraints()
        self._layer_a = LLMStructuralReviewer(llm_gateway=llm_gateway)
        self._layer_b = StrategyQualityPredictor()
        self._layer_c = StrategyReviewCommittee(llm_gateway=llm_gateway)
        self._fusion = DecisionFusion()
        self._enabled = AI_VALIDATION_ENABLED
        self._mode = AI_VALIDATION_MODE

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    async def validate(
        self,
        candidate: dict[str, Any],
        backtest_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the full AI validation pipeline.

        Returns:
            {
                "decision": "approve" | "observe" | "reject",
                "composite_score": float,
                "layer_results": {...},
                "elapsed_sec": float,
                "mode": "shadow" | "active",
            }
        """
        start = time.time()

        # 1. Hard constraints (instant, no AI)
        hard_check = self._hard_constraints.check(candidate, backtest_result)
        if not hard_check["passed"]:
            return self._build_result(
                decision="reject",
                hard_check=hard_check,
                layer_a={},
                layer_b={},
                layer_c={},
                fusion={"decision": "reject", "composite_score": 0.0},
                elapsed=time.time() - start,
            )

        # 2. Layer A + Layer B in parallel (independent)
        layer_a_task = asyncio.create_task(self._layer_a.review(candidate))
        layer_b_result = self._layer_b.predict(candidate, backtest_result)

        layer_a_result = await layer_a_task

        # 3. Layer C only if Layer B score is high enough
        layer_c_result = await self._layer_c.review(
            candidate, backtest_result, layer_b_result["quality_score"]
        )

        # 4. Fuse all results
        fusion_result = self._fusion.fuse(
            hard_check, layer_a_result, layer_b_result, layer_c_result
        )

        return self._build_result(
            decision=fusion_result["decision"],
            hard_check=hard_check,
            layer_a=layer_a_result,
            layer_b=layer_b_result,
            layer_c=layer_c_result,
            fusion=fusion_result,
            elapsed=time.time() - start,
        )

    async def validate_batch(
        self,
        candidates: list[dict[str, Any]],
        backtest_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Validate a batch of candidates concurrently."""
        tasks = [
            self.validate(candidate, result)
            for candidate, result in zip(candidates, backtest_results)
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def _build_result(
        self,
        *,
        decision: str,
        hard_check: dict,
        layer_a: dict,
        layer_b: dict,
        layer_c: dict,
        fusion: dict,
        elapsed: float,
    ) -> dict[str, Any]:
        return {
            "decision": decision,
            "composite_score": fusion.get("composite_score", 0.0),
            "mode": self._mode,
            "elapsed_sec": round(elapsed, 2),
            "layer_results": {
                "hard_constraints": hard_check,
                "layer_a": layer_a,
                "layer_b": layer_b,
                "layer_c": layer_c,
            },
            "fusion": fusion,
        }
