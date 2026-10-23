"""Selective execution gate (WP-C).

The measured failure this closes: paper orders were placed from the signal path
without reading any probability, so the tradable set was the whole signal set
and its hit rate was therefore the market's, not a selected subset's.

Rules:

* a prediction is **matched** only to the session immediately after its
  ``prediction_as_of``. The legacy fallback window
  (``prediction_as_of <= signal_date <= target``) matched 56 of 547 orders to
  predictions up to 180 days old and is deliberately not reproduced here — a
  stale prediction must match nothing.
* ``ordered`` requires an **active** calibration model and
  ``calibrated_confidence >= threshold``. Without one, every prediction is
  ``abstained`` with reason ``calibration_unavailable``. That is the correct
  fail-closed result, not an error.
* every decision, ordered or abstained, is written to
  ``prediction_executions`` so the scorecard can report ``abstain_rate``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

SELECTIVE_GATE_VERSION = "selective_gate.v1"
DEFAULT_THRESHOLD = 0.85
ACTION_ORDERED = "ordered"
ACTION_ABSTAINED = "abstained"

REASON_BELOW_THRESHOLD = "below_threshold"
REASON_CALIBRATION_UNAVAILABLE = "calibration_unavailable"
REASON_STALE_PREDICTION = "stale_prediction"
REASON_NEUTRAL = "neutral_direction"
REASON_TARGET_MISMATCH = "target_date_mismatch"


def _string(value: Any) -> str:
    return str(value or "").strip()


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    token = _string(value)
    if not token:
        return None
    for length, fmt in ((10, "%Y-%m-%d"), (8, "%Y%m%d")):
        try:
            return datetime.strptime(token[:length], fmt).date()
        except ValueError:
            continue
    return None


def _execution_id(prediction_id: str, signal_date: str) -> str:
    return "pe_" + hashlib.sha256(f"{prediction_id}|{signal_date}".encode()).hexdigest()[:24]


@dataclass(slots=True)
class SelectiveDecision:
    prediction_id: str
    action: str
    reason: Optional[str]
    calibrated_confidence: Optional[float]
    calibration_model_hash: Optional[str]
    execution_result: Optional[dict] = None

    def as_row(self, prediction: dict[str, Any], *, signal_date: str) -> dict[str, Any]:
        execution = self.execution_result or {}
        paper_order_id = None
        order_id = execution.get("order_id")
        if order_id and str(order_id).isdigit():
            paper_order_id = int(order_id)
        return {
            "id": _execution_id(_string(prediction.get("prediction_id")), signal_date),
            "prediction_id": _string(prediction.get("prediction_id")),
            "strategy_id": _string(prediction.get("strategy_id")),
            "stock_code": _string(prediction.get("stock_code")),
            "prediction_as_of": _string(prediction.get("prediction_as_of"))[:10] or None,
            "target_trading_date": _string(prediction.get("target_trading_date"))[:10] or None,
            # The direction is the prediction's own signal, not re-derived at
            # order time; the gate abstains on ``neutral`` before this is used.
            "direction": _string(prediction.get("direction")).lower() or None,
            "action": self.action,
            "reason": self.reason,
            "calibrated_confidence": self.calibrated_confidence,
            "calibration_model_hash": self.calibration_model_hash,
            "raw_confidence": _as_float(prediction.get("confidence")),
            "signal_id": execution.get("signal_id"),
            "paper_order_id": paper_order_id,
            "execution_result": execution,
        }


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _rows(db: Any, sql: str, *args: Any) -> list[dict[str, Any]]:
    fetch = getattr(db, "fetch", None)
    if not callable(fetch):
        return []
    raw = await fetch(sql, *args)
    if isinstance(raw, dict):
        for key in ("data", "rows", "items", "results"):
            if isinstance(raw.get(key), list):
                return [dict(item) for item in raw[key] if isinstance(item, dict)]
        return []
    return [dict(item) for item in (raw or []) if isinstance(item, dict)]


async def load_active_calibration(db: Any, *, strategy_family: str = "default") -> Optional[dict[str, Any]]:
    rows = await _rows(
        db,
        """
        SELECT model_hash, params_json, feature_version, fit_end
        FROM calibration_models
        WHERE status = 'active' AND (strategy_family = $1 OR strategy_family = 'default')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        strategy_family,
    )
    return rows[0] if rows else None


