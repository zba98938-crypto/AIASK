"""活跃因子池 — 因子挖掘工厂的核心产出物。

生命周期：candidate → validated → active → decaying → retired
"""

from __future__ import annotations

import json
import hashlib
from copy import deepcopy

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..engines.base import FactorCandidate
from ..quality import (
    PROMOTION_CRITERIA,
    compute_quality_score,
    evaluate_validation_evidence,
    safe_int,
    unmeasured_seed_reason,
)

logger = logging.getLogger(__name__)


class ActiveFactorPool:
    """活跃因子池。

    职责：
    - 入池审核（正交化检查 + 增量信息比）
    - 衰减追踪
    - 自动退役
    - 因子权重管理
    - 为策略工厂提供因子列表
    """

    MAX_POOL_SIZE = 200
    ORTHOGONALITY_THRESHOLD = 0.7
    DECAY_ALERT_THRESHOLD = 0.3
    RETIRE_THRESHOLD = 0.5
    # 最小复杂度门槛：拒绝 ``close``/``ts_mean(open,10)`` 这种最简表达式入池，
    # 即使它们偶然 IC 不为 0；真实 alpha 至少需要一个时序算子或多字段。
    MIN_EXPRESSION_COMPLEXITY = 8
    MIN_VALIDATION_SAMPLE_DATES = 60

    def __init__(self):
        self._factors: dict[str, dict[str, Any]] = {}  # factor_id → factor record
        self._knowledge_graph = None  # lazy-init to avoid circular imports
        self.correlation_evaluator = None

    def replace_from_records(self, records: list[dict[str, Any]]) -> None:
        """Replace the working set, including removing factors retired elsewhere."""
        refreshed = ActiveFactorPool()
        refreshed.hydrate(records)
        self._factors = refreshed._factors

    def hydrate(self, records: list[dict[str, Any]]) -> None:
        """Load persisted active factors into the in-memory working set."""
        for record in records or []:
            factor_id = str(record.get("factor_id") or "").strip()
            if not factor_id or record.get("status", "active") != "active":
                continue
            from ...factor_evidence_identity import requires_bound_evidence, resolve_factor_evidence_identity
            if requires_bound_evidence(record) and resolve_factor_evidence_identity(record).get("status") != "bound":
                logger.warning("ActiveFactorPool: excluded unbound mined factor %s; stored history retained", factor_id)
                continue
            seed_reason = unmeasured_seed_reason(record)
            if seed_reason:
                logger.warning("ActiveFactorPool: excluded %s (%s); stored history retained", factor_id, seed_reason)
                continue
            raw_inputs = record.get("inputs") or []
            if isinstance(raw_inputs, str):
                try:
                    raw_inputs = json.loads(raw_inputs)
                except Exception:
                    raw_inputs = []
            if not isinstance(raw_inputs, list):
                raw_inputs = list(raw_inputs) if raw_inputs else []
            gate = self._compile_gate(
                name=str(record.get("name") or factor_id),
                hypothesis=str(record.get("hypothesis") or "persisted factor"),
                family=str(record.get("family") or "custom"),
                inputs=list(raw_inputs or []),
                expression_dsl=str(record.get("expression_dsl") or ""),
            )
            if not gate.get("passed"):
                logger.info(
                    "ActiveFactorPool: skipped persisted factor %s (%s)",
                    factor_id,
                    gate.get("reason"),
                )
                continue
            self._factors[factor_id] = dict(record)

    def _ensure_kg(self):
        """Lazy-import the FactorKnowledgeGraph the first time it's needed.

        The graph tracks family / parent / sibling relationships between
        admitted factors so future search rounds can avoid redundant
        candidates. Lazy initialization keeps startup cost zero when the
        factory is imported but never run.
        """
        if self._knowledge_graph is None:
            try:
                from ..feedback.knowledge_graph import FactorKnowledgeGraph
                self._knowledge_graph = FactorKnowledgeGraph()
            except Exception as exc:
                logger.warning(
                    "ActiveFactorPool: knowledge graph unavailable (%s); "
                    "admit will skip graph updates",
                    exc,
                )
                self._knowledge_graph = False  # sentinel for "tried and failed"
        return self._knowledge_graph if self._knowledge_graph is not False else None

    @classmethod
    def _compile_gate(
        cls,
        *,
        name: str,
        hypothesis: str,
        family: str,
        inputs: list[Any],
        expression_dsl: str,
    ) -> dict[str, Any]:
        try:
            from ...factor_candidate_compiler import compile_factor_candidate

            compiled = compile_factor_candidate(
                {
                    "name": name,
                    "hypothesis": hypothesis or "factor candidate",
                    "family": family or "custom",
                    "inputs": inputs or ["close"],
                    "expression_dsl": expression_dsl,
                }
            )
            complexity_score = int(
                (compiled.get("complexity") or {}).get("score") or 0
            )
            functions = list(compiled.get("function_calls") or [])
            if not compiled.get("valid"):
                return {
                    "passed": False,
                    "reason": "compile_invalid",
                    "complexity_score": complexity_score,
                    "function_calls": len(functions),
                }
            if (
                complexity_score < cls.MIN_EXPRESSION_COMPLEXITY
                or len(functions) == 0
            ):
                return {
                    "passed": False,
                    "reason": "expression_too_simple",
                    "complexity_score": complexity_score,
                    "min_required": cls.MIN_EXPRESSION_COMPLEXITY,
                    "function_calls": len(functions),
                }
            return {
                "passed": True,
                "complexity_score": complexity_score,
                "function_calls": len(functions),
            }
        except Exception as exc:
            return {
                "passed": False,
                "reason": "compile_gate_failed",
                "error": str(exc),
            }

    @property
    def size(self) -> int:
        return len(self._factors)

    @property
    def family_distribution(self) -> dict[str, int]:
        """因子家族分布。"""
        dist: dict[str, int] = {}
        for record in self._factors.values():
            family = record.get("family", "custom")
            dist[family] = dist.get(family, 0) + 1
        return dist

    @property
    def avg_decay_rate(self) -> float:
        """平均衰减率。"""
        rates = [r.get("decay_rate", 0.0) for r in self._factors.values()]
        return sum(rates) / len(rates) if rates else 0.0

    @classmethod
    def _validation_evidence_gate(
        cls,
        validation_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Require real cross-section IC evidence before active-pool admission."""
        evidence = evaluate_validation_evidence(validation_result)
        if not evidence.get("passed"):
            return {
                "passed": False,
                "reason": "insufficient_ic_evidence",
                "reasons": list(evidence.get("reasons") or []),
                **dict(evidence.get("summary") or {}),
            }
        return {"passed": True, **dict(evidence.get("summary") or {})}

    async def admit_batch(self, candidates: list[FactorCandidate]) -> list[dict[str, Any]]:
        """批量入池审核。"""
        admitted = []
        for candidate in candidates:
            try:
                result = await self.admit(candidate)
            except Exception as exc:
                logger.exception("ActiveFactorPool: admission failed for %s", candidate.name)
                result = {"admitted": False, "reason": "pool_admission_error",
                          "error_type": type(exc).__name__, "error": str(exc)}
            identity_payload = {"expression_dsl": candidate.expression_dsl, "inputs": sorted(candidate.inputs)}
            from ...factor_evidence_identity import resolve_factor_evidence_identity

            evidence_identity = resolve_factor_evidence_identity(candidate.to_validation_dict(), candidate.validation_result or {})
            decision = deepcopy({k: v for k, v in result.items() if k not in {"record", "retired_records"}})
            decision.update({
                "attempted": True, "status": "admitted" if result.get("admitted") else "rejected",
                "candidate_name": candidate.name,
                "generation_engine": candidate.generation_engine,
                "evidence_identity": evidence_identity,
                "candidate_expression_sha256": hashlib.sha256(json.dumps(identity_payload, sort_keys=True).encode()).hexdigest(),
                "retired_factor_ids": [r.get("factor_id") for r in result.get("retired_records") or []],
            })
            candidate.generation_trace = {**dict(candidate.generation_trace or {}), "pool_admission": decision}
            if isinstance(result.get("record"), dict):
                result["record"]["generation_trace"] = deepcopy(candidate.generation_trace)
            if result.get("admitted"):
                admitted.append(result)
        return admitted

    async def admit(self, candidate: FactorCandidate) -> dict[str, Any]:
        """单个因子入池审核。"""
        factor_id = f"factor_{int(datetime.now().timestamp())}_{uuid4().hex[:6]}"

        # 0. 最小复杂度门槛：拒绝过简表达式
        try:
            from ...factor_candidate_compiler import compile_factor_candidate

            compiled = compile_factor_candidate(candidate.to_validation_dict())
            complexity_score = int(
                (compiled.get("complexity") or {}).get("score") or 0
            )
            functions = list(compiled.get("function_calls") or [])
            if not compiled.get("valid"):
                return {
                    "admitted": False,
                    "reason": "compile_invalid",
                    "complexity_score": complexity_score,
                    "function_calls": len(functions),
                }
            try:
                from ..quality import QUALITY_PROFILE_ACTIVE as _cx_profile
            except Exception:
                _cx_profile = "strict"
            min_complexity = int(self.MIN_EXPRESSION_COMPLEXITY)
            if str(_cx_profile or "strict").lower() in {"lite", "minimum"}:
                # Research track: allow compact seed expressions with >=1 function call.
                min_complexity = max(1, min_complexity // 2)
            if complexity_score < min_complexity or len(functions) == 0:
                # Allow plain market-field seeds with operators under lite (e.g. -return_5d).
                expr = str(candidate.expression_dsl or "")
                lite_ok = (
                    str(_cx_profile or "strict").lower() in {"lite", "minimum"}
                    and any(token in expr for token in ("return", "momentum", "zscore", "ts_", "volume", "vol"))
                    and len(expr.strip()) >= 6
                )
                if not lite_ok:
                    return {
                        "admitted": False,
                        "reason": "expression_too_simple",
                        "complexity_score": complexity_score,
                        "min_required": min_complexity,
                        "function_calls": len(functions),
                    }
        except Exception as exc:
            logger.debug(
                "ActiveFactorPool: compile gate failed for %s: %s",
                candidate.name,
                exc,
            )
            return {
                "admitted": False,
                "reason": "compile_gate_failed",
                "error": str(exc),
            }

        # 1. IC evidence gate (strict by default; lite may use quick-screen pass)
        from ...factor_evidence_identity import resolve_factor_evidence_identity

        identity = resolve_factor_evidence_identity(candidate.to_validation_dict(), candidate.validation_result or {})
        if identity.get("status") != "bound":
            return {"admitted": False, "reason": identity["reason"], "evidence_identity": identity}
        evidence_gate = self._validation_evidence_gate(candidate.validation_result)
        if not evidence_gate.get("passed"):
            try:
                from ..quality import QUALITY_PROFILE_ACTIVE as _pool_profile
            except Exception:
                _pool_profile = "strict"
            quick = dict(getattr(candidate, "quick_evidence", None) or {})
            gen_trace = dict(getattr(candidate, "generation_trace", None) or {})
            lite_ok = (
                str(_pool_profile or "strict").lower() in {"lite", "minimum"}
                and bool(quick.get("passed"))
                and (
                    bool(gen_trace.get("lite_admission_via_quick_evidence"))
                    or abs(float(quick.get("rank_ic_mean") or 0.0)) >= 0.015
                )
            )
            if not lite_ok:
                return {"admitted": False, **evidence_gate}
            evidence_gate = {
                "passed": True,
                "reason": "lite_quick_evidence_admission",
                "strict_reasons": list(evidence_gate.get("reasons") or []),
                "quick_rank_ic_mean": quick.get("rank_ic_mean"),
                "quick_rank_ic_ir": quick.get("rank_ic_ir"),
            }

        generation_trace = dict(getattr(candidate, "generation_trace", None) or {})
        quality_score = compute_quality_score(
            candidate.validation_result,
            structural_score=float(
                generation_trace.get("evolution_structural_score", 0.0) or 0.0
            ),
        )
        # Text overlap is useful for search diagnostics, but cannot establish
        # independence. Admission requires measured factor values on common
        # (trade date, stock) observations, supplied by the host's DB-only loader.
        if self._factors:
            if self.correlation_evaluator is None:
                return {"admitted": False, "reason": "orthogonality_unavailable"}
            similarity = await self.correlation_evaluator(candidate, list(self._factors.values()))
            if not similarity.get("measurable"):
                return {
                    "admitted": False,
                    "reason": "orthogonality_unmeasurable",
                    "orthogonality": similarity,
                }
        else:
            similarity = {"measurable": True, "max_correlation": 0.0, "basis": "empty_pool"}
        max_corr = float(similarity["max_correlation"])
        existing_quality = float(similarity.get("existing_quality_score") or 0.0)
        incremental_quality_score = round(quality_score - existing_quality, 4)
        replacement_decision = {
            "action": "none",
            "reason": "correlation_below_threshold",
            "threshold": self.ORTHOGONALITY_THRESHOLD,
            "max_correlation": max_corr,
            "similar_factor_id": similarity.get("factor_id"),
            "candidate_quality_score": quality_score,
            "existing_quality_score": existing_quality,
            "incremental_quality_score": incremental_quality_score,
        }
        retired_records: list[dict[str, Any]] = []
        if max_corr > self.ORTHOGONALITY_THRESHOLD:
            if incremental_quality_score < 10.0:
                replacement_decision["action"] = "reject"
                replacement_decision["reason"] = "redundant_without_quality_improvement"
                return {
                    "admitted": False,
                    "reason": "redundant",
                    "max_correlation": max_corr,
                    "correlation_to_pool": max_corr,
                    "incremental_quality_score": incremental_quality_score,
                    "replacement_decision": replacement_decision,
                }
            replacement_decision["action"] = "replace"
            replacement_decision["reason"] = "candidate_quality_materially_better"
            if similarity.get("factor_id"):
                retired = self._retire(str(similarity.get("factor_id")), reason="quality_replacement")
                if retired:
                    retired_records.append(retired)

        # 2. 池容量检查
        if self.size >= self.MAX_POOL_SIZE:
            weakest = self._find_weakest()
            if weakest and candidate.fitness > weakest.get("fitness", 0):
                retired = self._retire(weakest["factor_id"], reason="replaced")
                if retired:
                    retired_records.append(retired)
            else:
                return {"admitted": False, "reason": "pool_full"}

        # 3. 入池
        metrics = dict((candidate.validation_result or {}).get("metrics") or {})
        cross_section = dict((candidate.validation_result or {}).get("cross_section") or {})
        summary = dict(cross_section.get("summary") or {})
        admission_ic = self._metric_float(
            metrics.get("rank_ic_mean"),
            summary.get("rank_ic_mean"),
            metrics.get("normal_ic_mean"),
            summary.get("normal_ic_mean"),
        )
        evidence = evaluate_validation_evidence(candidate.validation_result)
        try:
            from ..quality import QUALITY_PROFILE_ACTIVE as _admit_status_profile
        except Exception:
            _admit_status_profile = "strict"
        quick = dict(getattr(candidate, "quick_evidence", None) or {})
        gen_trace = dict(getattr(candidate, "generation_trace", None) or {})
        rating = dict((candidate.validation_result or {}).get("rating") or {})
        grade = str(rating.get("grade") or "").upper()
        promote_active = False
        if str(_admit_status_profile or "strict").lower() in {"lite", "minimum"}:
            # Research-track consumable admission: strong quick evidence + non-hard governance.
            if bool(quick.get("passed")) and grade in {"A", "B", "C"}:
                promote_active = True
            if bool(gen_trace.get("lite_admission_via_quick_evidence")):
                promote_active = True
        if grade in {"A", "B"} and evidence.get("passed"):
            promote_active = True
        quality_status = "promoted" if promote_active else "quarantine"
        pool_status = "active" if promote_active else "quarantine"
        validation_summary = {
            "metrics": metrics,
            "rating": rating,
            "cross_section_summary": summary,
            "persisted_outputs": dict((candidate.validation_result or {}).get("persisted_outputs") or {}),
            "warnings": list((candidate.validation_result or {}).get("warnings") or [])[:20],
            "quality_status": quality_status,
            "quality_score": quality_score,
            "incremental_quality_score": incremental_quality_score,
            "correlation_to_pool": max_corr,
            "orthogonality": similarity,
            "replacement_decision": replacement_decision,
            "evidence_summary": dict(evidence.get("summary") or {}),
            "promotion_criteria": dict(PROMOTION_CRITERIA),
            "economic_hypothesis": getattr(candidate, "economic_hypothesis", "") or candidate.hypothesis,
            "blueprint_id": getattr(candidate, "blueprint_id", ""),
            "risk_exposure_hint": dict(getattr(candidate, "risk_exposure_hint", None) or {}),
            "quick_evidence": dict(getattr(candidate, "quick_evidence", None) or {}),
        }

        record = {
            "factor_id": factor_id,
            "name": candidate.name,
            "family": getattr(candidate, "factor_family", "") or candidate.family,
            "expression_dsl": candidate.expression_dsl,
            "inputs": candidate.inputs,
            "hypothesis": getattr(candidate, "economic_hypothesis", "") or candidate.hypothesis,
            "status": pool_status,
            "fitness": quality_score,
            "admission_date": datetime.now(timezone.utc).isoformat(),
            "admission_ic": admission_ic,
            "current_ic": admission_ic,
            "admission_grade": (candidate.validation_result or {}).get("rating", {}).get("grade", ""),
            "generation_engine": candidate.generation_engine,
            "generation_trace": generation_trace,
            "validation_summary": validation_summary,
            "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
            "decay_rate": 0.0,
            "pool_weight": 1.0 / max(1, self.size + 1),
        }
        # Update the factor knowledge graph so subsequent search rounds can
        # detect redundancy by family / expression-overlap signal beyond
        # the simple text similarity gate above.
        kg = self._ensure_kg()
        if kg is not None:
            try:
                if hasattr(kg, "add_factor"):
                    kg.add_factor(record)
                elif hasattr(kg, "add_node"):
                    kg.add_node(record)
            except Exception as exc:
                logger.debug(
                    "ActiveFactorPool: knowledge graph add_factor failed: %s", exc
                )

        logger.info(
            "ActiveFactorPool: admitted %s (%s) status=%s quality_score=%.4f pool_size=%d",
            candidate.name,
            factor_id,
            pool_status,
            quality_score,
            self.size + 1,
        )
        # Keep in-memory active map for status=active only.
        if pool_status == "active":
            self._factors[factor_id] = dict(record)
        return {
            "admitted": True,
            "quarantined": pool_status == "quarantine",
            "promoted_active": pool_status == "active",
            "factor_id": factor_id,
            "record": record,
            "retired_records": retired_records,
        }

    @staticmethod
    def _metric_float(*values: Any) -> float | None:
        for value in values:
            try:
                if value is None:
                    continue
                return float(value)
            except (TypeError, ValueError) as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/pool/active_pool.py:448: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue
        return None

    @staticmethod
    def _safe_int(value: Any) -> int:
        return safe_int(value)

    async def get_active_factors(
        self,
        *,
        families: list[str] | None = None,
        min_grade: str = "B",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取活跃因子列表（供策略工厂消费）。

        ALPHA-WIRING-V1 (P-B)：默认仍只放行 quality_status=='promoted' 的因子（零变化）。
        当 STRATEGY_FACTORY_FACTOR_POOL_ADMIT_ACTIVE_WITHOUT_PROMOTION=1 时，放宽为
        “status=active 且有非空 expression_dsl” 即可放行——用于消费已挖出但尚未走完
        promotion 流程（quality_status 缺失/null）的因子，避免高 IC 因子被卡在池里。
        """
        admit_active_without_promotion = str(
            os.getenv("STRATEGY_FACTORY_FACTOR_POOL_ADMIT_ACTIVE_WITHOUT_PROMOTION") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        results = []
        for record in self._factors.values():
            if record.get("status") != "active":
                continue
            if unmeasured_seed_reason(record):
                continue
            from ...factor_evidence_identity import requires_bound_evidence, resolve_factor_evidence_identity
            if requires_bound_evidence(record) and resolve_factor_evidence_identity(record).get("status") != "bound":
                continue
            validation_summary = dict(record.get("validation_summary") or {})
            quality_status = validation_summary.get("quality_status")
            if quality_status != "promoted":
                if not admit_active_without_promotion:
                    continue
                # 放宽分支：要求有可编译的因子表达式，避免放行空壳。
                if not str(record.get("expression_dsl") or "").strip():
                    continue
                if quality_status == "quarantine":
                    # quarantine 是显式淘汰态，放宽模式下仍不放行。
                    continue
            if families and record.get("family") not in families:
                continue
            results.append(record)

        results.sort(key=lambda x: -(x.get("fitness", 0)))
        return results[:limit]

    async def get_decay_monitored_factors(
        self,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return factors whose IC decay should be monitored."""
        results = [
            record
            for record in self._factors.values()
            if record.get("status") in {"active", "quarantine"}
        ]
        results.sort(key=lambda x: -(x.get("fitness", 0)))
        return results[:limit]

    async def update_decay(
        self,
        factor_id: str,
        decay_rate: float,
        *,
        current_ic: float | None = None,
    ) -> dict[str, Any]:
        """Update the in-memory decay fields for an active factor."""
        fid = str(factor_id or "").strip()
        if not fid or fid not in self._factors:
            return {"updated": False, "reason": "factor_not_found", "factor_id": fid}

        record = self._factors[fid]
        record["decay_rate"] = float(decay_rate or 0.0)
        if current_ic is not None:
            record["current_ic"] = float(current_ic)
        record["last_evaluated_at"] = datetime.now(timezone.utc).isoformat()
        return {"updated": True, "factor_id": fid, "record": dict(record)}

    def _max_expression_similarity(self, expression: str) -> float:
        """计算与池中因子的最大文本相似度。"""
        import re

        if not self._factors:
            return 0.0

        tokens_new = set(re.findall(r"[a-zA-Z_]\w*|\d+", expression.lower()))
        if not tokens_new:
            return 0.0

        max_sim = 0.0
        for record in self._factors.values():
            existing_expr = record.get("expression_dsl", "")
            tokens_existing = set(re.findall(r"[a-zA-Z_]\w*|\d+", existing_expr.lower()))
            if not tokens_existing:
                continue
            intersection = len(tokens_new & tokens_existing)
            union = len(tokens_new | tokens_existing)
            sim = intersection / union if union > 0 else 0.0
            max_sim = max(max_sim, sim)

        return max_sim

    def _expression_similarity_details(self, expression: str) -> dict[str, Any]:
        """Return diagnostic text similarity; never use it as correlation."""
        import re

        if not self._factors:
            return {
                "max_similarity": 0.0,
                "factor_id": None,
                "existing_quality_score": 0.0,
            }

        tokens_new = set(re.findall(r"[a-zA-Z_]\w*|\d+", expression.lower()))
        if not tokens_new:
            return {
                "max_similarity": 0.0,
                "factor_id": None,
                "existing_quality_score": 0.0,
            }

        max_sim = 0.0
        best_record: dict[str, Any] | None = None
        for record in self._factors.values():
            existing_expr = record.get("expression_dsl", "")
            tokens_existing = set(re.findall(r"[a-zA-Z_]\w*|\d+", existing_expr.lower()))
            if not tokens_existing:
                continue
            intersection = len(tokens_new & tokens_existing)
            union = len(tokens_new | tokens_existing)
            sim = intersection / union if union > 0 else 0.0
            if sim > max_sim:
                max_sim = sim
                best_record = record

        validation_summary = dict((best_record or {}).get("validation_summary") or {})
        existing_quality = self._metric_float(
            validation_summary.get("quality_score"),
            (best_record or {}).get("fitness"),
        )
        return {
            "max_similarity": max_sim,
            "factor_id": (best_record or {}).get("factor_id"),
            "existing_quality_score": float(existing_quality or 0.0),
        }

    def _find_weakest(self) -> dict[str, Any] | None:
        """找到池中最弱的因子。"""
        if not self._factors:
            return None
        return min(self._factors.values(), key=lambda x: x.get("fitness", 0))

    def _retire(self, factor_id: str, reason: str = "manual") -> dict[str, Any] | None:
        """退役因子。"""
        if factor_id not in self._factors:
            return None
        record = dict(self._factors[factor_id])
        record["status"] = "retired"
        record["retired_at"] = datetime.now(timezone.utc).isoformat()
        record["retired_reason"] = reason
        validation_summary = dict(record.get("validation_summary") or {})
        validation_summary["quality_status"] = "retired"
        validation_summary["retired_reason"] = reason
        record["validation_summary"] = validation_summary
        del self._factors[factor_id]
        logger.info("ActiveFactorPool: retired %s reason=%s", factor_id, reason)
        return record
