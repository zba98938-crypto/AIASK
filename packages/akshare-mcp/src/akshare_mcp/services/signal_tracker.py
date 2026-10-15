"""Asyncio-based signal tracker — daily forward signal generation & verification.

Runs daily at 18:30 CST (after FactorScheduler at 18:00):
- Phase A: Generate signals for all listed/incubating strategies
- Phase B: Compute forward returns for past signals (1/5/10/20 day)
- Phase C: Run lifecycle scan (auto-promote/demote strategies)

Usage:
    from .signal_tracker import get_signal_tracker
    tracker = get_signal_tracker()
    tracker.start()
"""
from __future__ import annotations

# --- signal_tracker_parts/context.py ---
import asyncio
import logging
import os
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# P2-2：前向收益采集窗口。默认 [1,5,10,20]（零变化）；可经
# STRATEGY_FACTORY_FORWARD_DAYS 覆盖（逗号分隔，如 "1,5,10,20,40" 纳入长线 40 日窗口）。
def _resolve_forward_days() -> list[int]:
    raw = os.getenv("STRATEGY_FACTORY_FORWARD_DAYS")
    if not raw:
        return [1, 5, 10, 20]
    out: list[int] = []
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            val = int(tok)
        except ValueError as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:42: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        if val > 0 and val not in out:
            out.append(val)
    return out or [1, 5, 10, 20]


FORWARD_DAYS = _resolve_forward_days()
FORWARD_RETURN_BATCH_LIMIT = 2000
FORWARD_RETURN_MAX_ROUNDS = 100
RECENT_SIGNAL_EVENT_LIMIT = 8


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _forward_return_provider_refresh_enabled() -> bool:
    return _env_bool("STRATEGY_FACTORY_FORWARD_RETURN_PROVIDER_REFRESH_ENABLED", default=True)


def _forward_return_provider_refresh_limit() -> int:
    raw = os.getenv("STRATEGY_FACTORY_FORWARD_RETURN_PROVIDER_LIMIT", "260")
    try:
        value = int(str(raw).strip())
    except Exception:
        value = 260
    return max(60, min(value, 5000))


def _signal_series_from_events(length: int, events: list[dict[str, Any]] | None) -> np.ndarray:
    signals = np.zeros(max(0, int(length)), dtype=np.int8)
    for event in list(events or []):
        idx = int(event.get("index") or 0)
        signal = int(event.get("signal") or 0)
        if 0 <= idx < len(signals) and signal != 0:
            signals[idx] = 1 if signal > 0 else -1
    return signals


def _signal_series_from_masks(entry_mask: np.ndarray, exit_mask: np.ndarray) -> np.ndarray:
    entry = np.asarray(entry_mask, dtype=bool)
    exit_ = np.asarray(exit_mask, dtype=bool)
    size = max(len(entry), len(exit_))
    signals = np.zeros(size, dtype=np.int8)
    if len(entry):
        signals[: len(entry)][entry] = 1
    if len(exit_):
        exit_slice = signals[: len(exit_)]
        exit_slice[np.asarray(exit_, dtype=bool) & (exit_slice == 0)] = -1
    return signals


def _coerce_event_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).date()
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:111: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass
    try:
        return date.fromisoformat(text[:10])
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:115: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:124: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


def _default_event_action(signal: int) -> str:
    return "enter" if int(signal or 0) > 0 else ("exit" if int(signal or 0) < 0 else "hold")


def _classify_action_source(
    *,
    execution_semantic_mode: str,
    signal: int,
    action: Optional[str],
    reason: Optional[str],
) -> str:
    normalized_mode = str(execution_semantic_mode or "").strip().lower()
    normalized_action = str(action or "").strip().lower()
    normalized_reason = str(reason or "").strip().lower()
    if normalized_mode != "compiled_dsl":
        return "builtin_legacy_signal"
    if normalized_action == "enter" or int(signal or 0) > 0:
        return "dsl_entry"
    if normalized_action == "reduce":
        return "runtime_playbook_reduce"
    if normalized_action in {"freeze_reentry", "stop"}:
        return "runtime_playbook_stop"
    runtime_stop_tokens = (
        "stop",
        "take_profit",
        "trailing",
        "time_stop",
        "freeze",
        "band",
    )
    if any(token in normalized_reason for token in runtime_stop_tokens):
        return "runtime_playbook_stop"
    return "dsl_exit"


def _normalize_signal_events(
    klines: list[dict[str, Any]],
    events: list[dict[str, Any]] | None,
    *,
    execution_semantic_mode: str,
) -> list[dict[str, Any]]:
    ordered = list(klines or [])
    normalized: list[dict[str, Any]] = []
    max_length = len(ordered)
    for sequence, raw_event in enumerate(list(events or [])):
        try:
            index = int(raw_event.get("index") or 0)
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:175: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
        if index < 0 or index >= max_length:
            continue
        raw_signal = int(raw_event.get("signal") or 0)
        signal = 1 if raw_signal > 0 else (-1 if raw_signal < 0 else 0)
        action = str(raw_event.get("action") or _default_event_action(signal)).strip().lower() or _default_event_action(signal)
        reason = str(raw_event.get("reason") or "").strip() or None
        units = _coerce_optional_float(raw_event.get("units"))
        remaining_units = _coerce_optional_float(raw_event.get("remaining_units"))
        bar = ordered[index] if 0 <= index < len(ordered) else {}
        event_date = _coerce_event_date(bar.get("date") or bar.get("time"))
        action_source = _classify_action_source(
            execution_semantic_mode=execution_semantic_mode,
            signal=signal,
            action=action,
            reason=reason,
        )
        normalized.append(
            {
                "sequence": sequence,
                "index": index,
                "date": event_date.isoformat() if event_date else None,
                "signal": signal,
                "action": action,
                "action_source": action_source,
                "reason": reason,
                "units": units,
                "remaining_units": remaining_units,
            }
        )
    return normalized