def apply_model(model: dict[str, Any], raw_confidence: Optional[float]) -> Optional[float]:
    if raw_confidence is None:
        return None
    try:
        params = json.loads(model.get("params_json") or "{}")
    except ValueError:
        return None
    a = float(params.get("a", 1.0))
    b = float(params.get("b", 0.0))
    z = a * raw_confidence + b
    return 1.0 / (1.0 + 2.718281828459045 ** -max(-30.0, min(30.0, z)))


def decide(
    prediction: dict[str, Any],
    *,
    signal_date: date,
    model: Optional[dict[str, Any]],
    threshold: float = DEFAULT_THRESHOLD,
) -> SelectiveDecision:
    prediction_id = _string(prediction.get("prediction_id"))
    as_of = _day(prediction.get("prediction_as_of"))
    if _string(prediction.get("direction")).lower() == "neutral":
        return SelectiveDecision(prediction_id, ACTION_ABSTAINED, REASON_NEUTRAL, None, None)
    if as_of is None:
        return SelectiveDecision(prediction_id, ACTION_ABSTAINED, REASON_STALE_PREDICTION, None, None)
    # The session the gate is deciding is the one the prediction targets. A
    # recorded target that disagrees with that session marks a stale or
    # mis-generated row — matching it anyway is what the legacy window did.
    target = _day(prediction.get("target_trading_date"))
    if target is not None and target != signal_date:
        return SelectiveDecision(prediction_id, ACTION_ABSTAINED, REASON_TARGET_MISMATCH, None, None)
    if model is None:
        return SelectiveDecision(prediction_id, ACTION_ABSTAINED, REASON_CALIBRATION_UNAVAILABLE, None, None)
    calibrated = apply_model(model, _as_float(prediction.get("confidence")))
    model_hash = _string(model.get("model_hash")) or None
    if calibrated is None:
        return SelectiveDecision(prediction_id, ACTION_ABSTAINED, REASON_CALIBRATION_UNAVAILABLE, None, model_hash)
    if calibrated < threshold:
        return SelectiveDecision(prediction_id, ACTION_ABSTAINED, REASON_BELOW_THRESHOLD, calibrated, model_hash)
    return SelectiveDecision(prediction_id, ACTION_ORDERED, None, calibrated, model_hash)


async def find_predictions_for_session(db: Any, *, signal_date: date, limit: int = 2000) -> list[dict[str, Any]]:
    """Predictions whose as-of is the trading session immediately before ``signal_date``.

    "Immediately before" is derived from the observed as-of values themselves
    rather than from the wall clock, so a weekend or a suspended session does
    not turn a valid prediction into a mismatch.
    """
    rows = await _rows(
        db,
        """
        SELECT prediction_id, strategy_id, stock_code, prediction_as_of,
               target_trading_date, direction, confidence, horizon
        FROM strategy_trade_predictions
        WHERE date(prediction_as_of) < date($1)
          AND date(prediction_as_of) >= date($2)
        LIMIT $3
        """,
        signal_date.isoformat(),
        (signal_date - timedelta(days=7)).isoformat(),
        int(limit),
    )
    by_as_of: dict[date, list[dict[str, Any]]] = {}
    for row in rows:
        as_of = _day(row.get("prediction_as_of"))
        if as_of is None:
            continue
        by_as_of.setdefault(as_of, []).append(row)
    if not by_as_of:
        return []
    latest = max(by_as_of)
    matched: list[dict[str, Any]] = []
    for row in by_as_of[latest]:
        target = _day(row.get("target_trading_date"))
        # A recorded target that disagrees with the session is a stale or
        # mis-generated row; matching it anyway is what the old window did.
        if target is not None and target != signal_date:
            continue
        matched.append(row)
    return matched


