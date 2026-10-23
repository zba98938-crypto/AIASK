"""Net-of-cost horizon scoring for frozen trade predictions (WP-A).

This is deliberately a *new* score version rather than a rewrite of the daily
scorer. ``trade_prediction_score_daily_v1`` answers "did the target day's
open→close move match the direction"; the question the 85% target actually
asks is "was the direction right at horizon k after paying costs and entering
only at the next session's open". Both can be right or wrong for different
reasons, so they are stored side by side and never averaged together.

Rules that are frozen here (see ``docs/PREDICTION_HITRATE_85_PLAN.md`` v3):

* entry  = open of the trading session **after** ``prediction_as_of``
* exit   = close of the **k-th** session after entry (holding k bars,
          ``entry_index + k``), never ``+ k - 1``
* net    = ``exit_close / (entry_open * (1 + cost)) - 1``
* hit    = ``net > 0`` for ``up``, ``net < 0`` for ``down``; ``neutral`` is
           excluded from the direction denominator but still persisted
* gaps   = a suspended session before entry shifts entry later but does not
           consume horizon; a suspended session *inside* the holding window
           does consume it (the bar marks a real market day)
* missing= a missing open/close is ``data_unavailable`` with a reason code,
           never a zero-filled return

Retries: ``pending_market_data`` is now bounded. A prediction whose bars are
not available yet is retried up to ``MAX_RETRY_ATTEMPTS`` times and then moves
to ``data_unavailable`` with the same reason, so a stalled sync becomes
visible instead of being silently re-skipped forever.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

HORIZON_SCORE_VERSION = "trade_prediction_horizon_net_v1"
HORIZON_SCORER_VERSION = "horizon_scorer.v1"
DEFAULT_ROUND_TRIP_COST = 0.0021
DEFAULT_FLAT_BAND = 0.0020
MAX_RETRY_ATTEMPTS = 5

STATUS_OK = "ok"
STATUS_PENDING = "pending_market_data"
STATUS_UNAVAILABLE = "data_unavailable"
STATUS_NEUTRAL_EXCLUDED = "neutral_direction_excluded"

# Stable reason codes. Do not reword these without bumping HORIZON_SCORER_VERSION:
# they are the only signal that separates "not measurable" from "measured weak".
REASON_ENTRY_BAR_MISSING = "entry_bar_missing"
REASON_ENTRY_OPEN_MISSING = "entry_open_missing"
REASON_EXIT_BAR_MISSING = "exit_bar_missing"
REASON_EXIT_CLOSE_MISSING = "exit_close_missing"
REASON_INVALID_OHLC = "invalid_ohlc"
REASON_HORIZON_UNPARSEABLE = "horizon_unparseable"
REASON_PREDICTION_AS_OF_MISSING = "prediction_as_of_missing"
REASON_NO_BARS = "no_bars"
REASON_RETRY_EXHAUSTED = "retry_exhausted"


def _string(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_horizon(value: Any) -> Optional[int]:
    """Return the holding length in trading sessions, or ``None`` if unparseable.

    Accepts the forms actually present in the database: ``5d``, ``5``, ``5D``,
    ``next_day``, ``5_day``, ``5 trading days``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        days = int(value)
        return days if days > 0 else None
    token = _string(value).lower()
    if not token:
        return None
    if token in {"next_day", "nextday", "next session", "next_session", "1", "1d", "t+1"}:
        return 1
    digits = ""
    for char in token:
        if char.isdigit():
            digits += char
        elif digits:
            break
    if not digits:
        return None
    days = int(digits)
    return days if days > 0 else None


def _bar_date(bar: dict[str, Any]) -> Optional[date]:
    raw = bar.get("date") or bar.get("time") or bar.get("trade_date") or bar.get("timestamp")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    token = _string(raw)
    if not token:
        return None
    for length, fmt in ((10, "%Y-%m-%d"), (8, "%Y%m%d")):
        try:
            return datetime.strptime(token[:length], fmt).date()
        except ValueError:
            continue
    return None