def _synthesize_events_from_masks(
    klines: list[dict[str, Any]],
    entry_mask: np.ndarray,
    exit_mask: np.ndarray,
    *,
    execution_semantic_mode: str,
) -> list[dict[str, Any]]:
    entry = np.asarray(entry_mask, dtype=bool)
    exit_ = np.asarray(exit_mask, dtype=bool)
    size = max(len(entry), len(exit_))
    events: list[dict[str, Any]] = []
    for index in range(size):
        if index < len(entry) and bool(entry[index]):
            events.append(
                {
                    "index": index,
                    "signal": 1,
                    "action": "enter",
                    "reason": "legacy_entry_mask",
                }
            )
        if index < len(exit_) and bool(exit_[index]):
            events.append(
                {
                    "index": index,
                    "signal": -1,
                    "action": "exit",
                    "reason": "legacy_exit_mask",
                }
            )
    return _normalize_signal_events(
        klines,
        events,
        execution_semantic_mode=execution_semantic_mode,
    )


def _synthesize_events_from_signal_series(
    klines: list[dict[str, Any]],
    signal_series: np.ndarray,
    *,
    execution_semantic_mode: str,
) -> list[dict[str, Any]]:
    signals = np.asarray(signal_series, dtype=np.int8)
    events: list[dict[str, Any]] = []
    last_nonzero_signal = 0
    for index, raw_signal in enumerate(signals):
        signal = 1 if int(raw_signal) > 0 else (-1 if int(raw_signal) < 0 else 0)
        if signal == 0 or signal == last_nonzero_signal:
            continue
        events.append(
            {
                "index": index,
                "signal": signal,
                "action": _default_event_action(signal),
                "reason": "legacy_signal_series",
            }
        )
        last_nonzero_signal = signal
    return _normalize_signal_events(
        klines,
        events,
        execution_semantic_mode=execution_semantic_mode,
    )


