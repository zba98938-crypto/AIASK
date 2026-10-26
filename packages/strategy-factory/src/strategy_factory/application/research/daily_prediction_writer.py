"""Daily per-symbol trade predictions (WP-0.5).

The measured baseline in ``docs/PREDICTION_HITRATE_85_PLAN.md`` (v3 §2.2) is
that all 8,932 existing predictions were created **once per strategy at
submission time**: one strategy, one prediction, and 0 of 547 paper orders
matched the target date. A hit rate cannot be measured on that shape, because
the same frozen prediction is never refreshed and paper orders come from the
signal path instead.

This writer closes the loop: on every closed session it writes one prediction
per ``(strategy, symbol)`` for the **next** session, derived from the signal the
strategy actually generated on the closed bar. Two rules matter:

* ``prediction_as_of`` is the last **closed** bar date from the data, never the
  wall clock. A Sunday run must not create a Sunday ``as_of``.
* the prediction is only written when the strategy produced a direction. A
  neutral hold is recorded as ``neutral`` and is excluded from the direction
  denominator downstream, but it is not silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("Asia/Shanghai")
PREDICTION_CONTRACT_VERSION = "strategy_factory.trade_prediction_contract.v1"
PREDICTION_CONTRACT_SOURCE = "daily_signal"
CLOSE_TIME = time(15, 0)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


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


def _closed_bars(klines: list[dict[str, Any]], *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    now = now or datetime.now(MARKET_TZ)
    by_day: dict[date, dict[str, Any]] = {}
    for item in klines or []:
        bar = dict(item)
        day = _day(bar.get("date") or bar.get("trade_date") or bar.get("time") or bar.get("timestamp"))
        if day is None or day.weekday() >= 5:
            continue
        if day > now.date() or (day == now.date() and now.time() < CLOSE_TIME):
            continue
        bar["date"] = day.isoformat()
        by_day[day] = bar
    return [by_day[day] for day in sorted(by_day)]


def _target_symbols(strategy: dict[str, Any]) -> list[str]:
    params = strategy.get("params") if isinstance(strategy.get("params"), dict) else {}
    if isinstance(strategy.get("params"), str):
        try:
            params = json.loads(strategy["params"])
        except ValueError:
            params = {}
    values = params.get("target_symbols") or strategy.get("target_symbols") or []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(_string(value) for value in values if _string(value)))


def _direction_from_signal(signal: int) -> str:
    if signal > 0:
        return "up"
    if signal < 0:
        return "down"
    return "neutral"


def _strategy_prediction_contract(strategy: dict[str, Any]) -> dict[str, Any]:
    """Return the strategy's explicit prediction contract, if it has one.

    The daily signal itself is a direction, not a probability.  A confidence
    value is therefore accepted only when it already exists in the strategy's
    persisted prediction contract (or its confidence contract).  This keeps
    the writer from manufacturing a probability from a rule signal.
    """
    params = strategy.get("params") if isinstance(strategy.get("params"), dict) else {}
    if isinstance(strategy.get("params"), str):
        try:
            params = json.loads(strategy["params"])
        except (TypeError, ValueError):
            params = {}
    for source in (
        strategy.get("trade_prediction_contract"),
        strategy.get("prediction_contract"),
        strategy.get("confidence_contract"),
        params.get("trade_prediction_contract"),
        params.get("prediction_contract"),
        params.get("confidence_contract"),
    ):
        if isinstance(source, dict):
            return dict(source)
    return {}


def _confidence_from_strategy(strategy: dict[str, Any]) -> tuple[Optional[float], Optional[str], list[str]]:
    contract = _strategy_prediction_contract(strategy)
    value = contract.get("confidence")
    if value is None:
        value = strategy.get("confidence")
    confidence = _number(value)
    if confidence is None or not 0.0 <= confidence <= 1.0:
        return None, None, []
    source = _string(contract.get("confidence_source") or strategy.get("confidence_source")) or None
    refs = contract.get("evidence_refs") or strategy.get("evidence_refs") or []
    if isinstance(refs, str):
        refs = [refs]
    return confidence, source, [_string(ref) for ref in refs if _string(ref)]


def _prediction_id(strategy_id: str, code: str, as_of: date) -> str:
    payload = f"{strategy_id}|{code}|{as_of.isoformat()}"
    return "tp_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


@dataclass(slots=True)
class DailyPredictionWriter:
    """Writes next-session predictions from a closed-bar signal."""

    contract_version: str = PREDICTION_CONTRACT_VERSION

    async def write(
        self,
        db: Any,
        *,
        strategy: dict[str, Any],
        code: str,
        klines: list[dict[str, Any]],
        signal: int,
        horizon: str | None = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        strategy_id = _string(strategy.get("id") or strategy.get("strategy_id"))
        if not strategy_id:
            return {"status": "skipped", "reason": "missing_strategy_id"}
        if not code:
            return {"status": "skipped", "reason": "missing_code"}
        bars = _closed_bars(klines, now=now)
        if not bars:
            return {"status": "skipped", "reason": "no_closed_bars", "strategy_id": strategy_id, "code": code}
        as_of = _day(bars[-1].get("date"))
        if as_of is None:
            return {"status": "skipped", "reason": "unparseable_as_of"}
        target, calendar_source = await self._next_session(db, as_of)
        if target is None:
            return {"status": "skipped", "reason": "next_session_unavailable", "strategy_id": strategy_id, "code": code, "as_of": as_of.isoformat()}
        direction = _direction_from_signal(int(signal))
        confidence, confidence_source, confidence_refs = _confidence_from_strategy(strategy)
        contract = {
            "strategy_id": strategy_id,
            "stock_code": code,
            "prediction_as_of": as_of.isoformat(),
            "target_trading_date": target.isoformat(),
            "direction": direction,
            "confidence": confidence,
            "horizon": horizon or self._horizon(strategy),
            "contract_version": self.contract_version,
            "contract_source": PREDICTION_CONTRACT_SOURCE,
            "evidence_refs": [],
        }
        if confidence_refs:
            contract["evidence_refs"] = confidence_refs
        if confidence_source:
            contract["confidence_source"] = confidence_source
        contract_hash = hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        contract["contract_hash"] = contract_hash
        payload = {
            "prediction_id": _prediction_id(strategy_id, code, as_of),
            "contract_json": contract,
            "contract_hash": contract_hash,
            "metadata": {
                "source": "strategy_factory.daily_prediction_writer",
                "signal": int(signal),
                "confidence_available": confidence is not None,
                "confidence_source": confidence_source,
                "confidence_evidence_refs": confidence_refs,
            },
        }
        saver = getattr(db, "save_strategy_trade_prediction", None)
        if not callable(saver):
            return {"status": "degraded", "reason": "prediction_storage_unavailable"}
        await saver(payload)
        return {
            "status": "ok",
            "prediction_id": payload["prediction_id"],
            "strategy_id": strategy_id,
            "code": code,
            "as_of": as_of.isoformat(),
            "target_trading_date": target.isoformat(),
            "direction": direction,
            "confidence": confidence,
            "calendar_source": calendar_source,
        }

    async def _next_session(self, db: Any, as_of: date) -> tuple[Optional[date], str]:
        """Next trading session; the calendar wins when it actually reaches past ``as_of``."""
        from_calendar = await self._next_session_from_calendar(db, as_of)
        if from_calendar is not None:
            return from_calendar, "trading_calendar"
        for offset in range(1, 11):
            candidate = as_of + timedelta(days=offset)
            if candidate.weekday() < 5:
                return candidate, "weekday_fallback"
        return None, "unavailable"

    async def _next_session_from_calendar(self, db: Any, as_of: date) -> Optional[date]:
        fetch = getattr(db, "fetch", None)
        if not callable(fetch):
            return None
        try:
            rows = await fetch(
                "SELECT MIN(trade_date) AS next FROM trading_dates WHERE trade_date > $1",
                as_of.strftime("%Y%m%d"),
            )
        except Exception as exc:
            logger.debug("trading calendar lookup failed: %s", exc)
            return None
        for row in rows or []:
            payload = row if isinstance(row, dict) else dict(row)
            day = _day(payload.get("next"))
            if day is not None and day > as_of:
                return day
        return None

    def _horizon(self, strategy: dict[str, Any]) -> str:
        params = strategy.get("params") if isinstance(strategy.get("params"), dict) else {}
        for source in (strategy, params):
            for key in ("holding_horizon", "hold_days", "horizon"):
                value = source.get(key)
                if value in (None, ""):
                    continue
                if isinstance(value, (int, float)):
                    return f"{int(value)}d"
                token = _string(value)
                if token.isdigit():
                    return f"{token}d"
                return token
        return "5d"


__all__ = [
    "DailyPredictionWriter",
    "MARKET_TZ",
    "PREDICTION_CONTRACT_SOURCE",
    "PREDICTION_CONTRACT_VERSION",
    "_closed_bars",
]
