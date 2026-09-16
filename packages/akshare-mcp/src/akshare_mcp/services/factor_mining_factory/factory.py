"""Factor mining factory orchestration.

The factory consumes market data only through the SQLite storage adapter. TDX
is upstream of this component and may only populate the DB through sync tasks.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .quality import compute_quality_score, evaluate_validation_evidence

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return int(default)


def _strict_validation_candidate_limit() -> int:
    return max(0, _env_int("FACTOR_MINING_STRICT_VALIDATION_CANDIDATE_LIMIT", 0))


class FactorMiningFactory:
    """Full factor mining cycle: search, evolve, validate, admit, persist."""

    def __init__(self):
        self._initialized = False
        self._engine_scheduler = None
        self._evolutionary_optimizer = None
        self._active_pool = None
        self._decay_monitor = None
        self._meta_learner = None
        self._last_run_at: Optional[datetime] = None
        self._run_count: int = 0
        self._pool_loaded_from_db = False

    def _ensure_initialized(self):
        if self._initialized:
            return
        from .engines import EngineScheduler
        from .evolution.optimizer import EvolutionaryOptimizer
        from .pool.active_pool import ActiveFactorPool
        from .feedback.decay_monitor import DecayMonitor
        from .feedback.meta_learner import FactorMetaLearner

        self._engine_scheduler = EngineScheduler()
        self._evolutionary_optimizer = EvolutionaryOptimizer()
        self._active_pool = ActiveFactorPool()
        self._decay_monitor = DecayMonitor()
        self._meta_learner = FactorMetaLearner()
        self._initialized = True

    async def _get_db(self):
        from ...storage import get_db

        db = get_db()
        await db.initialize()
        return db

    async def _ensure_persistent_pool(self, db) -> None:
        from .pool.storage import ensure_factor_pool_tables, load_active_pool_from_db

        await ensure_factor_pool_tables(db)
        if self._pool_loaded_from_db:
            return
        records = await load_active_pool_from_db(db)
        hydrate = getattr(self._active_pool, "hydrate", None)
        if callable(hydrate):
            hydrate(records)
        self._pool_loaded_from_db = True

    async def run_mining_cycle(
        self,
        *,
        trigger: str = "scheduled",
        engines: list[str] | None = None,
        candidate_count: int = 30,
        evolution_generations: int = 5,
        codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Compat entrypoint delegating canonical orchestration to strategy-factory."""
        from strategy_factory.runtime.factor_mining import build_factor_mining_runtime

        runtime = build_factor_mining_runtime(support=self)
        return await runtime.run_once(
            trigger=trigger,
            engines=engines,
            candidate_count=candidate_count,
            evolution_generations=evolution_generations,
            codes=codes,
        )

    async def run_maintenance(self) -> dict[str, Any]:
        """Compat entrypoint delegating canonical maintenance orchestration."""
        from strategy_factory.runtime.factor_mining import build_factor_mining_runtime

        runtime = build_factor_mining_runtime(support=self)
        return await runtime.run_maintenance()

    async def _run_qc_pipeline(self, db) -> dict[str, Any]:
        """P2-1：toggle ON 时，对活跃因子池跑残酷质检流水线并打标签/(可选)自动上下架。

        默认 OFF（qc_pipeline_enabled() False）→ 直接返回 skipped，零变化。
        runner 由现有 MCP 工具构造（validate_factor_oos / backtest_factor /
        factor_robustness_check），closure 捕获验证 universe；任一工具失败该项跳过。
        """
        from .qc_pipeline import (
            apply_qc_to_record,
            qc_autoshelf_enabled,
            qc_pipeline_enabled,
            run_factor_qc,
        )

        if not qc_pipeline_enabled():
            return {"enabled": False, "skipped": True}

        from .pool.storage import load_factor_pool_from_db, save_factor_to_pool

        try:
            from ...tools.quant_analysis import (
                run_factor_group_backtest,
                run_factor_oos_validation,
            )
            from ...tools._quant_analysis_support import run_factor_robustness_check
        except Exception as exc:  # noqa: BLE001
            logger.warning("qc_pipeline: tool import failed: %s", exc)
            return {"enabled": True, "skipped": True, "reason": f"tool_import_failed:{exc}"}

        # 验证 universe：复用挖矿上下文的 validation_codes（数据充足才有意义）。
        try:
            context = await self._build_mining_context(db=db)
            codes = list(getattr(context, "validation_codes", []) or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("qc_pipeline: build context failed: %s", exc)
            codes = []
        if len(codes) < 120:
            return {"enabled": True, "skipped": True, "reason": "data_universe_insufficient"}

        rows = await load_factor_pool_from_db(db, statuses=("active", "quarantine"), limit=200)
        if not rows:
            return {"enabled": True, "skipped": True, "reason": "empty_pool"}

        def _name(rec: dict) -> str:
            return str(rec.get("name") or "").strip()

        def _oos_runner(name):
            async def _run(_factor):
                resp = await run_factor_oos_validation(codes=codes, factor=name)
                return dict(resp.get("data") or {}) if isinstance(resp, dict) else None
            return _run

        def _layered_runner(name):
            async def _run(_factor):
                resp = await run_factor_group_backtest(codes=codes, factor=name)
                return dict(resp.get("data") or {}) if isinstance(resp, dict) else None
            return _run

        def _robust_runner(name):
            async def _run(_factor):
                resp = await run_factor_robustness_check(codes=codes, factor=name)
                return dict(resp.get("data") or {}) if isinstance(resp, dict) else None
            return _run

        decisions: dict[str, int] = {}
        processed = 0
        autoshelf = qc_autoshelf_enabled()
        for record in rows:
            name = _name(record)
            if not name:
                continue
            qc_result = await run_factor_qc(
                name,
                oos_runner=_oos_runner(name),
                layered_runner=_layered_runner(name),
                robustness_runner=_robust_runner(name),
                multiple_testing_runner=None,  # multiple_testing 嵌在 OOS 输出，单列不必重复跑
            )
            updated = apply_qc_to_record(record, qc_result)
            decision = str((qc_result.get("shelf_decision") or {}).get("decision") or "unknown")
            decisions[decision] = decisions.get(decision, 0) + 1
            try:
                await save_factor_to_pool(db, updated)
            except Exception as exc:  # noqa: BLE001
                logger.warning("qc_pipeline: save failed for %s: %s", name, exc)
            processed += 1

        return {
            "enabled": True,
            "skipped": False,
            "autoshelf_applied": autoshelf,
            "processed": processed,
            "decisions": decisions,
        }

    def status(self) -> dict[str, Any]:
        return {
            "initialized": self._initialized,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "run_count": self._run_count,
            "pool_size": self._active_pool.size if self._active_pool else 0,
            "engines": self._engine_scheduler.status() if self._engine_scheduler else {},
            "pool_loaded_from_db": self._pool_loaded_from_db,
        }

    async def _build_mining_context(self, *, db, codes: list[str] | None = None) -> "MiningContext":
        from .engines.context import MiningContext

        context = await MiningContext.build(db=db, codes=codes, active_pool=self._active_pool)
        memory = {}
        if self._meta_learner is not None and hasattr(self._meta_learner, "get_pattern_memory"):
            memory = self._meta_learner.get_pattern_memory()
        persistent_memory = await self._load_persistent_pattern_memory(db)
        context.failed_pattern_memory = self._merge_pattern_memory(
            list(memory.get("failed_pattern_memory") or []),
            list(persistent_memory.get("failed_pattern_memory") or []),
        )
        context.successful_pattern_memory = self._merge_pattern_memory(
            list(memory.get("successful_pattern_memory") or []),
            list(persistent_memory.get("successful_pattern_memory") or []),
        )
        try:
            from .blueprints import AlphaBlueprintLibrary

            context.alpha_blueprints = AlphaBlueprintLibrary().build_context_blueprints(
                failed_pattern_memory=context.failed_pattern_memory,
                successful_pattern_memory=context.successful_pattern_memory,
            )
        except Exception:
            pass
        return context

    async def _load_persistent_pattern_memory(self, db, *, limit: int = 50) -> dict[str, list[dict[str, Any]]]:
        """Load compact strict-validation pattern memory from recent persisted runs."""

        rows: list[dict[str, Any]] = []
        try:
            if hasattr(db, "acquire"):
                async with db.acquire() as conn:
                    fetched = await conn.fetch(
                        """
                        SELECT report
                        FROM factor_mining_runs
                        WHERE report IS NOT NULL
                        ORDER BY id DESC
                        LIMIT $1
                        """,
                        int(limit),
                    )
                rows = [dict(row) for row in fetched or []]
            else:
                raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
                if raw_conn is not None:
                    cursor = raw_conn.execute(
                        """
                        SELECT report
                        FROM factor_mining_runs
                        WHERE report IS NOT NULL
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (int(limit),),
                    )
                    columns = [desc[0] for desc in cursor.description]
                    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as exc:
            logger.debug("FactorMiningFactory: load persistent pattern memory failed: %s", exc)
            rows = []

        success_counts: dict[str, int] = {}
        failure_counts: dict[str, int] = {}
        for row in rows:
            try:
                report = json.loads(str(row.get("report") or "{}"))
            except Exception:
                continue
            quality_summary = dict(report.get("quality_summary") or {})
            for item in list(quality_summary.get("strict_candidate_results") or []):
                if not isinstance(item, dict):
                    continue
                pattern = self._strict_memory_pattern(item)
                if not pattern:
                    continue
                outcome = self._strict_memory_outcome(item)
                if outcome is True:
                    success_counts[pattern] = success_counts.get(pattern, 0) + 1
                elif outcome is False:
                    failure_counts[pattern] = failure_counts.get(pattern, 0) + 1
            for item in list(quality_summary.get("quick_candidate_results") or []):
                if not isinstance(item, dict) or item.get("passed") is not False:
                    continue
                pattern = self._strict_memory_pattern(item)
                if pattern:
                    failure_counts[pattern] = failure_counts.get(pattern, 0) + 1

        return {
            "successful_pattern_memory": self._counter_rows(success_counts),
            "failed_pattern_memory": self._counter_rows(failure_counts),
        }

    @staticmethod
    def _strict_memory_pattern(item: dict[str, Any]) -> str:
        trace = dict(item.get("generation_trace") or {})
        for value in (
            item.get("blueprint_id"),
            trace.get("blueprint_id"),
        ):
            text = str(value or "").strip()
            if text:
                return text
        engine = str(item.get("generation_engine") or trace.get("engine_id") or "unknown").strip() or "unknown"
        family = str(item.get("family") or trace.get("factor_family") or "").strip()
        return family or engine

    @staticmethod
    def _strict_memory_outcome(item: dict[str, Any]) -> bool | None:
        decision = dict(item.get("admission_decision") or {})
        blockers = [str(value) for value in list(decision.get("blockers") or []) if str(value)]
        if bool(decision.get("admitted")) or bool(decision.get("strict_gate_passed")):
            return True
        if blockers or decision.get("evidence_reasons"):
            return False

        result = dict(item.get("validation_result") or {})
        if not result:
            return None
        rating = dict(result.get("rating") or {})
        governance = dict(rating.get("governance") or {})
        grade = str(decision.get("rating_grade") or rating.get("grade") or "")
        if grade in {"A", "B"} and not bool(governance.get("admission_blocked")):
            return True
        if grade or governance.get("admission_blocked"):
            return False
        return None

    @staticmethod
    def _merge_pattern_memory(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for group in groups:
            for row in group or []:
                if not isinstance(row, dict):
                    continue
                pattern = str(row.get("pattern") or "").strip()
                if not pattern:
                    continue
                try:
                    count = int(row.get("count") or 1)
                except Exception:
                    count = 1
                counts[pattern] = counts.get(pattern, 0) + max(1, count)
        return FactorMiningFactory._counter_rows(counts)

    @staticmethod
    def _counter_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"pattern": pattern, "count": int(count)}
            for pattern, count in sorted(
                counts.items(),
                key=lambda item: (-int(item[1]), str(item[0])),
            )[:20]
        ]

    def _build_quick_evidence_evaluator(
        self,
        db,
        context,
        *,
        max_evaluations: int,
    ):
        from .quick_evidence import QuickEvidenceEvaluator

        return QuickEvidenceEvaluator(
            db,
            codes=getattr(context, "validation_codes", None) or [],
            horizon_days=10,
            max_codes=120,
            max_evaluations=max_evaluations,
        )

    def _install_quick_evidence_evaluators(
        self,
        db,
        context,
        *,
        max_evaluations: int | None = None,
    ):
        evaluators: dict[str, Any] = {}
        if max_evaluations is None:
            try:
                from .quality import QUALITY_PROFILE_ACTIVE as _qe_profile
            except Exception:
                _qe_profile = "strict"
            # Evolution + final quick filter share one budget; lite needs headroom.
            max_evaluations = 96 if str(_qe_profile or "strict").lower() in {"lite", "minimum"} else 32

        def _evaluator_for(candidate: Any):
            engine = self._quick_evidence_engine_key(candidate)
            evaluator = evaluators.get(engine)
            if evaluator is None:
                evaluator = self._build_quick_evidence_evaluator(
                    db,
                    context,
                    max_evaluations=max_evaluations,
                )
                evaluators[engine] = evaluator
            return evaluator

        async def evaluate(candidate: Any) -> dict[str, Any]:
            return await _evaluator_for(candidate).evaluate(candidate)

        async def ic_value(candidate: Any) -> float:
            return await _evaluator_for(candidate).ic_value(candidate)

        setattr(context, "quick_evidence_evaluator", evaluate)
        setattr(context, "quick_ic_evaluator", ic_value)
        setattr(context, "quick_evidence_evaluators", evaluators)
        return ic_value

    @staticmethod
    def _quick_evidence_engine_key(candidate: Any) -> str:
        if isinstance(candidate, dict):
            direct = candidate.get("generation_engine")
            trace = dict(candidate.get("generation_trace") or {})
        else:
            direct = getattr(candidate, "generation_engine", "")
            trace = dict(getattr(candidate, "generation_trace", None) or {})
        for value in (
            direct,
            trace.get("engine_id"),
            trace.get("generation_engine"),
            trace.get("engine"),
            trace.get("source_engine"),
        ):
            key = str(value or "").strip()
            if key:
                return key
        return "unknown"

    async def _quick_filter_candidates(self, candidates: list, context) -> list:
        evaluator = getattr(context, "quick_evidence_evaluator", None)
        if evaluator is None:
            return list(candidates or [])

        import inspect

        passed = []
        for candidate in candidates or []:
            # Always re-run the final quick gate. Evolution may attach provisional
            # IC probes that should not short-circuit the admission pre-screen.
            try:
                result = evaluator(candidate)
                if inspect.isawaitable(result):
                    result = await result
                evidence = dict(result or {}) if isinstance(result, dict) else {}
                candidate.quick_evidence = evidence
            except Exception as exc:
                evidence = {
                    "available": False,
                    "passed": False,
                    "fail_reasons": [f"quick_filter_failed:{type(exc).__name__}"],
                }
                candidate.quick_evidence = evidence
            if evidence.get("passed"):
                passed.append(candidate)
        return passed

    async def _validate_batch(self, db, candidates: list, context) -> list:
        from ..factor_validation_pipeline import validate_factor_candidate_pipeline

        validated = []
        validation_codes = context.validation_codes
        candidate_list = self._rank_strict_validation_candidates(list(candidates or []))
        validation_limit = _strict_validation_candidate_limit()
        if validation_limit > 0:
            candidate_list = candidate_list[:validation_limit]
        for rank, candidate in enumerate(candidate_list, start=1):
            generation_trace = dict(getattr(candidate, "generation_trace", None) or {})
            generation_trace["strict_validation_attempted"] = True
            generation_trace["strict_validation_priority_rank"] = rank
            if validation_limit > 0:
                generation_trace["strict_validation_candidate_limit"] = validation_limit
            candidate.generation_trace = generation_trace
            try:
                result = await validate_factor_candidate_pipeline(
                    db,
                    candidate.to_validation_dict(),
                    codes=validation_codes,
                    stage="strict",
                    lookback_bars=220,
                    horizon_days=10,
                    min_cross_section=30,
                    persist_outputs=True,
                    factor_key=candidate.name,
                    persist_ic_history=True,
                )
                generation_trace = dict(getattr(candidate, "generation_trace", None) or {})
                generation_trace["strict_validation_success"] = bool(result.get("success"))
                generation_trace["strict_validation_stage"] = result.get("stage")
                candidate.generation_trace = generation_trace
                if result.get("success"):
                    candidate.validation_result = result
                    rating = result.get("rating", {})
                    generation_trace = dict(getattr(candidate, "generation_trace", None) or {})
                    candidate.fitness = compute_quality_score(
                        result,
                        structural_score=float(
                            generation_trace.get("evolution_structural_score", 0.0)
                            or 0.0
                        ),
                    )
                    grade = str(rating.get("grade") or "").strip().upper()
                    governance = dict(rating.get("governance") or {})
                    try:
                        from .quality import QUALITY_PROFILE_ACTIVE as _admit_profile
                    except Exception:
                        _admit_profile = "strict"
                    profile = str(_admit_profile or "strict").lower()
                    allowed_grades = {"A", "B"}
                    if profile in {"lite", "minimum"}:
                        allowed_grades.add("C")
                    ic_ok = self._has_admissible_ic_evidence(result)
                    quick = dict(getattr(candidate, "quick_evidence", None) or {})
                    # Free-data/dev profiles: if the bounded quick screen passed and
                    # governance is not hard-blocked, allow C/B/A admission even when
                    # full-universe strict IC collapses (common on short free panels).
                    if profile in {"lite", "minimum"} and not ic_ok and bool(quick.get("passed")):
                        ic_ok = True
                        generation_trace = dict(getattr(candidate, "generation_trace", None) or {})
                        generation_trace["lite_admission_via_quick_evidence"] = True
                        generation_trace["strict_ic_failed"] = list(
                            ((rating.get("quality_evidence") or {}).get("reasons") or [])
                        )
                        candidate.generation_trace = generation_trace
                    if (
                        grade in allowed_grades
                        and ic_ok
                        and not bool(governance.get("admission_blocked"))
                    ):
                        validated.append(candidate)
            except Exception as exc:
                generation_trace = dict(getattr(candidate, "generation_trace", None) or {})
                generation_trace["strict_validation_success"] = False
                generation_trace["strict_validation_error"] = f"{type(exc).__name__}: {exc}"
                candidate.generation_trace = generation_trace
                candidate.validation_result = {
                    "success": False,
                    "stage": "strict",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "candidate": candidate.to_validation_dict()
                    if hasattr(candidate, "to_validation_dict")
                    else {},
                }
                logger.debug("Validation failed for %s: %s", candidate.name, exc)
        return validated

    @staticmethod
    def _rank_strict_validation_candidates(candidates: list[Any]) -> list[Any]:
        """Validate the strongest quick-evidence candidates first in bounded sessions."""

        def _float(value: Any) -> float:
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _score(candidate: Any) -> tuple[float, float, float, float, float, float]:
            quick = dict(getattr(candidate, "quick_evidence", None) or {})
            summary = dict(quick.get("evidence_summary") or {})
            quality_score = _float(quick.get("quality_score"))
            rank_ic = max(
                abs(_float(quick.get("rank_ic_mean"))),
                abs(_float(summary.get("rank_ic_mean"))),
            )
            rank_ir = max(
                _float(quick.get("rank_ic_ir")),
                _float(summary.get("rank_ic_ir")),
            )
            positive_ratio = max(
                _float(quick.get("positive_ratio")),
                _float(summary.get("positive_ratio")),
            )
            sample_dates = max(
                _float(quick.get("sample_dates")),
                _float(summary.get("sample_dates")),
            )
            fitness = _float(getattr(candidate, "fitness", 0.0))
            return (
                quality_score,
                rank_ic,
                rank_ir,
                positive_ratio,
                sample_dates,
                fitness,
            )

        return sorted(candidates, key=_score, reverse=True)

    @staticmethod
    def _has_admissible_ic_evidence(result: dict[str, Any]) -> bool:
        try:
            from .quality import QUALITY_PROFILE_ACTIVE
            require_history = str(QUALITY_PROFILE_ACTIVE or "strict").lower() == "strict"
        except Exception:
            require_history = True
        return bool(
            evaluate_validation_evidence(
                result,
                require_persisted_history=require_history,
            ).get("passed")
        )

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _build_ic_evaluator(self, db, context):
        async def _evaluate(candidate) -> float:
            from ..factor_validation_pipeline import validate_factor_candidate_pipeline

            try:
                result = await validate_factor_candidate_pipeline(
                    db,
                    candidate.to_validation_dict(),
                    codes=getattr(context, "validation_codes", None) or [],
                    lookback_bars=180,
                    horizon_days=10,
                    max_dates=80,
                    min_cross_section=30,
                    persist_outputs=False,
                    persist_ic_history=False,
                )
                metrics = dict(result.get("metrics") or {})
                cross_section = dict(result.get("cross_section") or {})
                summary = dict(cross_section.get("summary") or {})
                if int(summary.get("sample_dates") or metrics.get("sample_dates") or 0) < 20:
                    return 0.0
                value = (
                    metrics.get("rank_ic_mean")
                    or summary.get("rank_ic_mean")
                    or metrics.get("normal_ic_mean")
                    or summary.get("normal_ic_mean")
                    or 0.0
                )
                return float(value or 0.0)
            except Exception as exc:
                logger.debug("IC evaluator failed for %s: %s", getattr(candidate, "name", ""), exc)
                return 0.0

        return _evaluate

    def _build_quality_summary(
        self,
        raw: list[Any],
        evolved: list[Any],
        validated: list[Any],
        admitted: list[dict[str, Any]],
        context: Any,
    ) -> dict[str, Any]:
        reject_reasons: dict[str, int] = {}
        diagnostic_counts: dict[str, int] = {}
        quick_evaluated_count = 0
        quick_passed_count = 0
        for candidate in evolved or []:
            quick = dict(getattr(candidate, "quick_evidence", None) or {})
            if quick:
                quick_evaluated_count += 1
                if quick.get("passed"):
                    quick_passed_count += 1
                else:
                    for reason in list(quick.get("fail_reasons") or []):
                        reject_reasons[str(reason)] = reject_reasons.get(str(reason), 0) + 1
                for key, value in dict(quick.get("diagnostic_counts") or {}).items():
                    diagnostic_counts[str(key)] = diagnostic_counts.get(str(key), 0) + int(value or 0)
            result = getattr(candidate, "validation_result", None)
            if not result:
                continue
            evidence = evaluate_validation_evidence(result)
            if evidence.get("passed"):
                pass
            else:
                for reason in list(evidence.get("reasons") or []):
                    reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
            coverage = dict(result.get("coverage") or {})
            for key, value in dict(coverage.get("diagnostic_counts") or {}).items():
                diagnostic_counts[str(key)] = diagnostic_counts.get(str(key), 0) + int(value or 0)

        quarantine_count = 0
        active_promoted_count = 0
        by_engine: dict[str, dict[str, int]] = {}
        by_blueprint: dict[str, dict[str, int]] = {}
        strict_candidate_results = self._strict_candidate_results(
            evolved,
            validated,
            admitted,
        )
        quick_candidate_results = self._quick_candidate_results(evolved)
        for candidate in raw or []:
            engine = str(getattr(candidate, "generation_engine", "") or "unknown")
            by_engine.setdefault(engine, {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0})
            by_engine[engine]["raw"] += 1
            blueprint = str(getattr(candidate, "blueprint_id", "") or "none")
            by_blueprint.setdefault(
                blueprint,
                {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0},
            )
            by_blueprint[blueprint]["raw"] += 1
        for candidate in evolved or []:
            engine = str(getattr(candidate, "generation_engine", "") or "unknown")
            by_engine.setdefault(engine, {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0})
            blueprint = str(getattr(candidate, "blueprint_id", "") or "none")
            by_blueprint.setdefault(
                blueprint,
                {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0},
            )
            if (getattr(candidate, "quick_evidence", None) or {}).get("passed"):
                by_engine[engine]["quick_passed"] += 1
                by_blueprint[blueprint]["quick_passed"] += 1
        for candidate in validated or []:
            engine = str(getattr(candidate, "generation_engine", "") or "unknown")
            by_engine.setdefault(engine, {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0})
            by_engine[engine]["validated"] += 1
            blueprint = str(getattr(candidate, "blueprint_id", "") or "none")
            by_blueprint.setdefault(
                blueprint,
                {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0},
            )
            by_blueprint[blueprint]["validated"] += 1
        for item in admitted or []:
            record = dict((item or {}).get("record") or {})
            engine = str(record.get("generation_engine") or "unknown")
            by_engine.setdefault(engine, {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0})
            by_engine[engine]["accepted"] += 1
            generation_trace = dict(record.get("generation_trace") or {})
            blueprint = str(generation_trace.get("blueprint_id") or "none")
            by_blueprint.setdefault(
                blueprint,
                {"raw": 0, "quick_passed": 0, "validated": 0, "accepted": 0},
            )
            by_blueprint[blueprint]["accepted"] += 1
            validation_summary = dict(record.get("validation_summary") or {})
            if validation_summary.get("quality_status") == "quarantine":
                quarantine_count += 1
            if validation_summary.get("quality_status") == "promoted":
                active_promoted_count += 1

        return {
            "raw_candidate_count": len(raw or []),
            "evolved_count": len(evolved or []),
            "quick_evaluated_count": quick_evaluated_count,
            "quick_passed_count": quick_passed_count,
            "evidence_passed_count": len(validated or []),
            "quarantine_count": quarantine_count,
            "active_promoted_count": active_promoted_count,
            "reject_reasons": reject_reasons,
            "diagnostic_counts": diagnostic_counts,
            "quality_funnel": {
                "generated": len(raw or []),
                "evolved": len(evolved or []),
                "quick_evaluated": quick_evaluated_count,
                "quick_passed": quick_passed_count,
                "strict_validated": len(validated or []),
                "quarantine": quarantine_count,
                "promoted": active_promoted_count,
            },
            "by_engine": by_engine,
            "by_blueprint": by_blueprint,
            "strict_candidate_results": strict_candidate_results,
            "quick_candidate_results": quick_candidate_results,
            "validation_universe_health": getattr(context, "validation_universe_health", {}),
        }

    def _strict_candidate_results(
        self,
        evolved: list[Any],
        validated: list[Any],
        admitted: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        validated_ids = {id(candidate) for candidate in validated or []}
        admitted_names = {
            str(dict((item or {}).get("record") or {}).get("name") or "")
            for item in admitted or []
        }
        rows: list[dict[str, Any]] = []
        for candidate in evolved or []:
            trace = dict(getattr(candidate, "generation_trace", None) or {})
            result = getattr(candidate, "validation_result", None)
            if not trace.get("strict_validation_attempted") and not result:
                continue
            rows.append(
                self._strict_candidate_result_payload(
                    candidate,
                    strict_gate_passed=id(candidate) in validated_ids,
                    admitted=str(getattr(candidate, "name", "") or "") in admitted_names,
                )
            )
        return rows

    def _quick_candidate_results(self, evolved: list[Any], *, limit: int = 12) -> list[dict[str, Any]]:
        """Compact quick-stage diagnostics for generated candidates."""

        rows: list[dict[str, Any]] = []
        for candidate in evolved or []:
            quick = dict(getattr(candidate, "quick_evidence", None) or {})
            if not quick:
                continue
            rows.append(
                {
                    "name": str(getattr(candidate, "name", "") or ""),
                    "generation_engine": str(getattr(candidate, "generation_engine", "") or ""),
                    "blueprint_id": str(getattr(candidate, "blueprint_id", "") or ""),
                    "family": str(
                        getattr(candidate, "factor_family", "")
                        or getattr(candidate, "family", "")
                        or ""
                    ),
                    "expression_dsl": str(getattr(candidate, "expression_dsl", "") or ""),
                    "passed": bool(quick.get("passed")),
                    "fail_reasons": list(quick.get("fail_reasons") or []),
                    "quality_score": quick.get("quality_score"),
                    "evidence_summary": dict(quick.get("evidence_summary") or {}),
                }
            )

        def _score(row: dict[str, Any]) -> tuple[int, float]:
            try:
                score = float(row.get("quality_score") or 0.0)
            except Exception:
                score = 0.0
            return (1 if row.get("passed") else 0, score)

        return sorted(rows, key=_score, reverse=True)[: max(1, int(limit))]

    def _strict_candidate_result_payload(
        self,
        candidate: Any,
        *,
        strict_gate_passed: bool,
        admitted: bool,
    ) -> dict[str, Any]:
        result = getattr(candidate, "validation_result", None)
        result = dict(result or {})
        evidence = evaluate_validation_evidence(result) if result else {
            "passed": False,
            "reasons": ["missing_validation_result"],
            "summary": {},
        }
        rating = dict(result.get("rating") or {})
        governance = dict(rating.get("governance") or {})
        grade = str(rating.get("grade") or "")
        blockers: list[str] = []
        if not result.get("success"):
            blockers.append("strict_validation_failed")
        if not evidence.get("passed"):
            blockers.extend(str(item) for item in list(evidence.get("reasons") or []))
        if grade not in {"A", "B"}:
            blockers.append(f"rating_grade_not_admissible:{grade or 'missing'}")
        if governance.get("admission_blocked"):
            blockers.extend(
                str(item)
                for item in list(governance.get("admission_block_reasons") or [])
            )
        blockers = list(dict.fromkeys(item for item in blockers if item))
        return {
            "name": str(getattr(candidate, "name", "") or ""),
            "generation_engine": str(getattr(candidate, "generation_engine", "") or ""),
            "blueprint_id": str(getattr(candidate, "blueprint_id", "") or ""),
            "family": str(
                getattr(candidate, "factor_family", "")
                or getattr(candidate, "family", "")
                or ""
            ),
            "expression_dsl": str(getattr(candidate, "expression_dsl", "") or ""),
            "fitness": float(getattr(candidate, "fitness", 0.0) or 0.0),
            "quick_evidence": dict(getattr(candidate, "quick_evidence", None) or {}),
            "admission_decision": {
                "strict_gate_passed": bool(strict_gate_passed),
                "admitted": bool(admitted),
                "evidence_passed": bool(evidence.get("passed")),
                "evidence_reasons": list(evidence.get("reasons") or []),
                "evidence_summary": dict(evidence.get("summary") or {}),
                "rating_grade": grade,
                "rating_recommendation": rating.get("recommendation"),
                "governance_admission_blocked": bool(governance.get("admission_blocked")),
                "governance_block_reasons": list(
                    governance.get("admission_block_reasons") or []
                ),
                "blockers": blockers,
            },
            "generation_trace": dict(getattr(candidate, "generation_trace", None) or {}),
            "validation_result": result,
        }

    async def _persist_admitted_factors(self, db, admitted: list[dict[str, Any]]) -> None:
        from .pool.storage import save_factor_to_pool

        for item in admitted or []:
            record = item.get("record") if isinstance(item, dict) else None
            if isinstance(record, dict):
                await save_factor_to_pool(db, record)
            for retired in list(item.get("retired_records") or []) if isinstance(item, dict) else []:
                if isinstance(retired, dict) and retired.get("factor_id"):
                    await save_factor_to_pool(db, retired)

    async def _persist_mining_run(self, db, report: dict[str, Any]) -> None:
        from .pool.storage import save_mining_run

        await save_mining_run(db, report)

    async def _persist_decay_report(self, db, decay_report: dict[str, Any]) -> None:
        from .pool.storage import save_decay_measurement

        candidates = []
        if isinstance(decay_report, dict):
            for key in ("measurements", "decay_measurements", "items"):
                value = decay_report.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
        for item in candidates:
            if isinstance(item, dict) and item.get("factor_id"):
                item.setdefault("measured_at", datetime.now(timezone.utc).isoformat())
                await save_decay_measurement(db, item)

    async def _persist_decay_updates(self, db, decay_report: dict[str, Any]) -> None:
        from .pool.storage import save_factor_to_pool

        if not isinstance(decay_report, dict):
            return
        for record in list(decay_report.get("updated_records") or []):
            if isinstance(record, dict) and record.get("factor_id"):
                await save_factor_to_pool(db, record)

    async def _promote_quarantine_factors(self, db) -> dict[str, Any]:
        from .pool.storage import load_factor_pool_from_db, save_factor_to_pool

        rows = await load_factor_pool_from_db(
            db,
            statuses=("quarantine",),
            limit=200,
        )
        promoted: list[str] = []
        rejected: dict[str, int] = {}
        for record in rows:
            evidence_result = await self._build_quarantine_evidence_result(db, record)
            evidence = evaluate_validation_evidence(evidence_result)
            if not evidence.get("passed"):
                for reason in list(evidence.get("reasons") or []):
                    rejected[reason] = rejected.get(reason, 0) + 1
                validation_summary = dict(record.get("validation_summary") or {})
                validation_summary["quality_status"] = "quarantine"
                validation_summary["evidence_summary"] = dict(evidence.get("summary") or {})
                validation_summary["promotion_block_reasons"] = list(evidence.get("reasons") or [])
                record["validation_summary"] = validation_summary
                await save_factor_to_pool(db, record)
                continue

            validation_summary = dict(record.get("validation_summary") or {})
            validation_summary["quality_status"] = "promoted"
            validation_summary["evidence_summary"] = dict(evidence.get("summary") or {})
            validation_summary["promotion_block_reasons"] = []
            record["validation_summary"] = validation_summary
            record["status"] = "active"
            record["current_ic"] = (evidence.get("summary") or {}).get("rank_ic_mean")
            record["fitness"] = compute_quality_score(evidence_result)
            record["last_evaluated_at"] = datetime.now(timezone.utc).isoformat()
            await save_factor_to_pool(db, record)
            hydrate = getattr(self._active_pool, "hydrate", None)
            if callable(hydrate):
                hydrate([record])
            promoted.append(str(record.get("factor_id") or ""))

        return {
            "checked": len(rows),
            "promoted": promoted,
            "promoted_count": len(promoted),
            "rejected_reasons": rejected,
        }

    async def _reappraise_quarantine_factors(self, db, *, limit: int = 200) -> dict[str, Any]:
        from .reappraise import reappraise_quarantine_factors

        return await reappraise_quarantine_factors(db, limit=limit)

    async def _build_quarantine_evidence_result(self, db, record: dict[str, Any]) -> dict[str, Any]:
        name = str(record.get("name") or "").strip()
        if not name:
            return {}
        rows = []
        try:
            if hasattr(db, "get_factor_ic_history"):
                rows = await db.get_factor_ic_history(name, "10", 60)
            elif hasattr(db, "acquire"):
                async with db.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM factor_ic_history
                        WHERE factor_name = $1
                        ORDER BY ic_date DESC
                        LIMIT 60
                        """,
                        name,
                    )
            else:
                raw_conn = getattr(db, "conn", None) or getattr(db, "connection", None)
                if raw_conn is not None:
                    cursor = raw_conn.execute(
                        """
                        SELECT * FROM factor_ic_history
                        WHERE factor_name = ?
                        ORDER BY ic_date DESC
                        LIMIT 60
                        """,
                        (name,),
                    )
                    columns = [desc[0] for desc in cursor.description]
                    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception:
            rows = []
        rows = [dict(row) for row in rows or []]
        values = [float(row.get("rank_ic") or 0.0) for row in rows]
        sample_sizes = [int(row.get("sample_size") or 0) for row in rows]
        if not values:
            return {
                "metrics": {"sample_dates": 0},
                "coverage": {"avg_cross_section_n": 0.0},
                "persisted_outputs": {"enabled": True, "ic_history_rows": 0},
                "lookahead_audit": {"risk_level": "unknown"},
            }
        import numpy as np

        std = float(np.std(values)) if len(values) > 1 else 0.0
        mean = float(np.mean(values))
        return {
            "metrics": {
                "sample_dates": len(values),
                "rank_ic_mean": mean,
                "rank_ic_ir": mean / std if std > 1e-12 else 0.0,
                "positive_ratio": float(np.mean(np.array(values) > 0.0)),
            },
            "coverage": {
                "avg_cross_section_n": float(np.mean(sample_sizes)) if sample_sizes else 0.0,
            },
            "persisted_outputs": {"enabled": True, "ic_history_rows": len(values)},
            "lookahead_audit": {"risk_level": "low"},
        }

    async def _record_feedback(self, run_id, raw, evolved, validated, admitted):
        await self._meta_learner.record_cycle(
            run_id=run_id,
            raw_count=len(raw),
            evolved_count=len(evolved),
            validated_count=len(validated),
            admitted_count=len(admitted),
            candidates=raw + evolved,
        )
