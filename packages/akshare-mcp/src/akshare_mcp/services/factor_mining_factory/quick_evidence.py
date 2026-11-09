"""Fast validation feedback for candidate generation and pre-screening."""

from __future__ import annotations

import logging
from typing import Any

from .._factor_search_budget import search_checkpoint

from .quality import (
    QUALITY_PROFILE_ACTIVE,
    evaluate_validation_evidence,
    safe_float,
    safe_int,
    _resolve_thresholds,
)

logger = logging.getLogger(__name__)


def _resolve_quick_thresholds() -> dict[str, float]:
    """Quick stage follows AKSHARE_QUALITY_PROFILE but is slightly softer than full QC.

    - sample_dates: 2/3 of full threshold (bounded quick window)
    - ic_history_rows: not required at quick stage
    - cross-section n / IC stats: inherit profile
    """
    full = _resolve_thresholds()
    sample = max(10.0, float(full["min_sample_dates"]) * (2.0 / 3.0))
    return {
        "min_sample_dates": sample,
        "min_avg_cross_section_n": float(full["min_avg_cross_section_n"]),
        "min_ic_history_rows": 0.0,
        "min_abs_rank_ic_mean": float(full["min_abs_rank_ic_mean"]),
        "min_rank_ic_ir": float(full["min_rank_ic_ir"]),
        "min_positive_ratio": float(full["min_positive_ratio"]),
    }


QUICK_EVIDENCE_THRESHOLDS: dict[str, float] = _resolve_quick_thresholds()


