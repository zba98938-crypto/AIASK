"""Decision fusion: combines all AI layers into a final verdict.

Fusion logic:
1. Hard constraints: absolute veto (cannot be overridden)
2. Weighted combination: Layer B (0.45) + Layer C (0.35) + Layer A (0.20)
3. Thresholds: >= 0.6 approve, 0.4-0.6 observe, < 0.4 reject
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .config import AI_VALIDATION_CONFIG

logger = logging.getLogger(__name__)


class DecisionFusion:
    """Fuses all AI validation layers into a final decision."""

    def __init__(self):
        self._config = AI_VALIDATION_CONFIG["fusion"]

    def fuse(
        self,
        hard_check: dict[str, Any],
        layer_a: dict[str, Any],
        layer_b: dict[str, Any],
        layer_c: dict[str, Any],
    ) -> dict[str, Any]:
        """Produce final validation decision.

        Args:
            hard_check: HardConstraints.check() result
            layer_a: LLMStructuralReviewer.review() result
            layer_b: StrategyQualityPredictor.predict() result
            layer_c: StrategyReviewCommittee.review() result

        Returns:
            Final decision with full audit trail.
        """
        # 1. Hard constraints: absolute veto
        if not hard_check.get("passed"):
            return {
                "decision": "reject",
                "reason": "hard_constraint_violation",
                "violations": hard_check.get("violations", []),
                "composite_score": 0.0,
                "layer_scores": {},
                "audit": self._build_audit(hard_check, layer_a, layer_b, layer_c),
            }

        # 2. Extract scores from each layer
        a_score = float(layer_a.get("economic_rationale_score") or 0.5)
        b_score = float(layer_b.get("quality_score") or 0.5)
        c_score = self._committee_to_score(layer_c)

        # 3. Weighted fusion
        w_a = self._config["layer_a_weight"]
        w_b = self._config["layer_b_weight"]
        w_c = self._config["layer_c_weight"]

        composite_score = (a_score * w_a + b_score * w_b + c_score * w_c)

        # 4. Layer A veto: if LLM detects critical issues
        if not layer_a.get("passed") and layer_a.get("confidence", 0) > 0.8:
            composite_score *= 0.5  # Heavily penalize

        # 5. Decision
        approve_threshold = self._config["approve_threshold"]
        observe_threshold = self._config["observe_threshold"]

        if composite_score >= approve_threshold:
            decision = "approve"
        elif composite_score >= observe_threshold:
            decision = "observe"
        else:
            decision = "reject"

        # 6. Build result
        return {
            "decision": decision,
            "composite_score": round(composite_score, 4),
            "layer_scores": {
                "layer_a": round(a_score, 4),
                "layer_b": round(b_score, 4),
                "layer_c": round(c_score, 4),
            },
            "weights": {"a": w_a, "b": w_b, "c": w_c},
            "thresholds": {
                "approve": approve_threshold,
                "observe": observe_threshold,
            },
            "reasoning": self._build_reasoning(layer_a, layer_b, layer_c, decision),
            "audit": self._build_audit(hard_check, layer_a, layer_b, layer_c),
        }

    def _committee_to_score(self, layer_c: dict[str, Any]) -> float:
        """Convert committee decision to numeric score."""
        if layer_c.get("skipped"):
            return 0.3  # Below threshold, conservative
        decision = str(layer_c.get("decision") or "observe").lower()
        confidence = float(layer_c.get("confidence") or 0.5)
        base = {"approve": 0.85, "observe": 0.5, "reject": 0.15}.get(decision, 0.5)
        # Modulate by confidence
        return base * (0.5 + confidence * 0.5)

    def _build_reasoning(self, layer_a, layer_b, layer_c, decision) -> str:
        """Build human-readable reasoning."""
        parts = []
        if layer_a.get("issues"):
            parts.append(f"结构审查发现 {len(layer_a['issues'])} 个问题")
        if float(layer_b.get("quality_score") or 0) > 0.7:
            parts.append("ML 模型预测质量较高")
        elif float(layer_b.get("quality_score") or 0) < 0.3:
            parts.append("ML 模型预测质量较低")
        if layer_c.get("decision") == "approve":
            parts.append("审查委员会建议通过")
        elif layer_c.get("decision") == "reject":
            parts.append("审查委员会建议拒绝")
        return "；".join(parts) if parts else f"综合评分决策：{decision}"

    def _build_audit(self, hard_check, layer_a, layer_b, layer_c) -> dict[str, Any]:
        """Build complete audit trail for compliance."""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "hard_constraints": {
                "passed": hard_check.get("passed"),
                "violations": hard_check.get("violations", []),
            },
            "layer_a": {
                "model": layer_a.get("model_used"),
                "passed": layer_a.get("passed"),
                "strategy_type_detected": layer_a.get("strategy_type_detected"),
                "confidence": layer_a.get("confidence"),
                "issues_count": len(layer_a.get("issues") or []),
            },
            "layer_b": {
                "model": layer_b.get("model_used"),
                "quality_score": layer_b.get("quality_score"),
            },
            "layer_c": {
                "model": layer_c.get("model_used"),
                "decision": layer_c.get("decision"),
                "confidence": layer_c.get("confidence"),
                "skipped": layer_c.get("skipped", False),
            },
        }