@dataclass(slots=True)
class SelectiveGate:
    threshold: float = DEFAULT_THRESHOLD

    async def evaluate_session(
        self,
        db: Any,
        *,
        signal_date: date,
        persist: bool = True,
        strategies: Optional[dict[str, dict[str, Any]]] = None,
        executor: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Decide every prediction written for the session before ``signal_date``.

        ``executor`` is the execution half: an object exposing
        ``place_order_for_execution(strategy=, code=, signal_date=,
        prediction_id=, direction=)``. It is called only for ``ordered``
        decisions, and its refusal is recorded on the execution row rather than
        raising — a gate that says "order" but cannot place one must not report
        that an order happened.
        """
        predictions = await find_predictions_for_session(db, signal_date=signal_date)
        model = await load_active_calibration(db)
        strategies = strategies or {}
        decisions: list[dict[str, Any]] = []
        for prediction in predictions:
            decision = decide(prediction, signal_date=signal_date, model=model, threshold=self.threshold)
            if decision.action == ACTION_ORDERED and executor is not None:
                decision.execution_result = await self._execute(
                    executor,
                    prediction,
                    strategies=strategies,
                    signal_date=signal_date,
                )
            row = decision.as_row(prediction, signal_date=signal_date.isoformat())
            if persist:
                await self._persist(db, row)
            decisions.append(row)
        ordered = [row for row in decisions if row["action"] == ACTION_ORDERED]
        placed = [row for row in ordered if row.get("paper_order_id") is not None]
        return {
            "gate_version": SELECTIVE_GATE_VERSION,
            "signal_date": signal_date.isoformat(),
            "matched_predictions": len(predictions),
            "ordered": len(ordered),
            "orders_placed": len(placed),
            "abstained": len(decisions) - len(ordered),
            "calibration_model_hash": _string((model or {}).get("model_hash")) or None,
            "reason_counts": _count(decisions),
            "decisions": decisions,
            "orders": ordered,
        }

    async def _execute(
        self,
        executor: Any,
        prediction: dict[str, Any],
        *,
        strategies: dict[str, dict[str, Any]],
        signal_date: date,
    ) -> dict[str, Any]:
        strategy_id = _string(prediction.get("strategy_id"))
        strategy = strategies.get(strategy_id)
        if strategy is None:
            return {"placed": False, "reason": "strategy_not_loaded"}
        direction = _string(prediction.get("direction")).lower()
        place = getattr(executor, "place_order_for_execution", None)
        if not callable(place):
            return {"placed": False, "reason": "executor_unavailable"}
        try:
            result = await place(
                strategy=strategy,
                code=_string(prediction.get("stock_code")),
                signal_date=signal_date.isoformat(),
                prediction_id=_string(prediction.get("prediction_id")),
                direction=direction,
            )
        except Exception as exc:
            logger.warning("selective gate execution failed for %s: %s", prediction.get("prediction_id"), exc)
            return {"placed": False, "reason": f"execution_failed: {type(exc).__name__}"}
        return dict(result or {})

    async def _persist(self, db: Any, row: dict[str, Any]) -> None:
        execute = getattr(db, "execute", None)
        if not callable(execute):
            return
        try:
            await execute(
                """
                INSERT INTO prediction_executions (
                    id, prediction_id, strategy_id, stock_code, prediction_as_of,
                    target_trading_date, action, reason, calibrated_confidence,
                    calibration_model_hash, raw_confidence, signal_id, paper_order_id,
                    execution_result
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (id) DO UPDATE SET
                    action = EXCLUDED.action,
                    reason = EXCLUDED.reason,
                    calibrated_confidence = EXCLUDED.calibrated_confidence,
                    calibration_model_hash = EXCLUDED.calibration_model_hash,
                    signal_id = EXCLUDED.signal_id,
                    paper_order_id = EXCLUDED.paper_order_id,
                    execution_result = EXCLUDED.execution_result
                """,
                row["id"],
                row["prediction_id"],
                row["strategy_id"],
                row["stock_code"],
                row["prediction_as_of"],
                row["target_trading_date"],
                row["action"],
                row["reason"],
                row["calibrated_confidence"],
                row["calibration_model_hash"],
                row["raw_confidence"],
                row["signal_id"],
                row["paper_order_id"],
                json.dumps(row["execution_result"] or {}, sort_keys=True, default=str),
            )
        except Exception as exc:
            logger.warning("prediction_executions write failed: %s", exc)


def _count(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in decisions:
        key = _string(row.get("reason")) or _string(row.get("action")) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "ACTION_ABSTAINED",
    "ACTION_ORDERED",
    "DEFAULT_THRESHOLD",
    "REASON_BELOW_THRESHOLD",
    "REASON_CALIBRATION_UNAVAILABLE",
    "REASON_NEUTRAL",
    "REASON_STALE_PREDICTION",
    "REASON_TARGET_MISMATCH",
    "SELECTIVE_GATE_VERSION",
    "SelectiveDecision",
    "SelectiveGate",
    "apply_model",
    "decide",
    "find_predictions_for_session",
    "load_active_calibration",
]