def _build_signal_tracking_artifacts(
    instance: Any,
    klines: list[dict[str, Any]],
    *,
    execution_semantic_mode: str,
) -> dict[str, Any]:
    ordered = list(klines or [])
    if not ordered:
        return {
            "signal_series": np.zeros(0, dtype=np.int8),
            "events": [],
            "latest_bar_signal": 0,
            "latest_bar_date": None,
            "signal_row": None,
            "snapshot": None,
        }

    signal_series: Optional[np.ndarray] = None
    normalized_events: list[dict[str, Any]] = []
    if hasattr(instance, "generate_signal_events_from_klines"):
        events = instance.generate_signal_events_from_klines(ordered)
        if events is not None:
            normalized_events = _normalize_signal_events(
                ordered,
                events,
                execution_semantic_mode=execution_semantic_mode,
            )
            signal_series = _signal_series_from_events(len(ordered), normalized_events)
    if signal_series is None and hasattr(instance, "generate_entry_exit_masks_from_klines"):
        entry_mask, exit_mask = instance.generate_entry_exit_masks_from_klines(ordered)
        signal_series = _signal_series_from_masks(entry_mask, exit_mask)
        normalized_events = _synthesize_events_from_masks(
            ordered,
            entry_mask,
            exit_mask,
            execution_semantic_mode=execution_semantic_mode,
        )
    if signal_series is None:
        closes = np.array([float(k.get("close", 0) or 0.0) for k in ordered], dtype=float)
        volumes = np.array([float(k.get("volume", 0) or 0.0) for k in ordered], dtype=float)
        try:
            signal_series = np.asarray(instance.generate_signals(closes, volumes), dtype=np.int8)
        except TypeError:
            signal_series = np.asarray(instance.generate_signals(closes), dtype=np.int8)
        normalized_events = _synthesize_events_from_signal_series(
            ordered,
            signal_series,
            execution_semantic_mode=execution_semantic_mode,
        )

    latest_bar_signal = int(signal_series[-1]) if len(signal_series) > 0 else 0
    latest_bar_date = _coerce_event_date((ordered[-1] or {}).get("date") or (ordered[-1] or {}).get("time"))
    latest_bar_index = len(signal_series) - 1
    latest_bar_event = next(
        (event for event in reversed(normalized_events) if int(event.get("index") or -1) == latest_bar_index),
        None,
    )
    latest_event = normalized_events[-1] if normalized_events else None
    latest_entry = next((event for event in reversed(normalized_events) if str(event.get("action") or "") == "enter"), None)
    latest_exit = next((event for event in reversed(normalized_events) if str(event.get("action") or "") == "exit"), None)
    nonzero_indexes = np.flatnonzero(np.asarray(signal_series, dtype=np.int8) != 0)
    latest_nonzero_index = int(nonzero_indexes[-1]) if len(nonzero_indexes) > 0 else None
    latest_nonzero_date = None
    latest_nonzero_signal = None
    if latest_nonzero_index is not None and 0 <= latest_nonzero_index < len(ordered):
        latest_nonzero_date = _coerce_event_date(
            (ordered[latest_nonzero_index] or {}).get("date") or (ordered[latest_nonzero_index] or {}).get("time")
        )
        latest_nonzero_signal = int(signal_series[latest_nonzero_index])

    default_action_source = _classify_action_source(
        execution_semantic_mode=execution_semantic_mode,
        signal=latest_bar_signal,
        action=_default_event_action(latest_bar_signal) if latest_bar_signal != 0 else None,
        reason=None,
    ) if latest_bar_signal != 0 else None
    signal_row = None
    if latest_bar_signal != 0:
        signal_row = {
            "signal": latest_bar_signal,
            "score": float(latest_bar_signal),
            "execution_semantic_mode": execution_semantic_mode,
            "action_source": (
                str(latest_bar_event.get("action_source") or "").strip() or default_action_source
                if latest_bar_event
                else default_action_source
            ),
            "event_action": (
                str(latest_bar_event.get("action") or "").strip() or _default_event_action(latest_bar_signal)
                if latest_bar_event
                else _default_event_action(latest_bar_signal)
            ),
            "action_reason": (
                str(latest_bar_event.get("reason") or "").strip() or None
                if latest_bar_event
                else None
            ),
            "signal_metadata": {
                "latest_bar_date": latest_bar_date.isoformat() if latest_bar_date else None,
                "latest_event_index": latest_event.get("index") if latest_event else None,
                "latest_event_date": latest_event.get("date") if latest_event else None,
                "latest_nonzero_signal_index": latest_nonzero_index,
                "latest_nonzero_signal_date": latest_nonzero_date.isoformat() if latest_nonzero_date else None,
                "latest_nonzero_signal": latest_nonzero_signal,
                "event_count": len(normalized_events),
                "latest_bar_has_event": latest_bar_event is not None,
                "execution_semantic_mode": execution_semantic_mode,
                "action_source": (
                    str(latest_bar_event.get("action_source") or "").strip() or default_action_source
                    if latest_bar_event
                    else default_action_source
                ),
                "event_action": (
                    str(latest_bar_event.get("action") or "").strip() or _default_event_action(latest_bar_signal)
                    if latest_bar_event
                    else _default_event_action(latest_bar_signal)
                ),
                "action_reason": (
                    str(latest_bar_event.get("reason") or "").strip() or None
                    if latest_bar_event
                    else None
                ),
            },
        }

    recent_events = []
    for event in normalized_events[-RECENT_SIGNAL_EVENT_LIMIT:]:
        recent_events.append(
            {
                "index": int(event.get("index") or 0),
                "date": event.get("date"),
                "signal": int(event.get("signal") or 0),
                "action": event.get("action"),
                "action_source": event.get("action_source"),
                "reason": event.get("reason"),
                "units": event.get("units"),
                "remaining_units": event.get("remaining_units"),
            }
        )

    snapshot = {
        "latest_bar_date": latest_bar_date,
        "latest_bar_signal": latest_bar_signal,
        "execution_semantic_mode": execution_semantic_mode,
        "latest_event_index": latest_event.get("index") if latest_event else None,
        "latest_event_date": latest_event.get("date") if latest_event else None,
        "latest_event_signal": latest_event.get("signal") if latest_event else None,
        "latest_event_action": latest_event.get("action") if latest_event else None,
        "latest_event_action_source": latest_event.get("action_source") if latest_event else None,
        "latest_event_reason": latest_event.get("reason") if latest_event else None,
        "latest_event_units": latest_event.get("units") if latest_event else None,
        "latest_entry_date": latest_entry.get("date") if latest_entry else None,
        "latest_exit_date": latest_exit.get("date") if latest_exit else None,
        "event_count": len(normalized_events),
        "recent_events": recent_events,
        "metadata": {
            "latest_bar_has_event": latest_bar_event is not None,
            "recent_event_window": RECENT_SIGNAL_EVENT_LIMIT,
            "latest_nonzero_signal_index": latest_nonzero_index,
            "latest_nonzero_signal_date": latest_nonzero_date.isoformat() if latest_nonzero_date else None,
            "latest_nonzero_signal": latest_nonzero_signal,
        },
    }
    return {
        "signal_series": signal_series,
        "events": normalized_events,
        "latest_bar_signal": latest_bar_signal,
        "latest_bar_date": latest_bar_date,
        "signal_row": signal_row,
        "snapshot": snapshot,
    }

# --- signal_tracker_parts/specs.py ---
import asyncio
import logging
import os
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Constants
FORWARD_RETURN_BATCH_LIMIT = 2000
FORWARD_RETURN_MAX_ROUNDS = 100


