"""DB-only, cycle-scoped measurements used by the active-pool admission gate."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..._factor_validation_pipeline_support import _load_validation_frame
from ...factor_candidate_compiler import compile_factor_candidate, evaluate_compiled_factor
from .orthogonalizer import FactorOrthogonalizer


class FactorPoolCorrelationEvaluator:
    """Evaluate expressions on one immutable per-cycle market-data snapshot.

    Observation keys are (trade_date, stock_code), never row offsets. Frames
    and compiled expression values are reused within a cycle; a new context
    creates a new evaluator so updated data is visible in subsequent cycles.
    """

    def __init__(self, db: Any, codes: list[str], *, lookback: int = 220, horizon: int = 10):
        self.db = db
        self.codes = list(dict.fromkeys(str(code) for code in codes))
        self.lookback = lookback
        self.horizon = horizon
        self._frames: dict[str, pd.DataFrame] | None = None
        self._values: dict[tuple[str, tuple[str, ...]], pd.Series] = {}
        self._returns = pd.Series(dtype=float)
        self._failures: list[dict[str, str]] = []

    async def _load_frames(self) -> None:
        if self._frames is not None:
            return
        self._frames = {}
        returns = []
        for code in self.codes:
            frame, _, reason = await _load_validation_frame(self.db, code, self.lookback)
            if reason or frame.empty:
                self._failures.append({"code": code, "reason": reason or "empty_frame"})
                continue
            # Canonical trading dates, with bad dates excluded explicitly.
            parsed = pd.to_datetime(frame["date"], errors="coerce")
            if parsed.isna().any():
                self._failures.append({"code": code, "reason": "invalid_trade_dates"})
                continue
            frame = frame.copy()
            frame["date"] = parsed.dt.strftime("%Y-%m-%d")
            frame = frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
            self._frames[code] = frame
            index = pd.MultiIndex.from_arrays([frame["date"], [code] * len(frame)], names=["date", "code"])
            close = frame["close"].astype(float)
            future = close.shift(-self.horizon) / close.replace(0.0, np.nan) - 1.0
            returns.append(pd.Series(future.to_numpy(), index=index))
        if returns:
            self._returns = pd.concat(returns).sort_index()

    def _factor_values(self, payload: dict[str, Any]) -> pd.Series:
        inputs = payload.get("inputs") or ["close"]
        if isinstance(inputs, str):
            import json
            inputs = json.loads(inputs)
        payload = {**payload, "inputs": list(inputs), "hypothesis": payload.get("hypothesis") or "pool correlation measurement"}
        key = (str(payload.get("expression_dsl") or ""), tuple(sorted(str(x) for x in inputs)))
        if key in self._values:
            return self._values[key]
        compiled = compile_factor_candidate(payload)
        if not compiled.get("valid"):
            raise ValueError("invalid_factor_expression")
        values = []
        for code, frame in (self._frames or {}).items():
            evaluated = evaluate_compiled_factor(compiled, frame)
            index = pd.MultiIndex.from_arrays([frame["date"], [code] * len(frame)], names=["date", "code"])
            values.append(pd.Series(np.asarray(evaluated, dtype=float), index=index))
        result = pd.concat(values).sort_index() if values else pd.Series(dtype=float)
        self._values[key] = result.replace([np.inf, -np.inf], np.nan)
        return self._values[key]

    async def evaluate(self, candidate: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
        await self._load_frames()
        common = {
            "basis": "factor_values_pooled_spearman",
            "observation_key": ["trade_date", "stock_code"],
            "requested_codes": len(self.codes),
            "loaded_codes": len(self._frames or {}),
            "failed_codes": list(self._failures),
        }
        if not self._frames or self._failures:
            return {**common, "measurable": False, "max_correlation": None, "reason": "incomplete_correlation_universe"}
        try:
            payload = candidate if isinstance(candidate, dict) else candidate.to_validation_dict()
            new_values = self._factor_values(payload)
            pool_values = {str(record["factor_id"]): self._factor_values(record) for record in records}
            result = await FactorOrthogonalizer().check_orthogonality(
                new_values, pool_values, forward_returns=self._returns
            )
        except Exception as exc:
            return {**common, "measurable": False, "max_correlation": None, "reason": "factor_value_evaluation_failed", "error": str(exc)}
        matched = next((r for r in records if r["factor_id"] == result.most_correlated_factor), {})
        quality = (matched.get("validation_summary") or {}).get("quality_score", matched.get("fitness", 0.0))
        sample_counts = {
            key: int(pd.concat([new_values, value], axis=1).dropna().shape[0])
            for key, value in pool_values.items()
        }
        return {
            **common,
            **result.to_dict(),
            "factor_id": result.most_correlated_factor,
            "existing_quality_score": quality,
            "common_observations": sample_counts,
        }


class QuarantinePromotionGate:
    """Recheck independence against active peers, sharing one batch snapshot."""

    def __init__(self, db: Any, *, correlation_evaluator=None, threshold: float = 0.7):
        self.db = db
        self.evaluator = correlation_evaluator
        self.threshold = threshold

    async def check(self, record: dict[str, Any], active_records: list[dict[str, Any]]) -> dict[str, Any]:
        peers = [row for row in active_records if row.get("factor_id") != record.get("factor_id")]
        if not peers:
            return {"measurable": True, "max_correlation": 0.0, "basis": "empty_pool", "passed": True}
        try:
            if self.evaluator is None:
                from ..engines.context import MiningContext

                context = await MiningContext.build(db=self.db)
                self.evaluator = FactorPoolCorrelationEvaluator(self.db, context.validation_codes).evaluate
            report = dict(await self.evaluator(record, peers))
        except Exception as exc:
            report = {"measurable": False, "max_correlation": None, "error": str(exc)}
        correlation = report.get("max_correlation")
        if not report.get("measurable") or correlation is None or not np.isfinite(correlation):
            return {**report, "passed": False, "promotion_block_reason": "orthogonality_unmeasurable"}
        passed = float(correlation) <= self.threshold
        return {
            **report,
            "passed": passed,
            "promotion_block_reason": None if passed else "orthogonality_redundant",
        }
