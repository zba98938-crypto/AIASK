"""Trade prediction outcome scoring for incubation observability.

This module evaluates frozen Strategy Factory trade prediction contracts without
mutating the contract payload. Daily scoring is diagnostic by default; intraday
replay only writes v2 metrics when real intraday bars are available.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DAILY_SCORE_VERSION = "trade_prediction_score_daily_v1"
INTRADAY_SCORE_VERSION = "trade_prediction_score_v2"


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    fallback = _finite_float(default)
    try:
        if value in (None, ""):
            return fallback
        numeric = float(value)
    except Exception:
        return fallback
    return numeric if math.isfinite(numeric) else fallback


def _safe_int(value: Any, default: int = 0) -> int:
    fallback = _finite_float(default)
    try:
        if value in (None, ""):
            return int(fallback if fallback is not None else 0)
        numeric = float(value)
    except Exception:
        return int(fallback if fallback is not None else 0)
    if not math.isfinite(numeric):
        return int(fallback if fallback is not None else 0)
    return int(numeric)


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except Exception:
        return None
    return numeric if math.isfinite(numeric) else None


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    token = _string(value)
    if not token:
        return None
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00")).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(token[:10], fmt).date()
        except Exception:
            continue
    return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    token = _string(value)
    if not token:
        return None
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M"):
        try:
            return datetime.strptime(token, fmt)
        except Exception:
            continue
    return None


def _direction_token(value: Any) -> str:
    token = _string(value).lower()
    aliases = {
        "buy": "up",
        "long": "up",
        "bullish": "up",
        "rise": "up",
        "sell": "down",
        "short": "down",
        "bearish": "down",
        "fall": "down",
        "flat": "neutral",
        "hold": "neutral",
    }
    return aliases.get(token, token or "neutral")


def _normalize_code_candidates(code: str) -> list[str]:
    raw = _string(code)
    if not raw:
        return []
    base = raw.split(".")[0]
    lowered = raw.lower()
    candidates = [raw, lowered, base]
    if len(base) == 6:
        if base.startswith("6"):
            candidates.extend([f"sh{base}", f"{base}.SH"])
        else:
            candidates.extend([f"sz{base}", f"{base}.SZ"])
    unique: list[str] = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    return unique


def _target_range(contract: dict[str, Any]) -> tuple[float | None, float | None]:
    raw = contract.get("target_price_range") or contract.get("target_range") or contract.get("target_price")
    if isinstance(raw, dict):
        low = _safe_float(raw.get("low") or raw.get("min") or raw.get("lower"))
        high = _safe_float(raw.get("high") or raw.get("max") or raw.get("upper"))
    elif isinstance(raw, (list, tuple)) and raw:
        low = _safe_float(raw[0])
        high = _safe_float(raw[-1])
    else:
        low = high = _safe_float(raw)
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high


def _risk_pct(contract: dict[str, Any], key: str, fallback: float) -> float:
    risk = contract.get("risk_rules") or contract.get("risk") or {}
    if not isinstance(risk, dict):
        risk = {}
    value = (
        contract.get(key)
        or contract.get(f"{key}_pct")
        or risk.get(key)
        or risk.get(f"{key}_pct")
    )
    pct = abs(_safe_float(value, fallback) or fallback)
    if pct > 1.0:
        pct /= 100.0
    return max(0.0001, pct)


def _score_bool(value: Any, neutral: float = 0.5) -> float:
    coerced = _safe_bool(value)
    if coerced is None:
        return neutral
    return 1.0 if coerced else 0.0


def _latest_event_as_of_ok(contract: dict[str, Any]) -> tuple[bool, list[str]]:
    prediction_as_of = _parse_datetime(contract.get("prediction_as_of"))
    if prediction_as_of is None:
        return True, []
    rejected: list[str] = []
    refs = contract.get("evidence_refs") or contract.get("event_refs") or contract.get("factor_refs") or []
    if isinstance(refs, dict):
        refs = list(refs.values())
    if not isinstance(refs, list):
        refs = [refs]
    for item in refs:
        if not isinstance(item, dict):
            continue
        ts = _parse_datetime(item.get("observed_at") or item.get("evidence_time") or item.get("timestamp") or item.get("as_of"))
        if ts and ts > prediction_as_of:
            rejected.append(_string(item.get("id") or item.get("ref") or ts.isoformat()))
    return not rejected, rejected


def _extract_bars(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "bars", "items", "klines", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return [payload]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    return []


async def _find_daily_bar(db: Any, code: str, target_date: date) -> tuple[dict[str, Any] | None, str | None]:
    if not hasattr(db, "get_klines"):
        return None, None
    target = target_date.isoformat()
    for candidate in _normalize_code_candidates(code):
        try:
            bars = await db.get_klines(candidate, start_date=target, end_date=target, limit=5)
        except TypeError:
            bars = await db.get_klines(candidate, target, target, 5)
        except Exception as exc:
            logger.debug("daily bar lookup failed for %s: %s", candidate, exc)
            continue
        for bar in _extract_bars(bars):
            bar_date = _parse_date(bar.get("date") or bar.get("time") or bar.get("timestamp"))
            if bar_date == target_date:
                return bar, candidate
    return None, None


def _ohlc_status(bar: dict[str, Any]) -> str:
    raw_values = [bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")]
    if any(value in (None, "") for value in raw_values):
        return "daily_bar_missing"
    open_ = _safe_float(bar.get("open"))
    high = _safe_float(bar.get("high"))
    low = _safe_float(bar.get("low"))
    close = _safe_float(bar.get("close"))
    if open_ is None or high is None or low is None or close is None:
        return "invalid_ohlc"
    if high < max(open_, low, close) or low > min(open_, high, close):
        return "invalid_ohlc"
    return "ok"


def _daily_outcome(prediction: dict[str, Any], bar: dict[str, Any], *, resolved_code: str | None = None) -> dict[str, Any]:
    contract = dict(prediction.get("contract_json") or {})
    target_date = _parse_date(prediction.get("target_trading_date") or contract.get("target_trading_date"))
    direction = _direction_token(prediction.get("direction") or contract.get("direction"))
    open_ = _safe_float(bar.get("open"), 0.0) or 0.0
    high = _safe_float(bar.get("high"), open_) or open_
    low = _safe_float(bar.get("low"), open_) or open_
    close = _safe_float(bar.get("close"), open_) or open_
    planned_return = (close - open_) / open_ if open_ > 0 else 0.0
    flat_threshold = _safe_float(contract.get("flat_threshold_pct"), 0.002) or 0.002
    if flat_threshold > 1.0:
        flat_threshold /= 100.0
    if direction == "up":
        direction_hit = planned_return > flat_threshold
        adverse_move = (open_ - low) / open_ if open_ > 0 else 0.0
    elif direction == "down":
        direction_hit = planned_return < -flat_threshold
        adverse_move = (high - open_) / open_ if open_ > 0 else 0.0
    else:
        direction_hit = abs(planned_return) <= flat_threshold
        adverse_move = abs(planned_return)
    target_low, target_high = _target_range(contract)
    target_touch = None
    if target_low is not None or target_high is not None:
        lower = target_low if target_low is not None else target_high
        upper = target_high if target_high is not None else target_low
        target_touch = bool(high >= float(lower) and low <= float(upper))
    stop_loss_pct = _risk_pct(contract, "stop_loss", 0.08)
    risk_proxy_score = max(0.0, min(1.0, 1.0 - max(0.0, adverse_move) / stop_loss_pct))
    target_component = _score_bool(target_touch, neutral=0.5)
    score = 0.55 * _score_bool(direction_hit) + 0.25 * target_component + 0.20 * risk_proxy_score
    as_of_ok, rejected_refs = _latest_event_as_of_ok(contract)
    score_status = "ok" if as_of_ok else "post_hoc_rejected"
    if target_touch is None:
        score_status = "partial_daily_only"
    return {
        "prediction_id": prediction.get("prediction_id"),
        "strategy_id": prediction.get("strategy_id"),
        "stock_code": prediction.get("stock_code"),
        "actual_trading_date": target_date.isoformat() if target_date else None,
        "score_version": DAILY_SCORE_VERSION,
        "score_status": score_status,
        "trade_prediction_score": round(max(0.0, min(1.0, score)), 6),
        "data_quality_status": "ok",
        "outcome_json": {
            "score_version": DAILY_SCORE_VERSION,
            "score_status": score_status,
            "data_quality_status": "ok",
            "direction_hit": bool(direction_hit),
            "target_touch": target_touch,
            "risk_proxy_score": round(risk_proxy_score, 6),
            "time_bucket_hit_rate": None,
            "entry_window_hit": None,
            "exit_window_hit": None,
            "planned_trade_return": round(planned_return, 6),
            "actual_trading_date": target_date.isoformat() if target_date else None,
            "resolved_daily_code": resolved_code,
            "post_hoc_rejected_refs": rejected_refs,
        },
        "metadata": {
            "source": "incubation_factory.trade_prediction_daily_verifier",
            "direction": direction,
        },
    }


def _missing_outcome(prediction: dict[str, Any], *, score_status: str, data_quality_status: str, reason: str) -> dict[str, Any]:
    contract = dict(prediction.get("contract_json") or {})
    target_date = _parse_date(prediction.get("target_trading_date") or contract.get("target_trading_date"))
    return {
        "prediction_id": prediction.get("prediction_id"),
        "strategy_id": prediction.get("strategy_id"),
        "stock_code": prediction.get("stock_code"),
        "actual_trading_date": target_date.isoformat() if target_date else None,
        "score_version": DAILY_SCORE_VERSION,
        "score_status": score_status,
        "trade_prediction_score": None,
        "data_quality_status": data_quality_status,
        "outcome_json": {
            "score_version": DAILY_SCORE_VERSION,
            "score_status": score_status,
            "data_quality_status": data_quality_status,
            "direction_hit": None,
            "target_touch": None,
            "risk_proxy_score": None,
            "time_bucket_hit_rate": None,
            "entry_window_hit": None,
            "exit_window_hit": None,
            "planned_trade_return": None,
            "actual_trading_date": target_date.isoformat() if target_date else None,
            "reason": reason,
        },
        "metadata": {
            "source": "incubation_factory.trade_prediction_daily_verifier",
            "reason": reason,
        },
    }


def _window_to_times(value: Any) -> tuple[time | None, time | None]:
    if isinstance(value, dict):
        value = value.get("window") or value.get("time") or value.get("range") or f"{value.get('start', '')}-{value.get('end', '')}"
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start_raw, end_raw = value[0], value[1]
    else:
        token = _string(value)
        if not token:
            return None, None
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
        elif "~" in token:
            start_raw, end_raw = token.split("~", 1)
        else:
            start_raw, end_raw = token, token
    return _parse_clock(start_raw), _parse_clock(end_raw)


def _parse_clock(value: Any) -> time | None:
    token = _string(value)
    if not token:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(token[:8], fmt).time()
        except Exception:
            continue
    return None


def _bar_dt(bar: dict[str, Any]) -> datetime | None:
    return _parse_datetime(bar.get("timestamp") or bar.get("time") or bar.get("date"))


def _bars_in_window(bars: Iterable[dict[str, Any]], window: Any) -> list[dict[str, Any]]:
    start, end = _window_to_times(window)
    if start is None or end is None:
        return []
    selected: list[dict[str, Any]] = []
    for bar in bars:
        dt = _bar_dt(bar)
        if not dt:
            continue
        clock = dt.time()
        if start <= clock <= end:
            selected.append(bar)
    return selected


def _window_hit(bars: list[dict[str, Any]], contract: dict[str, Any], direction: str) -> bool | None:
    if not bars:
        return None
    target_low, target_high = _target_range(contract)
    if target_low is not None or target_high is not None:
        lower = target_low if target_low is not None else target_high
        upper = target_high if target_high is not None else target_low
        return any((_safe_float(bar.get("high"), 0.0) or 0.0) >= float(lower) and (_safe_float(bar.get("low"), 0.0) or 0.0) <= float(upper) for bar in bars)
    first_open = _safe_float(bars[0].get("open"), 0.0) or 0.0
    last_close = _safe_float(bars[-1].get("close"), first_open) or first_open
    if first_open <= 0:
        return None
    ret = (last_close - first_open) / first_open
    if direction == "down":
        return ret < 0
    if direction == "neutral":
        return abs(ret) <= 0.002
    return ret > 0


def _bar_quality_status(bars: list[dict[str, Any]]) -> str:
    if not bars:
        return "intraday_missing"
    if any(_ohlc_status(dict(bar or {})) != "ok" for bar in bars):
        return "invalid_ohlc"
    if any(_string(bar.get("data_quality_status")).lower() == "invalid_ohlc" for bar in bars):
        return "invalid_ohlc"
    if any(_string(bar.get("data_quality_status")).lower() not in {"", "ok"} for bar in bars):
        return "partial_gap"
    return "ok"


@dataclass(slots=True)
class IntradayReplayService:
    period: str = "5m"
    min_bars: int = 8

    async def replay_prediction(self, db: Any, prediction: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        if not hasattr(db, "list_intraday_bars"):
            return await self._write_partial(db, prediction, "intraday_storage_unavailable", persist=persist)
        contract = dict(prediction.get("contract_json") or {})
        target_date = _parse_date(prediction.get("target_trading_date") or contract.get("target_trading_date"))
        if target_date is None:
            return await self._write_partial(db, prediction, "target_trading_date_missing", persist=persist)
        bars: list[dict[str, Any]] = []
        resolved_code = None
        for candidate in _normalize_code_candidates(_string(prediction.get("stock_code"))):
            try:
                rows = await db.list_intraday_bars(
                    candidate,
                    self.period,
                    start_ts=f"{target_date.isoformat()} 09:30:00",
                    end_ts=f"{target_date.isoformat()} 15:00:00",
                )
            except Exception as exc:
                logger.debug("intraday lookup failed for %s: %s", candidate, exc)
                continue
            if rows:
                bars = list(rows)
                resolved_code = candidate
                break
        quality_status = _bar_quality_status(bars)
        if quality_status != "ok" or len(bars) < self.min_bars:
            reason = quality_status if quality_status != "ok" else f"insufficient_intraday_bars:{len(bars)}<{self.min_bars}"
            return await self._write_partial(db, prediction, reason, persist=persist)
        outcome = self._score_intraday(prediction, bars, resolved_code=resolved_code)
        if persist and hasattr(db, "save_strategy_trade_prediction_outcome"):
            await db.save_strategy_trade_prediction_outcome(outcome)
        return outcome

    def _score_intraday(self, prediction: dict[str, Any], bars: list[dict[str, Any]], *, resolved_code: str | None) -> dict[str, Any]:
        contract = dict(prediction.get("contract_json") or {})
        direction = _direction_token(prediction.get("direction") or contract.get("direction"))
        target_date = _parse_date(prediction.get("target_trading_date") or contract.get("target_trading_date"))
        entry_window = contract.get("entry_window") or (contract.get("entry_plan") or {}).get("window")
        exit_window = contract.get("exit_window") or (contract.get("exit_plan") or {}).get("latest_exit_window")
        entry_bars = _bars_in_window(bars, entry_window)
        exit_bars = _bars_in_window(bars, exit_window)
        entry_window_hit = bool(entry_bars) if entry_window else None
        exit_window_hit = bool(exit_bars) if exit_window else None
        bucket_hits: list[bool] = []
        for bucket in contract.get("time_buckets") or []:
            hit = _window_hit(_bars_in_window(bars, bucket), contract, direction)
            if hit is not None:
                bucket_hits.append(bool(hit))
        time_bucket_hit_rate = sum(1 for item in bucket_hits if item) / len(bucket_hits) if bucket_hits else None

        entry_price = _safe_float((entry_bars or bars)[0].get("open"), 0.0) or 0.0
        exit_price = _safe_float((exit_bars or bars)[-1].get("close"), entry_price) or entry_price
        planned_return = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
        if direction == "down":
            planned_return = -planned_return
        target_low, target_high = _target_range(contract)
        target_touch = None
        if target_low is not None or target_high is not None:
            lower = target_low if target_low is not None else target_high
            upper = target_high if target_high is not None else target_low
            target_touch = any((_safe_float(bar.get("high"), 0.0) or 0.0) >= float(lower) and (_safe_float(bar.get("low"), 0.0) or 0.0) <= float(upper) for bar in bars)
        direction_hit = planned_return > 0.0 if direction != "neutral" else abs(planned_return) <= 0.002
        stop_loss_pct = _risk_pct(contract, "stop_loss", 0.08)
        if direction == "down":
            adverse = max(((_safe_float(bar.get("high"), entry_price) or entry_price) - entry_price) / entry_price for bar in bars) if entry_price > 0 else 0.0
        else:
            adverse = max((entry_price - (_safe_float(bar.get("low"), entry_price) or entry_price)) / entry_price for bar in bars) if entry_price > 0 else 0.0
        risk_proxy_score = max(0.0, min(1.0, 1.0 - max(0.0, adverse) / stop_loss_pct))
        score = (
            0.35 * _score_bool(direction_hit)
            + 0.20 * _score_bool(target_touch, neutral=0.5)
            + 0.15 * risk_proxy_score
            + 0.15 * _score_bool(entry_window_hit, neutral=0.5)
            + 0.10 * _score_bool(exit_window_hit, neutral=0.5)
            + 0.05 * (time_bucket_hit_rate if time_bucket_hit_rate is not None else 0.5)
        )
        return {
            "prediction_id": prediction.get("prediction_id"),
            "strategy_id": prediction.get("strategy_id"),
            "stock_code": prediction.get("stock_code"),
            "actual_trading_date": target_date.isoformat() if target_date else None,
            "score_version": INTRADAY_SCORE_VERSION,
            "score_status": "ok",
            "trade_prediction_score": round(max(0.0, min(1.0, score)), 6),
            "data_quality_status": "ok",
            "outcome_json": {
                "score_version": INTRADAY_SCORE_VERSION,
                "score_status": "ok",
                "data_quality_status": "ok",
                "direction_hit": bool(direction_hit),
                "target_touch": target_touch,
                "risk_proxy_score": round(risk_proxy_score, 6),
                "time_bucket_hit_rate": round(time_bucket_hit_rate, 6) if time_bucket_hit_rate is not None else None,
                "entry_window_hit": entry_window_hit,
                "exit_window_hit": exit_window_hit,
                "planned_trade_return": round(planned_return, 6),
                "actual_trading_date": target_date.isoformat() if target_date else None,
                "resolved_intraday_code": resolved_code,
                "bar_count": len(bars),
            },
            "metadata": {"source": "incubation_factory.intraday_replay_service", "period": self.period},
        }

    async def _write_partial(self, db: Any, prediction: dict[str, Any], reason: str, *, persist: bool) -> dict[str, Any]:
        data_quality = "intraday_missing" if "missing" in reason or "unavailable" in reason else "partial_gap"
        outcome = dict(_missing_outcome(prediction, score_status="partial_intraday_missing", data_quality_status=data_quality, reason=reason))
        outcome["score_version"] = INTRADAY_SCORE_VERSION
        outcome["outcome_json"]["score_version"] = INTRADAY_SCORE_VERSION
        if persist and hasattr(db, "save_strategy_trade_prediction_outcome"):
            await db.save_strategy_trade_prediction_outcome(outcome)
        return outcome


@dataclass(slots=True)
class TradePredictionDailyVerifier:
    replay_service: IntradayReplayService | None = None

    async def verify_pending(
        self,
        db: Any,
        *,
        as_of: date | None = None,
        limit: int = 200,
        include_intraday: bool = True,
        sync_intraday_before_replay: bool = False,
        intraday_period: str = "5m",
        intraday_limit: int = 300,
        persist: bool = True,
    ) -> dict[str, Any]:
        today = as_of or date.today()
        if not hasattr(db, "list_strategy_trade_predictions"):
            return {"status": "degraded", "reason": "prediction_storage_unavailable", "evaluated": 0}
        predictions = await db.list_strategy_trade_predictions(
            target_trading_date_lte=today.isoformat(),
            pending_for_outcome=True,
            exclude_outcome_score_version=DAILY_SCORE_VERSION,
            limit=limit,
        )
        outcomes: list[dict[str, Any]] = []
        intraday_outcomes: list[dict[str, Any]] = []
        intraday_sync: dict[str, Any] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "period": intraday_period,
            "status_counts": {},
            "errors": [],
        }
        for prediction in predictions:
            target_date = _parse_date(prediction.get("target_trading_date") or (prediction.get("contract_json") or {}).get("target_trading_date"))
            if target_date is None or target_date > today:
                outcome = _missing_outcome(prediction, score_status="pending_market_data", data_quality_status="daily_bar_missing", reason="target_date_future_or_missing")
            else:
                bar, resolved_code = await _find_daily_bar(db, _string(prediction.get("stock_code")), target_date)
                if not bar:
                    outcome = _missing_outcome(prediction, score_status="pending_market_data", data_quality_status="daily_bar_missing", reason="daily_bar_missing")
                else:
                    quality = _ohlc_status(bar)
                    if quality != "ok":
                        outcome = _missing_outcome(prediction, score_status="pending_market_data", data_quality_status=quality, reason=quality)
                    else:
                        outcome = _daily_outcome(prediction, bar, resolved_code=resolved_code)
            if persist and hasattr(db, "save_strategy_trade_prediction_outcome"):
                await db.save_strategy_trade_prediction_outcome(outcome)
            outcomes.append(outcome)
            if include_intraday:
                if sync_intraday_before_replay and target_date is not None and target_date <= today:
                    code = _string(prediction.get("stock_code") or (prediction.get("contract_json") or {}).get("stock_code"))
                    if code:
                        intraday_sync["attempted"] += 1
                        try:
                            sync_result = await sync_minute_kline_to_storage(
                                db,
                                code,
                                period=intraday_period,
                                limit=intraday_limit,
                            )
                            status = _string(sync_result.get("status")) or "unknown"
                            status_counts = intraday_sync["status_counts"]
                            status_counts[status] = int(status_counts.get(status) or 0) + 1
                            if status == "ok":
                                intraday_sync["succeeded"] += 1
                            else:
                                intraday_sync["failed"] += 1
                                if len(intraday_sync["errors"]) < 10:
                                    intraday_sync["errors"].append({
                                        "prediction_id": prediction.get("prediction_id"),
                                        "code": code,
                                        "status": status,
                                        "reason": sync_result.get("reason"),
                                    })
                        except Exception as exc:
                            intraday_sync["failed"] += 1
                            status_counts = intraday_sync["status_counts"]
                            status_counts["exception"] = int(status_counts.get("exception") or 0) + 1
                            if len(intraday_sync["errors"]) < 10:
                                intraday_sync["errors"].append({
                                    "prediction_id": prediction.get("prediction_id"),
                                    "code": code,
                                    "status": "exception",
                                    "reason": str(exc),
                                    "error_type": type(exc).__name__,
                                })
                    else:
                        intraday_sync["skipped"] += 1
                replay = self.replay_service or IntradayReplayService(period=intraday_period)
                intraday_outcomes.append(await replay.replay_prediction(db, prediction, persist=persist))
        status_counts: dict[str, int] = {}
        data_quality_counts: dict[str, int] = {}
        for outcome in outcomes + intraday_outcomes:
            status = _string(outcome.get("score_status")) or "unknown"
            quality = _string(outcome.get("data_quality_status")) or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
            data_quality_counts[quality] = data_quality_counts.get(quality, 0) + 1
        return {
            "status": "ok",
            "score_version": DAILY_SCORE_VERSION,
            "intraday_score_version": INTRADAY_SCORE_VERSION if include_intraday else None,
            "evaluated": len(outcomes),
            "intraday_evaluated": len(intraday_outcomes),
            "score_status_counts": status_counts,
            "data_quality_status_counts": data_quality_counts,
            "intraday_sync": intraday_sync,
            "outcomes": outcomes,
            "intraday_outcomes": intraday_outcomes,
        }


async def sync_minute_kline_to_storage(
    db: Any,
    code: str,
    *,
    period: str = "5m",
    limit: int = 300,
    adjust: str = "",
    bars: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist already-supplied local minute bars into Quant Core storage.

    Incubation runtime is DB-only: market acquisition happens in explicit sync
    jobs/tools, while verification only consumes bars that are already local.
    """

    if not hasattr(db, "save_intraday_bars"):
        return {"status": "degraded", "reason": "intraday_storage_unavailable", "accepted_count": 0}

    supplied_bars = list(bars or [])
    if not supplied_bars:
        return {
            "status": "source_unavailable",
            "reason": "intraday_source_unavailable_db_only",
            "accepted_count": 0,
            "code": code,
            "period": period,
        }

    source_chain = [{"source": "local_intraday_bars", "mode": "db_only"}]
    normalized: list[dict[str, Any]] = []
    for bar in supplied_bars[: max(1, _safe_int(limit, 1))]:
        item = dict(bar)
        item.setdefault("code", code)
        item.setdefault("period", period)
        item.setdefault("timestamp", item.get("date") or item.get("time"))
        item.setdefault("source_chain", source_chain)
        item.setdefault("source", item.get("source") or "local_intraday_bars")
        normalized.append(item)
    summary = await db.save_intraday_bars(code, normalized, period=period, adjust=adjust, source="local_intraday_bars")
    return {"status": "ok", "code": code, "period": period, "source_chain": source_chain, **summary}


__all__ = [
    "DAILY_SCORE_VERSION",
    "INTRADAY_SCORE_VERSION",
    "IntradayReplayService",
    "TradePredictionDailyVerifier",
    "sync_minute_kline_to_storage",
]