class SignalTracker:
    """Asyncio-based daily signal tracking scheduler."""

    def __init__(self, run_time: time = time(18, 30)):
        self.run_time = run_time
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_run: Optional[datetime] = None
        self.last_result: Optional[dict] = None

    def start(self):
        if self._running:
            logger.warning("SignalTracker already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="signal-tracker")
        logger.info("SignalTracker started, daily run at %s", self.run_time)

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("SignalTracker stopped")

    async def shutdown(self, grace_sec: float = 5.0):
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            logger.info("SignalTracker stopped")
            return
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, grace_sec))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        else:
            with suppress(asyncio.CancelledError):
                await task
        logger.info("SignalTracker stopped")

    async def _loop(self):
        while self._running:
            try:
                now = datetime.now()
                target = datetime.combine(now.date(), self.run_time)
                if target <= now:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.info("SignalTracker: next run in %.0f seconds at %s", wait_seconds, target)
                await asyncio.sleep(wait_seconds)

                if self._running:
                    await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("SignalTracker loop error: %s", e, exc_info=True)
                await asyncio.sleep(60)

    @staticmethod
    def _merge_unique_strategies(*groups: list[dict]) -> list[dict]:
        merged: list[dict] = []
        seen: set[str] = set()
        for group in groups:
            for strategy in list(group or []):
                strategy_id = str((strategy or {}).get("id") or "").strip()
                if not strategy_id or strategy_id in seen:
                    continue
                seen.add(strategy_id)
                merged.append(strategy)
        return merged

    async def _load_runtime_submitted_strategies(self, db, *, limit: int = 200) -> list[dict]:
        if not hasattr(db, "list_strategies"):
            return []
        rows = await db.list_strategies("submitted", limit=limit)
        if not rows:
            return []
        get_quality_report = getattr(db, "get_strategy_quality_report", None)
        eligible: list[dict] = []
        for row in list(rows or []):
            if await self._is_runtime_submitted_strategy(
                db,
                row,
                get_quality_report=get_quality_report,
            ):
                eligible.append(row)
        return eligible

    async def _is_runtime_submitted_strategy(self, db, strategy: dict, *, get_quality_report=None) -> bool:
        strategy_id = str((strategy or {}).get("id") or "").strip()
        if not strategy_id:
            return False
        report = None
        if callable(get_quality_report):
            try:
                report = await get_quality_report(strategy_id, "submission")
            except Exception:
                report = None
        summary = dict((report or {}).get("summary") or {})
        lane = str(
            summary.get("submission_lane")
            or (report or {}).get("submission_lane")
            or ""
        ).strip().lower()
        if lane in {"observe_incubation", "live_ready_review"}:
            return True
        for field_name in (
            "paper_lane_ready",
            "live_review_ready",
            "paper_account_id",
            "live_review_account_id",
        ):
            if summary.get(field_name) or (report or {}).get(field_name):
                return True
        params = dict((strategy or {}).get("params") or {})
        incubation_budget = dict(params.get("incubation_budget") or {})
        return str(incubation_budget.get("track") or "").strip().lower() in {
            "observe_incubation",
            "live_ready_review",
        }

    async def _load_runtime_observation_strategies(self, db, *, limit: int = 200) -> list[dict]:
        """Load paper/observe strategies for signal generation and paper execution."""
        candidates: list[dict] = []
        for method_name in (
            "list_active_paper_observation_strategies",
            "list_paper_observation_strategies",
        ):
            method = getattr(db, method_name, None)
            if not callable(method):
                continue
            try:
                rows = await method(limit=limit)
            except TypeError:
                try:
                    rows = await method()
                except Exception as exc:
                    logger.warning("SignalTracker: %s failed: %s", method_name, exc)
                    continue
            except Exception as exc:
                logger.warning("SignalTracker: %s failed: %s", method_name, exc)
                continue
            candidates.extend(list(rows or []))
        return self._merge_unique_strategies(candidates)

    @staticmethod
    def _phase_timeout_seconds(phase_name: str) -> float:
        specific = f"STRATEGY_SIGNAL_TRACKER_PHASE_{phase_name.upper()}_TIMEOUT_SEC"
        raw = os.getenv(specific) or os.getenv("STRATEGY_SIGNAL_TRACKER_PHASE_TIMEOUT_SEC") or "45"
        try:
            value = float(str(raw).strip())
        except Exception:
            value = 45.0
        return max(5.0, min(value, 300.0))

    @staticmethod
    def _resolve_strategy_universe(strategy: dict, default_universe: list[str]) -> list[str]:
        payload = dict(strategy or {})
        params = dict(payload.get("params") or {})
        ordered: list[str] = []
        seen: set[str] = set()
        filter_payloads: list[dict[str, Any]] = []

        def _push(value: Any) -> None:
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    _push(item)
                return
            if isinstance(value, dict):
                for key in ("symbols", "target_symbols", "symbol", "stock_code", "code"):
                    if key in value:
                        _push(value.get(key))
                return
            text = str(value or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            ordered.append(text)

        def _collect_codes(value: Any) -> list[str]:
            codes: list[str] = []
            code_seen: set[str] = set()

            def _visit(item: Any) -> None:
                if isinstance(item, (list, tuple, set)):
                    for entry in item:
                        _visit(entry)
                    return
                if isinstance(item, dict):
                    for key in (
                        "prioritized_symbols",
                        "preferred_symbols",
                        "universe_priority_symbols",
                        "excluded_symbols",
                        "symbols",
                        "target_symbols",
                        "symbol",
                        "stock_code",
                        "code",
                    ):
                        if key in item:
                            _visit(item.get(key))
                    return
                text = str(item or "").strip()
                if not text or text in code_seen:
                    return
                code_seen.add(text)
                codes.append(text)

            _visit(value)
            return codes

        for candidate in (
            payload.get("target_symbols"),
            payload.get("stock_pool"),
            payload.get("research_task"),
            params.get("target_symbols"),
            params.get("stock_pool"),
            params.get("research_task"),
            dict(params.get("dsl") or {}).get("metadata"),
        ):
            _push(candidate)
        for candidate in (
            payload.get("stock_pool"),
            payload.get("research_task"),
            params.get("stock_pool"),
            params.get("research_task"),
            dict(params.get("dsl") or {}).get("metadata"),
        ):
            if isinstance(candidate, dict):
                filters = dict(candidate.get("filters") or {})
                if filters:
                    filter_payloads.append(filters)

        prioritized_symbols = _collect_codes(
            [
                params.get("prioritized_symbols"),
                params.get("preferred_symbols"),
                params.get("universe_priority_symbols"),
                [payload_.get("prioritized_symbols") for payload_ in filter_payloads],
                [payload_.get("preferred_symbols") for payload_ in filter_payloads],
                [payload_.get("universe_priority_symbols") for payload_ in filter_payloads],
            ]
        )
        excluded_symbols = set(
            _collect_codes(
                [
                    params.get("excluded_symbols"),
                    [payload_.get("excluded_symbols") for payload_ in filter_payloads],
                ]
            )
        )

        budget_candidates = [
            params.get("max_active_symbols"),
            *[payload_.get("max_active_symbols") for payload_ in filter_payloads],
        ]
        max_active_symbols = 0
        for raw_budget in budget_candidates:
            try:
                max_active_symbols = max(max_active_symbols, int(raw_budget or 0))
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:728: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue

        resolved = [code for code in ordered if code not in excluded_symbols]
        if prioritized_symbols:
            priority_order = [code for code in prioritized_symbols if code in resolved]
            remaining = [code for code in resolved if code not in set(priority_order)]
            resolved = [*priority_order, *remaining]
        if max_active_symbols > 0:
            resolved = resolved[:max_active_symbols]
        return resolved or list(default_universe or [])

    @staticmethod
    def _get_default_universe() -> list[str]:
        from .factor_scheduler import DEFAULT_UNIVERSE

        return list(DEFAULT_UNIVERSE)

    @staticmethod
    def _build_signal_tracking_artifacts(instance: Any, klines: list[dict[str, Any]], *, execution_semantic_mode: str) -> dict[str, Any]:
        return _build_signal_tracking_artifacts(
            instance,
            klines,
            execution_semantic_mode=execution_semantic_mode,
        )

    @staticmethod
    async def _load_executable_strategies_with_fallback(
        db,
        *,
        limit: int = 500,
        use_contract: bool = True,
    ) -> list[dict]:
        from .execution_universe_adapter import load_executable_strategies_with_fallback

        return await load_executable_strategies_with_fallback(
            db,
            limit=limit,
            use_contract=use_contract,
        )

    @staticmethod
    def _get_runtime_control_service():
        from .runtime_control import get_strategy_runtime_control_service

        return get_strategy_runtime_control_service()

    @staticmethod
    def _get_runtime_risk_service():
        from .runtime_risk import get_strategy_runtime_risk_service

        return get_strategy_runtime_risk_service()

    @staticmethod
    async def _run_lifecycle_scan(db, strategies=None):
        from ..tools.managers.strategy_manager import _lifecycle_scan

        # strategies is accepted for provider compatibility; scan is DB-driven.
        return await _lifecycle_scan(db)

    @staticmethod
    def _get_vector_governance_service():
        from .vector_governance import get_strategy_vector_governance_service

        return get_strategy_vector_governance_service()

    @staticmethod
    def _get_domain_projection_service():
        from .domain_projection import get_strategy_domain_projection_service

        return get_strategy_domain_projection_service()


    async def _generate_signals(self, db, strategy: dict, *, as_of=None) -> dict:
        """Generate signals for one strategy via free-data rules + incubation sync.

        1) Normalize strategy params (JSON string -> dict)
        2) If no DB signals for as_of, compute simple free K-line signals and save
        3) Convert today's signals into paper orders via incubation service
        """
        import json as _json
        from datetime import date as _date

        payload = dict(strategy or {})
        # Normalize params/factor_weights if stored as JSON text.
        for key in ("params", "factor_weights"):
            value = payload.get(key)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    payload[key] = {}
                    continue
                try:
                    payload[key] = _json.loads(text)
                except Exception:
                    payload[key] = {}
            elif value is None:
                payload[key] = {}

        params = dict(payload.get("params") or {})
        # Ensure free-data target codes when missing.
        codes = []
        for candidate in (
            payload.get("target_symbols"),
            params.get("target_symbols"),
            params.get("codes"),
            params.get("stock_pool"),
        ):
            if isinstance(candidate, (list, tuple, set)):
                codes.extend([str(x).strip() for x in candidate if str(x).strip()])
            elif isinstance(candidate, str) and candidate.strip():
                codes.append(candidate.strip())
        codes = list(dict.fromkeys(codes))
        if not codes:
            codes = list(self._get_default_universe())[:5]
        params.setdefault("target_symbols", list(codes))
        params.setdefault("codes", list(codes))
        payload["params"] = params
        payload["target_symbols"] = list(codes)

        signal_date = as_of
        if signal_date is None:
            signal_date = _date.today()
        elif not isinstance(signal_date, _date):
            try:
                signal_date = _date.fromisoformat(str(signal_date)[:10])
            except Exception:
                signal_date = _date.today()

        errors: list[str] = []
        generated = 0
        # Create free-rule signals if none exist for today.
        try:
            existing = await db.get_signals(
                str(payload.get("id") or ""),
                start_date=signal_date,
                end_date=signal_date,
                limit=200,
            )
        except Exception as exc:
            existing = []
            errors.append(f"get_signals:{exc}")

        force_buy_seed = str(
            os.getenv("FREE_EVIDENCE_FORCE_BUY", "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if (not existing or force_buy_seed) and hasattr(db, "save_signals"):
            free_signals: list[dict] = []
            for code in codes[:8]:
                try:
                    klines = await self._get_klines_with_fallback(db, code, limit=60)
                except Exception as exc:
                    errors.append(f"kline:{code}:{exc}")
                    continue
                closes = []
                for row in list(klines or []):
                    try:
                        closes.append(float(row.get("close")))
                    except Exception as _swallowed:
                        logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:886: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                        continue
                if len(closes) < 20:
                    continue
                # Simple free MA crossover proxy: fast5 vs slow20.
                fast = sum(closes[-5:]) / 5.0
                slow = sum(closes[-20:]) / 20.0
                prev_fast = sum(closes[-6:-1]) / 5.0
                prev_slow = sum(closes[-21:-1]) / 20.0 if len(closes) >= 21 else slow
                signal = 0
                score = abs(fast - slow) / max(abs(slow), 1e-9)
                if prev_fast <= prev_slow and fast > slow:
                    signal = 1
                elif prev_fast >= prev_slow and fast < slow:
                    signal = -1
                elif fast > slow * 1.01:
                    signal = 1
                elif fast < slow * 0.99:
                    signal = -1
                if signal == 0:
                    # Force a weak directional seed so paper path can exercise
                    # on free data days without a clean crossover.
                    signal = 1 if closes[-1] >= closes[-5] else -1
                    score = max(score, 0.05)
                if force_buy_seed:
                    # Evidence-mode: prefer buys so free-data paper path is not
                    # stuck on sell_without_position when no long inventory exists.
                    signal = 1
                    score = max(score, 0.08)
                free_signals.append(
                    {
                        "code": code,
                        "signal": int(signal),
                        "score": float(min(max(score, 0.0), 1.0)),
                        "execution_semantic_mode": "paper",
                        "action_source": "free_data_ma_cross",
                        "event_action": "enter" if signal > 0 else "exit_or_short_proxy",
                        "action_reason": "free_kline_ma_proxy",
                        "signal_metadata": {
                            "fast": fast,
                            "slow": slow,
                            "close": closes[-1],
                            "source": "free_data_daily_signal",
                        },
                    }
                )
            if free_signals:
                try:
                    generated = int(
                        await db.save_signals(
                            str(payload.get("id") or ""),
                            signal_date,
                            free_signals,
                        )
                        or 0
                    )
                except Exception as exc:
                    errors.append(f"save_signals:{exc}")
                    generated = 0

        # Convert today's signals to paper orders.
        orders_created = 0
        sync_result: dict = {}
        try:
            from .incubation import get_strategy_incubation_service

            sync_result = await get_strategy_incubation_service().sync_signals_to_orders(
                db, payload, signal_date
            )
            orders_created = int(
                sync_result.get("created_count")
                or sync_result.get("orders_created")
                or 0
            )
            if not generated:
                # count existing signals for the day if we only synced
                try:
                    todays = await db.get_signals(
                        str(payload.get("id") or ""),
                        start_date=signal_date,
                        end_date=signal_date,
                        limit=200,
                    )
                    generated = len(list(todays or []))
                except Exception as _swallowed:
                    logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:970: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    pass
        except Exception as exc:
            errors.append(f"sync_signals_to_orders:{exc}")

        return {
            "signals_generated": int(generated or 0),
            "orders_created": int(orders_created or 0),
            "errors": errors,
            "signal_date": str(signal_date),
            "strategy_id": str(payload.get("id") or ""),
            "sync_result": sync_result,
        }

    async def _create_signal_event_snapshot(self, db, strategy: dict, signal_result: dict, *, as_of=None) -> dict:
        """Persist per-code signal event snapshots for today's free-data signals."""
        from datetime import date as _date

        save = getattr(db, "save_strategy_signal_event_snapshot", None)
        if not callable(save):
            return {"snapshot_created": False, "reason": "api_missing", "count": 0}

        strategy_id = str((strategy or {}).get("id") or (signal_result or {}).get("strategy_id") or "").strip()
        if not strategy_id:
            return {"snapshot_created": False, "reason": "missing_strategy_id", "count": 0}

        signal_date = as_of or (signal_result or {}).get("signal_date")
        if not isinstance(signal_date, _date):
            try:
                signal_date = _date.fromisoformat(str(signal_date)[:10])
            except Exception:
                signal_date = _date.today()

        try:
            signals = await db.get_signals(
                strategy_id,
                start_date=signal_date,
                end_date=signal_date,
                limit=200,
            )
        except Exception as exc:
            return {"snapshot_created": False, "reason": f"get_signals:{exc}", "count": 0}

        created = 0
        errors: list[str] = []
        for item in list(signals or []):
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            signal_val = int(item.get("signal") or 0)
            payload = {
                "strategy_id": strategy_id,
                "code": code,
                "as_of_date": signal_date,
                "latest_bar_date": signal_date,
                "latest_bar_signal": signal_val,
                "execution_semantic_mode": item.get("execution_semantic_mode") or "paper",
                "latest_event_index": 0,
                "latest_event_date": signal_date,
                "latest_event_signal": signal_val,
                "latest_event_action": item.get("event_action") or ("enter" if signal_val > 0 else "exit_or_short_proxy"),
                "latest_event_action_source": item.get("action_source") or "free_data_ma_cross",
                "latest_event_reason": item.get("action_reason") or "free_kline_ma_proxy",
                "latest_event_units": float(item.get("score") or 0.0),
                "latest_entry_date": signal_date if signal_val > 0 else None,
                "latest_exit_date": signal_date if signal_val < 0 else None,
                "event_count": 1,
                "recent_events": [
                    {
                        "code": code,
                        "signal": signal_val,
                        "score": item.get("score"),
                        "as_of_date": str(signal_date),
                        "action_source": item.get("action_source"),
                    }
                ],
                "metadata": {
                    "signal_metadata": item.get("signal_metadata") or {},
                    "source": "free_data_signal_event_snapshot",
                    "signals_generated": int((signal_result or {}).get("signals_generated") or 0),
                    "orders_created": int((signal_result or {}).get("orders_created") or 0),
                },
            }
            try:
                result = save(payload)
                if hasattr(result, "__await__"):
                    await result
                created += 1
            except Exception as exc:
                errors.append(f"{code}:{exc}")
        return {
            "snapshot_created": created > 0,
            "count": created,
            "errors": errors,
        }


    async def run_once(self):
        """Compat entrypoint delegating canonical orchestration to strategy-factory."""
        from strategy_factory.runtime.signal_tracker import build_signal_tracker_runtime

        runtime = build_signal_tracker_runtime(support=self, run_time=self.run_time)
        return await runtime.run_once()

    async def _backfill_forward_returns(self, *args, **kwargs):
        """Provider-compatible alias."""
        return await self.backfill_forward_returns(*args, **kwargs)

    async def backfill_forward_returns(
        self,
        db=None,
        *,
        forward_days_list: Optional[List[int]] = None,
        batch_limit: int = FORWARD_RETURN_BATCH_LIMIT,
        max_rounds: int = FORWARD_RETURN_MAX_ROUNDS,
    ) -> dict:
        """批量回填历史前向收益，支持在日常 Phase B 中复用。"""
        from ..storage import get_db

        database = db or get_db()
        windows = [int(item) for item in list(forward_days_list or FORWARD_DAYS) if int(item) > 0]
        per_window: dict[str, Any] = {}
        total_computed = 0

        for forward_days in windows:
            window_key = f"{forward_days}D"
            rounds = 0
            computed = 0
            pending_seen = 0
            cursor_signal_date: Optional[date] = None
            cursor_id = 0
            truncated = False

            while rounds < max(1, int(max_rounds or 1)):
                rounds += 1
                pending = await database.get_pending_forward_returns(
                    forward_days,
                    limit=batch_limit,
                    after_signal_date=cursor_signal_date,
                    after_id=cursor_id,
                )
                if not pending:
                    break
                pending_seen += len(pending)
                saved = await self._compute_forward_returns_batch(
                    database,
                    pending,
                    forward_days=forward_days,
                )
                computed += saved
                total_computed += saved
                last_record = dict(pending[-1] or {})
                cursor_signal_date = self._coerce_trade_date(
                    last_record.get("effective_signal_date") or last_record.get("signal_date")
                )
                cursor_id = int(last_record.get("id") or 0)
            else:
                truncated = True
                logger.warning(
                    "SignalTracker: forward-return backfill truncated for %s after %d rounds",
                    window_key,
                    rounds,
                )

            per_window[window_key] = {
                "rounds": rounds,
                "pending_seen": pending_seen,
                "computed": computed,
                "stalled": truncated,
            }

        return {
            "computed": total_computed,
            "batch_limit": max(1, int(batch_limit or FORWARD_RETURN_BATCH_LIMIT)),
            "max_rounds": max(1, int(max_rounds or FORWARD_RETURN_MAX_ROUNDS)),
            "windows": per_window,
        }

    async def _compute_forward_returns_batch(
        self,
        db,
        pending: List[dict],
        *,
        forward_days: int,
    ) -> int:
        if not pending:
            return 0

        pending_by_code: Dict[str, List[dict]] = {}
        for record in list(pending or []):
            code = str(record.get("code") or "").strip()
            if not code:
                continue
            pending_by_code.setdefault(code, []).append(record)

        rows_to_save: list[dict[str, Any]] = []
        for code, records in pending_by_code.items():
            base_dates = [self._resolve_signal_base_date(item) for item in records]
            base_dates = [item for item in base_dates if item is not None]
            if not base_dates:
                continue
            earliest_signal_date = min(base_dates)
            klines = await self._get_klines_with_fallback(
                db,
                code,
                start_date=earliest_signal_date,
                limit=None,
                allow_data_source_fallback=True,
                required_base_dates=base_dates,
                forward_days=forward_days,
            )
            close_series = self._build_close_series(klines)
            if not close_series:
                continue
            index_by_date = {trade_date: idx for idx, (trade_date, _close) in enumerate(close_series)}
            closes = [close for _trade_date, close in close_series]
            for record in records:
                signal_date = self._resolve_signal_base_date(record)
                if signal_date is None:
                    continue
                base_index = self._resolve_forward_base_index(
                    close_series,
                    signal_date,
                    index_by_date=index_by_date,
                )
                if base_index is None:
                    continue
                future_index = base_index + int(forward_days)
                if future_index >= len(closes):
                    continue
                base_close = float(closes[base_index] or 0.0)
                future_close = float(closes[future_index] or 0.0)
                if base_close <= 0:
                    continue
                rows_to_save.append(
                    {
                        "signal_id": int(record["id"]),
                        "forward_days": int(forward_days),
                        "actual_return": (future_close - base_close) / base_close,
                    }
                )

        if not rows_to_save:
            return 0

        if hasattr(db, "save_forward_returns_batch"):
            return int(await db.save_forward_returns_batch(rows_to_save) or 0)

        saved = 0
        for row in rows_to_save:
            await db.save_forward_returns(
                row["signal_id"],
                row["forward_days"],
                row["actual_return"],
            )
            saved += 1
        return saved

    @staticmethod
    def _decode_signal_metadata(record: dict[str, Any]) -> dict[str, Any]:
        raw = dict(record or {}).get("signal_metadata")
        if isinstance(raw, dict):
            return dict(raw)
        if raw in (None, "", [], {}):
            return {}
        try:
            import json

            parsed = json.loads(str(raw))
            return dict(parsed or {}) if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _resolve_signal_record_date(self, signal_row: dict[str, Any]) -> Optional[date]:
        metadata = dict((signal_row or {}).get("signal_metadata") or {})
        for key in ("latest_bar_date", "latest_nonzero_signal_date", "latest_event_date"):
            resolved = self._coerce_trade_date(metadata.get(key))
            if resolved is not None:
                return resolved
        return self._coerce_trade_date((signal_row or {}).get("signal_date"))

    def _resolve_signal_base_date(self, record: dict[str, Any]) -> Optional[date]:
        metadata = self._decode_signal_metadata(record)
        for key in ("latest_bar_date", "latest_nonzero_signal_date", "latest_event_date"):
            resolved = self._coerce_trade_date(metadata.get(key))
            if resolved is not None:
                return resolved
        return self._coerce_trade_date((record or {}).get("signal_date"))

    @staticmethod
    def _coerce_trade_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:1274: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return None

    def _build_close_series(self, klines: List[dict]) -> List[tuple[date, float]]:
        close_by_date: dict[date, float] = {}
        for kline in list(klines or []):
            trade_date = self._coerce_trade_date(kline.get("date") or kline.get("time"))
            if trade_date is None:
                continue
            try:
                close = float(kline.get("close") or 0.0)
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:1285: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue
            if close <= 0:
                continue
            close_by_date[trade_date] = close
        return sorted(close_by_date.items(), key=lambda item: item[0])

    @staticmethod
    def _resolve_forward_base_index(
        close_series: List[tuple[date, float]],
        base_date: date,
        *,
        index_by_date: Optional[dict[date, int]] = None,
    ) -> Optional[int]:
        if not close_series or base_date is None:
            return None
        index_map = index_by_date or {
            trade_date: idx for idx, (trade_date, _close) in enumerate(close_series)
        }
        exact_index = index_map.get(base_date)
        if exact_index is not None:
            return exact_index
        previous_index: Optional[int] = None
        for idx, (trade_date, _close) in enumerate(close_series):
            if trade_date > base_date:
                break
            previous_index = idx
        return previous_index

    def _has_forward_coverage(
        self,
        close_series: List[tuple[date, float]],
        *,
        required_base_dates: Optional[List[date]] = None,
        forward_days: Optional[int] = None,
    ) -> bool:
        if not close_series:
            return False
        base_dates = [item for item in list(required_base_dates or []) if item is not None]
        if not base_dates:
            return True
        horizon = max(int(forward_days or 0), 0)
        index_by_date = {trade_date: idx for idx, (trade_date, _close) in enumerate(close_series)}
        for base_date in base_dates:
            base_index = self._resolve_forward_base_index(
                close_series,
                base_date,
                index_by_date=index_by_date,
            )
            if base_index is None:
                return False
            if horizon > 0 and base_index + horizon >= len(close_series):
                return False
        return True

    @staticmethod
    def _normalize_provider_klines(code: str, rows: list[dict]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in list(rows or []):
            if not isinstance(row, dict):
                continue
            payload = dict(row)
            payload.setdefault("code", code)
            if not payload.get("date") and payload.get("time"):
                payload["date"] = str(payload.get("time"))[:10]
            if not payload.get("date") or payload.get("close") in (None, ""):
                continue
            normalized.append(payload)
        return normalized

    async def _fetch_provider_klines(self, code: str, *, limit: int) -> list[dict[str, Any]]:
        providers: list[tuple[str, Any]] = []
        try:
            from akshare_mcp.services.data_sync import data_sync_service

            providers.append(
                (
                    "data_sync_service",
                    lambda: data_sync_service.get_kline_with_cache(
                        code,
                        "daily",
                        "",
                        "",
                        limit,
                        use_cache=True,
                    ),
                )
            )
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:1373: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

        for provider_name, loader in providers:
            try:
                payload = await loader()
            except Exception as exc:
                logger.warning(
                    "SignalTracker: provider kline refresh failed for %s via %s: %s",
                    code,
                    provider_name,
                    exc,
                )
                continue
            if isinstance(payload, dict):
                rows = payload.get("data") or payload.get("rows") or []
            else:
                rows = payload
            normalized = self._normalize_provider_klines(code, list(rows or []))
            if normalized:
                return normalized
        return []

    async def _get_klines_with_fallback(
        self,
        db,
        code: str,
        limit: Optional[int] = 200,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        allow_data_source_fallback: bool = False,
        required_base_dates: Optional[List[date]] = None,
        forward_days: Optional[int] = None,
    ) -> list:
        """从 DB 获取 K 线；仅在前向收益补证据时按开关刷新 provider 数据。"""
        kwargs: dict[str, Any] = {}
        if start_date is not None:
            kwargs["start_date"] = start_date.isoformat()
        if end_date is not None:
            kwargs["end_date"] = end_date.isoformat()
        if limit is not None:
            kwargs["limit"] = limit

        klines = await db.get_klines(code, **kwargs)
        minimum_bars = 1 if start_date is not None or end_date is not None else 20
        close_series = self._build_close_series(list(klines or []))
        if (
            klines
            and len(klines) >= minimum_bars
            and self._has_forward_coverage(
                close_series,
                required_base_dates=required_base_dates,
                forward_days=forward_days,
            )
        ):
            return klines
        if not allow_data_source_fallback or not _forward_return_provider_refresh_enabled():
            return klines or []

        provider_limit = _forward_return_provider_refresh_limit()
        if start_date is not None:
            provider_limit = max(provider_limit, min(5000, (date.today() - start_date).days * 2 + 60))
        provider_rows = self._normalize_provider_klines(
            code,
            await self._fetch_provider_klines(code, limit=provider_limit),
        )
        if provider_rows and hasattr(db, "save_klines"):
            try:
                await db.save_klines(code, provider_rows)
            except Exception as exc:
                logger.warning(
                    "SignalTracker: provider kline save failed for %s: %s",
                    code,
                    exc,
                )
        merged = [*list(klines or []), *provider_rows]
        return merged or list(klines or [])

    def status(self) -> dict:
        return {
            "running": self._running,
            "run_time": str(self.run_time),
            "last_run": str(self.last_run) if self.last_run else None,
            "last_result": self.last_result,
        }

# --- signal_tracker_parts/runtime.py ---
_tracker: Optional[SignalTracker] = None


def get_signal_tracker() -> SignalTracker:
    global _tracker
    if _tracker is None:
        _tracker = SignalTracker()
    return _tracker