class QuickEvidenceEvaluator:
    """Run a bounded quick IC screen on a healthy validation subset."""

    def __init__(
        self,
        db: Any,
        *,
        codes: list[str],
        horizon_days: int = 10,
        max_codes: int = 120,
        max_evaluations: int = 32,
    ) -> None:
        self._db = db
        self._codes = [str(code).strip() for code in list(codes or []) if str(code).strip()]
        self._horizon_days = int(horizon_days or 10)
        self._max_codes = max(1, int(max_codes or 120))
        self._max_evaluations = max(1, int(max_evaluations or 32))
        self._cache: dict[str, dict[str, Any]] = {}
        self._evaluation_count = 0

    @property
    def evaluation_count(self) -> int:
        return self._evaluation_count

    async def evaluate(self, candidate: Any) -> dict[str, Any]:
        """Return structured quick evidence and attach it to the candidate."""

        search_checkpoint()
        expression = str(getattr(candidate, "expression_dsl", "") or "").strip()
        cache_key = expression or str(getattr(candidate, "name", "") or "")
        if cache_key in self._cache:
            evidence = dict(self._cache[cache_key])
            self._attach(candidate, evidence)
            return evidence

        if len(self._codes) < int(QUICK_EVIDENCE_THRESHOLDS["min_avg_cross_section_n"]):
            evidence = self._failure(
                "quick_universe_too_small",
                eligible_code_count=len(self._codes),
            )
            self._cache[cache_key] = evidence
            self._attach(candidate, evidence)
            return evidence

        if self._evaluation_count >= self._max_evaluations:
            evidence = self._failure(
                "quick_evaluation_budget_exhausted",
                eligible_code_count=len(self._codes),
            )
            self._cache[cache_key] = evidence
            self._attach(candidate, evidence)
            return evidence

        self._evaluation_count += 1
        try:
            from ..factor_validation_pipeline import validate_factor_candidate_pipeline

            result = await validate_factor_candidate_pipeline(
                self._db,
                candidate.to_validation_dict(),
                codes=self._codes[: self._max_codes],
                stage="quick",
                lookback_bars=180,
                horizon_days=self._horizon_days,
                max_dates=60,
                min_cross_section=80,
                persist_outputs=False,
                factor_key=getattr(candidate, "name", None),
                persist_ic_history=False,
            )
            search_checkpoint()
            evidence = self._from_validation_result(result)
        except Exception as exc:
            logger.debug(
                "QuickEvidenceEvaluator failed for %s: %s",
                getattr(candidate, "name", ""),
                exc,
            )
            evidence = self._failure(
                "quick_validation_failed",
                error=f"{type(exc).__name__}: {exc}",
                eligible_code_count=len(self._codes),
            )

        self._cache[cache_key] = dict(evidence)
        self._attach(candidate, evidence)
        return evidence

    async def ic_value(self, candidate: Any) -> float:
        """Adapter for legacy IC feedback interfaces."""

        evidence = await self.evaluate(candidate)
        if not evidence.get("passed"):
            return 0.0
        return safe_float(evidence.get("rank_ic_mean"))

    async def __call__(self, candidate: Any) -> dict[str, Any]:
        return await self.evaluate(candidate)

    def _from_validation_result(self, result: dict[str, Any]) -> dict[str, Any]:
        coverage = dict(result.get("coverage") or {})
        metrics = dict(result.get("metrics") or {})
        cross_section = dict(result.get("cross_section") or {})
        summary = dict(cross_section.get("summary") or {})
        evidence = evaluate_validation_evidence(
            result,
            require_persisted_history=False,
            thresholds=QUICK_EVIDENCE_THRESHOLDS,
        )
        evidence_summary = dict(evidence.get("summary") or {})
        quality_score = safe_float(result.get("quality_score"))
        if quality_score <= 0.0:
            quality_score = self._quick_quality_score(evidence_summary, evidence.get("passed"))
        return {
            "available": bool(result.get("success")),
            "stage": "quick",
            "passed": bool(evidence.get("passed")),
            "fail_reasons": list(evidence.get("reasons") or []),
            "rank_ic_mean": None if metrics.get("available") is False else safe_float(
                evidence_summary.get("rank_ic_mean"),
                safe_float(metrics.get("rank_ic_mean"), safe_float(summary.get("rank_ic_mean"))),
            ),
            "rank_ic_ir": None if metrics.get("available") is False else safe_float(
                evidence_summary.get("rank_ic_ir"),
                safe_float(metrics.get("rank_ic_ir"), safe_float(summary.get("rank_ic_ir"))),
            ),
            "positive_ratio": None if metrics.get("available") is False else safe_float(
                evidence_summary.get("positive_ratio"),
                safe_float(metrics.get("positive_ratio"), safe_float(summary.get("positive_ratio"))),
            ),
            "sample_dates": safe_int(evidence_summary.get("sample_dates")),
            "avg_cross_section_n": safe_float(
                evidence_summary.get("avg_cross_section_n"),
                safe_float(coverage.get("avg_cross_section_n")),
            ),
            "eligible_code_count": safe_int(coverage.get("eligible_code_count")),
            "quality_score": round(quality_score, 4),
            "evidence_summary": evidence_summary,
            "diagnostic_counts": dict(coverage.get("diagnostic_counts") or {}),
        }

    @staticmethod
    def _quick_quality_score(summary: dict[str, Any], passed: Any) -> float:
        rank_ic = abs(safe_float(summary.get("rank_ic_mean")))
        rank_ir = max(0.0, safe_float(summary.get("rank_ic_ir")))
        pos = safe_float(summary.get("positive_ratio"))
        sample_dates = safe_float(summary.get("sample_dates"))
        avg_n = safe_float(summary.get("avg_cross_section_n"))
        score = (
            35.0 * min(rank_ic / 0.04, 1.0)
            + 25.0 * min(rank_ir / 0.50, 1.0)
            + 20.0 * min(max((pos - 0.5) / 0.10, 0.0), 1.0)
            + 10.0 * min(sample_dates / 60.0, 1.0)
            + 10.0 * min(avg_n / 120.0, 1.0)
        )
        if not passed:
            score *= 0.35
        return max(0.0, min(100.0, score))

    @staticmethod
    def _failure(reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "available": False,
            "stage": "quick",
            "passed": False,
            "fail_reasons": [reason],
            "rank_ic_mean": None,
            "rank_ic_ir": None,
            "positive_ratio": None,
            "sample_dates": 0,
            "avg_cross_section_n": 0.0,
            "eligible_code_count": int(extra.pop("eligible_code_count", 0) or 0),
            "quality_score": 0.0,
            "evidence_summary": {},
            "diagnostic_counts": {},
            **extra,
        }

    @staticmethod
    def _attach(candidate: Any, evidence: dict[str, Any]) -> None:
        try:
            candidate.quick_evidence = dict(evidence)
            trace = dict(getattr(candidate, "generation_trace", None) or {})
            trace["quick_evidence"] = dict(evidence)
            candidate.generation_trace = trace
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/quick_evidence.py:225: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
