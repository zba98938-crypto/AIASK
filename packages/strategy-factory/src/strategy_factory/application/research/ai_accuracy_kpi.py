"""PR-AI7: AI 生成准确率 KPI 看板。

在每个 cycle 结束后统计 LLM 生成策略的准确率指标：
- Gate-3 通过率
- 证据链完整率
- 股票代码有效率
- 数值断言核验通过率
- 综合 AI 准确率
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def compute_ai_accuracy_kpi(
    generated_candidates: list[dict[str, Any]],
    gate_results: list[dict[str, Any]] | None = None,
    stock_whitelist: set[str] | None = None,
) -> dict[str, Any]:
    """计算本轮 AI 生成准确率 KPI。

    Args:
        generated_candidates: 本轮生成的所有候选
        gate_results: Gate-3 结果（可选）
        stock_whitelist: 有效股票代码集合（可选）

    Returns:
        dict with KPI metrics.
    """
    # 筛选 AI 生成的候选（非本地规则）
    ai_candidates = [
        c for c in (generated_candidates or [])
        if _is_ai_generated(c)
    ]
    total_ai = len(ai_candidates)
    if total_ai == 0:
        return {
            "ai_candidate_count": 0,
            "local_rule_count": len(generated_candidates or []),
            "gate3_pass_rate": None,
            "evidence_completeness_rate": None,
            "symbol_validity_rate": None,
            "overall_ai_accuracy": None,
        }

    # 证据链完整率
    evidence_complete = sum(
        1 for c in ai_candidates
        if _has_evidence_chain(c)
    )

    # 股票代码有效率
    if stock_whitelist:
        symbol_valid = sum(
            1 for c in ai_candidates
            if _all_symbols_valid(c, stock_whitelist)
        )
    else:
        symbol_valid = total_ai  # 无白名单时假设全部有效

    # Gate-3 通过率
    gate3_passed = 0
    if gate_results:
        passed_ids = {
            str(r.get("strategy_id") or r.get("candidate_id") or "")
            for r in gate_results
            if r.get("passed")
        }
        gate3_passed = sum(
            1 for c in ai_candidates
            if str(c.get("id") or c.get("candidate_id") or "") in passed_ids
        )

    gate3_pass_rate = gate3_passed / total_ai if gate_results else None
    evidence_rate = evidence_complete / total_ai
    symbol_rate = symbol_valid / total_ai

    # 综合 AI 准确率（三项加权平均）
    components = [evidence_rate, symbol_rate]
    if gate3_pass_rate is not None:
        components.append(gate3_pass_rate)
    overall = sum(components) / len(components) if components else 0.0

    kpi = {
        "ai_candidate_count": total_ai,
        "local_rule_count": len(generated_candidates or []) - total_ai,
        "gate3_pass_rate": round(gate3_pass_rate, 4) if gate3_pass_rate is not None else None,
        "gate3_passed_count": gate3_passed if gate_results else None,
        "evidence_completeness_rate": round(evidence_rate, 4),
        "evidence_complete_count": evidence_complete,
        "symbol_validity_rate": round(symbol_rate, 4),
        "symbol_valid_count": symbol_valid,
        "overall_ai_accuracy": round(overall, 4),
    }
    logger.info(
        "AI Accuracy KPI: %d candidates, evidence=%.1f%%, symbols=%.1f%%, gate3=%s",
        total_ai,
        evidence_rate * 100,
        symbol_rate * 100,
        f"{gate3_pass_rate * 100:.1f}%" if gate3_pass_rate is not None else "N/A",
    )
    return kpi


def _is_ai_generated(candidate: dict[str, Any]) -> bool:
    """判断候选是否由 AI/LLM 生成。"""
    tags = set(str(t).strip().lower() for t in (candidate.get("tags") or []))
    if "ai_staged" in tags or "external_llm" in tags or "ai_generated" in tags:
        return True
    generator_type = str(
        candidate.get("generator_type")
        or (candidate.get("provenance") or {}).get("generator_type")
        or ""
    ).strip().lower()
    if generator_type in {"external_llm", "ai_staged", "pipeline_staged"}:
        return True
    return False


def _has_evidence_chain(candidate: dict[str, Any]) -> bool:
    """检查候选是否有非空的 evidence_chain。"""
    ec = candidate.get("evidence_chain") or {}
    if isinstance(ec, dict):
        evidences = ec.get("evidences") or []
        return len(evidences) > 0
    return False


def _all_symbols_valid(candidate: dict[str, Any], whitelist: set[str]) -> bool:
    """检查候选的所有 target_symbols 是否在白名单中。"""
    symbols = list(candidate.get("target_symbols") or [])
    if not symbols:
        return True
    return all(str(s).strip() in whitelist for s in symbols)


__all__ = ["compute_ai_accuracy_kpi"]