def _sorted_bars(bars: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalise storage payloads to date-ordered dicts, dropping undated rows."""
    out: list[dict[str, Any]] = []
    for item in bars or []:
        if isinstance(item, dict):
            bar = dict(item)
        else:
            continue
        day = _bar_date(bar)
        if day is None:
            continue
        bar["_day"] = day
        out.append(bar)
    out.sort(key=lambda bar: bar["_day"])
    return out


def _ohlc_ok(bar: dict[str, Any]) -> bool:
    values = [_finite(bar.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in values):
        return False
    open_, high, low, close = values  # type: ignore[misc]
    if open_ <= 0 or high <= 0 or low <= 0 or close <= 0:
        return False
    return high >= max(open_, close, low) and low <= min(open_, close, high)


@dataclass(slots=True)
class HorizonScore:
    status: str
    net_return: Optional[float]
    direction_hit: Optional[bool]
    reason: Optional[str]
    entry_date: Optional[str]
    exit_date: Optional[str]
    entry_open: Optional[float]
    exit_close: Optional[float]
    cost: float
    horizon_days: Optional[int]

    def as_outcome(self, prediction: dict[str, Any]) -> dict[str, Any]:
        return {
            "prediction_id": prediction.get("prediction_id"),
            "strategy_id": prediction.get("strategy_id"),
            "stock_code": prediction.get("stock_code"),
            "actual_trading_date": self.exit_date,
            "score_version": HORIZON_SCORE_VERSION,
            "score_status": self.status,
            "trade_prediction_score": None if self.direction_hit is None else (1.0 if self.direction_hit else 0.0),
            "data_quality_status": "ok" if self.status == STATUS_OK else ("missing" if self.net_return is None else "partial"),
            "outcome_json": {
                "score_version": HORIZON_SCORE_VERSION,
                "scorer_version": HORIZON_SCORER_VERSION,
                "score_status": self.status,
                "reason": self.reason,
                "direction_hit": self.direction_hit,
                "net_return": self.net_return,
                "entry_date": self.entry_date,
                "exit_date": self.exit_date,
                "entry_open": self.entry_open,
                "exit_close": self.exit_close,
                "cost": self.cost,
                "horizon_days": self.horizon_days,
                "direction": _string(prediction.get("direction")).lower() or None,
            },
            "metadata": {
                "source": "incubation_factory.horizon_scorer",
                "scorer_version": HORIZON_SCORER_VERSION,
            },
        }


def score_horizon(
    prediction: dict[str, Any],
    bars: Iterable[Any],
    *,
    as_of: Optional[date] = None,
    cost: float = DEFAULT_ROUND_TRIP_COST,
    flat_band: float = DEFAULT_FLAT_BAND,
) -> HorizonScore:
    """Score one frozen prediction on the net-of-cost horizon.

    ``as_of`` is the last session that is known to be *closed*; it defaults to
    no upper bound. Passing it is how the caller prevents scoring on a bar that
    is still forming.
    """
    contract = prediction.get("contract_json") or {}
    if not isinstance(contract, dict):
        contract = {}
    horizon_days = parse_horizon(prediction.get("horizon") or contract.get("horizon"))
    as_of_date = prediction.get("prediction_as_of") or contract.get("prediction_as_of")
    if isinstance(as_of_date, datetime):
        as_of_date = as_of_date.date()
    if isinstance(as_of_date, str):
        try:
            as_of_date = datetime.strptime(as_of_date[:10], "%Y-%m-%d").date()
        except ValueError:
            as_of_date = None
    if not isinstance(as_of_date, date):
        return HorizonScore(STATUS_UNAVAILABLE, None, None, REASON_PREDICTION_AS_OF_MISSING, None, None, None, None, cost, horizon_days)
    if horizon_days is None:
        return HorizonScore(STATUS_UNAVAILABLE, None, None, REASON_HORIZON_UNPARSEABLE, None, None, None, None, cost, None)

    series = _sorted_bars(bars)
    if not series:
        return HorizonScore(STATUS_PENDING, None, None, REASON_NO_BARS, None, None, None, None, cost, horizon_days)

    limit = as_of if isinstance(as_of, date) else None
    entry_index = next((index for index, bar in enumerate(series) if bar["_day"] > as_of_date and (limit is None or bar["_day"] <= limit)), None)
    if entry_index is None:
        # Either nothing after as_of yet, or the session after as_of has not closed.
        return HorizonScore(STATUS_PENDING, None, None, REASON_ENTRY_BAR_MISSING, None, None, None, None, cost, horizon_days)

    exit_index = entry_index + horizon_days
    if exit_index >= len(series) or (limit is not None and series[exit_index]["_day"] > limit):
        return HorizonScore(STATUS_PENDING, None, None, REASON_EXIT_BAR_MISSING, None, None, None, None, cost, horizon_days)

    entry_bar = series[entry_index]
    exit_bar = series[exit_index]
    if not _ohlc_ok(entry_bar):
        reason = REASON_ENTRY_OPEN_MISSING if _finite(entry_bar.get("open")) in (None, 0.0) else REASON_INVALID_OHLC
        return HorizonScore(STATUS_UNAVAILABLE, None, None, reason, entry_bar["_day"].isoformat(), None, None, None, cost, horizon_days)
    if not _ohlc_ok(exit_bar):
        reason = REASON_EXIT_CLOSE_MISSING if _finite(exit_bar.get("close")) in (None, 0.0) else REASON_INVALID_OHLC
        return HorizonScore(STATUS_UNAVAILABLE, None, None, reason, entry_bar["_day"].isoformat(), exit_bar["_day"].isoformat(), _finite(entry_bar.get("open")), None, cost, horizon_days)

    entry_open = float(entry_bar["open"])
    exit_close = float(exit_bar["close"])
    if entry_open <= 0:
        return HorizonScore(STATUS_UNAVAILABLE, None, None, REASON_ENTRY_OPEN_MISSING, entry_bar["_day"].isoformat(), exit_bar["_day"].isoformat(), None, exit_close, cost, horizon_days)

    net = exit_close / (entry_open * (1.0 + cost)) - 1.0
    direction = _string(prediction.get("direction") or contract.get("direction")).lower()
    band = flat_band if flat_band >= 0 else DEFAULT_FLAT_BAND
    if direction == "up":
        hit: Optional[bool] = net > band
    elif direction == "down":
        hit = net < -band
    elif direction == "neutral":
        hit = None
        status = STATUS_NEUTRAL_EXCLUDED
        return HorizonScore(status, net, hit, "neutral_excluded_from_direction_denominator", entry_bar["_day"].isoformat(), exit_bar["_day"].isoformat(), entry_open, exit_close, cost, horizon_days)
    else:
        return HorizonScore(STATUS_UNAVAILABLE, net, None, "direction_unrecognised", entry_bar["_day"].isoformat(), exit_bar["_day"].isoformat(), entry_open, exit_close, cost, horizon_days)

    return HorizonScore(STATUS_OK, net, hit, None, entry_bar["_day"].isoformat(), exit_bar["_day"].isoformat(), entry_open, exit_close, cost, horizon_days)


def _bars_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "bars", "items", "klines", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return [dict(payload)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    return []


def _code_candidates(code: str) -> list[str]:
    raw = _string(code)
    if not raw:
        return []
    base = raw.split(".")[0]
    candidates = [raw, raw.lower(), base]
    if len(base) == 6 and base.isdigit():
        candidates.extend([f"sh{base}" if base.startswith("6") else f"sz{base}", f"{base}.SH" if base.startswith("6") else f"{base}.SZ"])
    unique: list[str] = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    return unique


async def load_daily_bars(db: Any, code: str, *, before: date) -> list[dict[str, Any]]:
    """Load the daily series up to ``before`` for the first code alias that has data."""
    if not hasattr(db, "get_klines"):
        return []
    # An unbounded lookup must not become the string "9999-12-31": the adapter
    # combines the bound with the market timezone, and zoneinfo overflows
    # ``date.max`` — the first release of this pass therefore saw every series
    # as "no_bars" even when the stock had a full 301-bar history.
    end_date: Optional[str] = None if before == date.max else before.isoformat()
    for candidate in _code_candidates(code):
        try:
            payload = await db.get_klines(candidate, end_date=end_date, limit=400)
        except TypeError:
            try:
                payload = await db.get_klines(candidate, None, end_date, 400)
            except Exception as exc:  # storage shape differs; report, do not fabricate
                logger.debug("kline lookup failed for %s: %s", candidate, exc)
                continue
        except Exception as exc:
            logger.debug("kline lookup failed for %s: %s", candidate, exc)
            continue
        bars = _bars_from_payload(payload)
        if bars:
            return bars
    return []


@dataclass(slots=True)
class HorizonScorer:
    """Batch scorer over pending predictions; persists one outcome per prediction."""

    cost: float = DEFAULT_ROUND_TRIP_COST
    flat_band: float = DEFAULT_FLAT_BAND
    max_retry_attempts: int = MAX_RETRY_ATTEMPTS

    async def verify_pending(
        self,
        db: Any,
        *,
        as_of: Optional[date] = None,
        limit: int = 500,
        persist: bool = True,
    ) -> dict[str, Any]:
        if not hasattr(db, "list_strategy_trade_predictions"):
            return {"status": "degraded", "reason": "prediction_storage_unavailable", "evaluated": 0}
        pending = await db.list_strategy_trade_predictions(
            pending_for_outcome=True,
            exclude_outcome_score_version=HORIZON_SCORE_VERSION,
            limit=limit,
        )
        # An outcome with this score version means "already scored", so a
        # prediction is listed again only while its resumable state (pending or
        # not yet persisted) remains. Retry counting therefore happens on the
        # pending rows themselves, not on this list.
        retry_counts = await self._retry_counts(db)
        results: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        bar_cache: dict[str, list[dict[str, Any]]] = {}
        for prediction in pending:
            prediction_id = _string(prediction.get("prediction_id"))
            attempts = retry_counts.get(prediction_id, 0)
            code = _string(prediction.get("stock_code"))
            if code not in bar_cache:
                bar_cache[code] = await load_daily_bars(db, code, before=(as_of or date.max))
            score = score_horizon(prediction, bar_cache[code], as_of=as_of, cost=self.cost, flat_band=self.flat_band)
            if score.status == STATUS_PENDING and attempts >= self.max_retry_attempts:
                score = HorizonScore(
                    STATUS_UNAVAILABLE,
                    score.net_return,
                    score.direction_hit,
                    f"{REASON_RETRY_EXHAUSTED}:{score.reason}",
                    score.entry_date,
                    score.exit_date,
                    score.entry_open,
                    score.exit_close,
                    score.cost,
                    score.horizon_days,
                )
            outcome = score.as_outcome(prediction)
            # Counting this pass: a still-pending row increments, a terminal
            # row keeps what it took to get here.
            outcome["metadata"]["retry_attempts"] = attempts + 1 if score.status == STATUS_PENDING else attempts
            if persist and hasattr(db, "save_strategy_trade_prediction_outcome"):
                await db.save_strategy_trade_prediction_outcome(outcome)
            results.append(outcome)
            status = outcome["score_status"]
            counts[status] = counts.get(status, 0) + 1
        return {
            "status": "ok",
            "scorer_version": HORIZON_SCORER_VERSION,
            "score_version": HORIZON_SCORE_VERSION,
            "evaluated": len(results),
            "score_status_counts": counts,
            "reason_counts": _reason_counts(results),
            "outcomes": results,
        }

    async def _retry_counts(self, db: Any) -> dict[str, int]:
        """Attempts per prediction, read in one query.

        ``pending_for_outcome`` excludes a prediction only when an outcome with
        this score version exists, so a retried prediction is still returned by
        the list query; its attempts live on the previous pending row.
        """
        if not hasattr(db, "fetch"):
            return {}
        try:
            rows = await db.fetch(
                """
                SELECT prediction_id, MAX(CAST(COALESCE(json_extract(metadata, '$.retry_attempts'), 0) AS INTEGER)) AS attempts
                FROM strategy_trade_prediction_outcomes
                WHERE score_version = $1
                GROUP BY prediction_id
                """,
                HORIZON_SCORE_VERSION,
            )
        except Exception as exc:
            logger.debug("retry count query failed: %s", exc)
            return {}
        counts: dict[str, int] = {}
        for row in rows or []:
            payload = dict(row) if not isinstance(row, dict) else row
            prediction_id = _string(payload.get("prediction_id"))
            try:
                value = int(payload.get("attempts") or 0)
            except (TypeError, ValueError):
                value = 0
            if prediction_id:
                counts[prediction_id] = value
        return counts


def _reason_counts(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        payload = outcome.get("outcome_json") or {}
        reason = payload.get("reason")
        if not reason:
            continue
        code = str(reason).split(":", 1)[0]
        counts[code] = counts.get(code, 0) + 1
    return counts


__all__ = [
    "DEFAULT_FLAT_BAND",
    "DEFAULT_ROUND_TRIP_COST",
    "HORIZON_SCORER_VERSION",
    "HORIZON_SCORE_VERSION",
    "HorizonScore",
    "HorizonScorer",
    "MAX_RETRY_ATTEMPTS",
    "STATUS_OK",
    "STATUS_PENDING",
    "STATUS_UNAVAILABLE",
    "load_daily_bars",
    "parse_horizon",
    "score_horizon",
]
