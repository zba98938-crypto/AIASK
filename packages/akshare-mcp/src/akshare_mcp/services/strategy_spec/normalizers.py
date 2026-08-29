"""Normalization helpers for strategy-spec generation."""

from __future__ import annotations

import math
from typing import Any, Optional

from strategy_factory.api.semantic_contract import (
    build_market_fact_gate_audit as _build_market_fact_gate_audit,
    normalize_market_evidence_facts,
)
from strategy_factory.api.semantic_contract import (
    build_research_validation_contract,
    normalize_field_provenance_token,
    normalize_prediction_trace_id,
)

from .constants import _EMPTY_VALUES

def _normalize_code_list(*values: Any, limit: int = 12) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key in ("code", "symbol", "stock_code"):
                if value.get(key) is not None:
                    visit(value.get(key))
            for key in ("codes", "symbols", "stock_codes", "target_symbols"):
                if value.get(key) is not None:
                    visit(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        raw = str(value or "").strip()
        if not raw:
            return
        if any(sep in raw for sep in [",", ";", "|", "\n", "\t", " "]):
            normalized = (
                raw.replace(";", ",")
                .replace("|", ",")
                .replace("\n", ",")
                .replace("\t", ",")
                .replace(" ", ",")
            )
            for part in normalized.split(","):
                visit(part)
            return
        code = raw.split(".")[0].strip()
        if not code or code in seen:
            return
        seen.add(code)
        codes.append(code)

    for value in values:
        visit(value)
    return codes[: max(1, min(int(limit or 12), 40))]


def _safe_normalize_research_task(task: Any) -> dict[str, Any]:
    payload = dict(task or {})
    if not payload:
        return {}
    try:
        from strategy_factory.api.semantic_contract import normalize_research_task_contract

        return dict(normalize_research_task_contract(payload))
    except Exception:
        task_source = str(payload.get("task_source") or "snapshot").strip().lower() or "snapshot"
        target_symbols = _normalize_code_list(
            [
                payload.get("target_symbols"),
                payload.get("stock_pool"),
                (payload.get("event_context") or {}).get("target_symbols"),
            ],
            limit=12,
        )
        stock_pool = dict(payload.get("stock_pool") or {})
        if target_symbols and not stock_pool:
            stock_pool = {"selection_mode": "explicit", "symbols": list(target_symbols)}
        holding_window = dict(payload.get("holding_window") or {})
        if not holding_window:
            holding_window = {"max_days": 10 if task_source == "event_driven" else 20}
        return {
            **payload,
            "task_source": task_source,
            "target_symbols": list(target_symbols),
            "stock_pool": stock_pool,
            "target_symbol_policy": str(
                payload.get("target_symbol_policy")
                or ("strict_intersection" if task_source == "event_driven" else "prefer_intersection")
            ).strip().lower(),
            "universe_expansion_policy": str(
                payload.get("universe_expansion_policy")
                or ("forbid" if task_source == "event_driven" else "allow_market_fallback")
            ).strip().lower(),
            "validation_focus": str(
                payload.get("validation_focus")
                or ("candidate_target_only" if task_source == "event_driven" else "target_plus_representative")
            ).strip().lower(),
            "holding_window": holding_window,
        }


def _task_source(research_task: dict[str, Any], event_context: dict[str, Any]) -> str:
    source = str(research_task.get("task_source") or "").strip().lower()
    if source:
        return source
    return "event_driven" if event_context else "snapshot"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        fallback = float(default)
    except Exception:
        fallback = 0.0
    if not math.isfinite(fallback):
        fallback = 0.0
    try:
        parsed = float(value)
    except Exception:
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        fallback = int(default)
    except Exception:
        fallback = 0
    try:
        parsed = float(value)
    except Exception:
        return fallback
    if not math.isfinite(parsed):
        return fallback
    return int(parsed)


def _normalize_turnover_band(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"very_high", "high", "medium", "low"}:
        return token
    return ""


def _clip_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    resolved = _safe_float(value, default)
    return max(float(minimum), min(float(maximum), resolved))


def _resolve_profile_metric(
    *,
    profile: Optional[dict[str, Any]],
    summary: Optional[dict[str, Any]],
    measured_keys: tuple[str, ...],
    legacy_keys: tuple[str, ...],
    minimum: float,
    maximum: float,
    default: float,
) -> tuple[float, str]:
    for source_label, source in (("profile", dict(profile or {})), ("summary", dict(summary or {}))):
        for key in (*measured_keys, *legacy_keys):
            value = source.get(key)
            if value not in _EMPTY_VALUES:
                return round(_clip_float(value, minimum, maximum, default), 4), f"{source_label}:{key}"
    return round(float(default), 4), "default"


def _instrument_profile_metric(
    profile: Optional[dict[str, Any]],
    *keys: str,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    payload = dict(profile or {})
    for key in keys:
        value = payload.get(key)
        if value not in _EMPTY_VALUES:
            resolved = _safe_float(value, default)
            if minimum is not None:
                resolved = max(float(minimum), resolved)
            if maximum is not None:
                resolved = min(float(maximum), resolved)
            return float(resolved)
    resolved = float(default)
    if minimum is not None:
        resolved = max(float(minimum), resolved)
    if maximum is not None:
        resolved = min(float(maximum), resolved)
    return resolved


def _primary_target_code(target_symbols: list[str]) -> str:
    return str((list(target_symbols or []) or [""])[0] or "").strip()


def _normalize_board_bucket(value: Any, *, code: str = "") -> str:
    token = str(value or "").strip().lower()
    aliases = {
        "futures": "futures",
        "commodity_futures": "futures",
        "star": "star",
        "star_market": "star",
        "科创板": "star",
        "chinext": "chinext",
        "创业板": "chinext",
        "growth_enterprise_market": "chinext",
        "beijing": "beijing",
        "bj": "beijing",
        "北交所": "beijing",
        "main_board": "main_board",
        "main": "main_board",
        "主板": "main_board",
    }
    if token in aliases:
        return aliases[token]
    normalized_code = str(code or "").split(".")[0].strip()
    if normalized_code.isalpha():
        return "futures"
    if normalized_code.startswith("688"):
        return "star"
    if normalized_code.startswith(("300", "301")):
        return "chinext"
    if normalized_code.startswith(("8", "4", "92")):
        return "beijing"
    if normalized_code:
        return "main_board"
    return "unknown"


def _default_instrument_profile(
    *,
    target_symbols: list[str],
    source_symbol_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    code = _primary_target_code(target_symbols)
    summary = dict(source_symbol_summary or {})
    board_bucket = _normalize_board_bucket(
        summary.get("board_bucket") or summary.get("market"),
        code=code,
    )
    defaults_by_board = {
        "star": {
            "annual_volatility": 0.47,
            "atr14_pct": 0.044,
            "gap_p95": 0.051,
            "trend_efficiency_60d": 0.24,
            "turnover_median": 1.8,
        },
        "chinext": {
            "annual_volatility": 0.42,
            "atr14_pct": 0.039,
            "gap_p95": 0.045,
            "trend_efficiency_60d": 0.26,
            "turnover_median": 1.6,
        },
        "beijing": {
            "annual_volatility": 0.55,
            "atr14_pct": 0.060,
            "gap_p95": 0.070,
            "trend_efficiency_60d": 0.18,
            "turnover_median": 2.2,
        },
        "main_board": {
            "annual_volatility": 0.30,
            "atr14_pct": 0.026,
            "gap_p95": 0.028,
            "trend_efficiency_60d": 0.33,
            "turnover_median": 1.1,
        },
    }
    defaults = dict(defaults_by_board.get(board_bucket) or {
        "annual_volatility": 0.34,
        "atr14_pct": 0.030,
        "gap_p95": 0.032,
        "trend_efficiency_60d": 0.30,
        "turnover_median": 1.2,
    })
    market_cap = _safe_float(summary.get("market_cap"), 0.0)
    if market_cap >= 150_000_000_000.0:
        defaults["annual_volatility"] = max(0.18, defaults["annual_volatility"] - 0.06)
        defaults["atr14_pct"] = max(0.018, defaults["atr14_pct"] - 0.006)
        defaults["gap_p95"] = max(0.018, defaults["gap_p95"] - 0.006)
        defaults["trend_efficiency_60d"] = min(0.55, defaults["trend_efficiency_60d"] + 0.05)
        defaults["turnover_median"] = max(0.7, defaults["turnover_median"] - 0.15)
    elif 0 < market_cap <= 15_000_000_000.0:
        defaults["annual_volatility"] = min(0.62, defaults["annual_volatility"] + 0.05)
        defaults["atr14_pct"] = min(0.080, defaults["atr14_pct"] + 0.008)
        defaults["gap_p95"] = min(0.090, defaults["gap_p95"] + 0.008)
        defaults["trend_efficiency_60d"] = max(0.14, defaults["trend_efficiency_60d"] - 0.04)
        defaults["turnover_median"] = min(3.0, defaults["turnover_median"] + 0.2)
    intraday_range_p90 = round(max(defaults["atr14_pct"] * 1.55, defaults["gap_p95"] * 1.25), 4)
    volume_ratio_p80 = round(max(1.02, min(1.20, 1.0 + defaults["atr14_pct"] * 2.0)), 4)
    volume_ratio_p90 = round(max(volume_ratio_p80 + 0.04, min(1.45, volume_ratio_p80 + 0.12)), 4)
    turnover_rate_p80 = round(max(0.9, defaults["turnover_median"] * 0.95), 4)
    turnover_rate_p90 = round(max(turnover_rate_p80 + 0.15, min(6.0, defaults["turnover_median"] * 1.2)), 4)
    return {
        "annual_volatility_realized_252d": round(defaults["annual_volatility"], 4),
        "annual_volatility": round(defaults["annual_volatility"], 4),
        "atr14_pct_realized": round(defaults["atr14_pct"], 4),
        "atr14_pct": round(defaults["atr14_pct"], 4),
        "gap_p95_realized": round(defaults["gap_p95"], 4),
        "gap_p95": round(defaults["gap_p95"], 4),
        "intraday_range_p90": intraday_range_p90,
        "trend_efficiency_60d_realized": round(defaults["trend_efficiency_60d"], 4),
        "trend_efficiency_60d": round(defaults["trend_efficiency_60d"], 4),
        "turnover_median": round(defaults["turnover_median"], 4),
        "volume_ratio_p80": volume_ratio_p80,
        "volume_ratio_p90": volume_ratio_p90,
        "turnover_rate_p80": turnover_rate_p80,
        "turnover_rate_p90": turnover_rate_p90,
        "measurement_source": "default_board_profile",
        "measurement_sources": {
            "annual_volatility_realized_252d": "default",
            "atr14_pct_realized": "default",
            "gap_p95_realized": "default",
            "intraday_range_p90": "default",
            "trend_efficiency_60d_realized": "default",
            "volume_ratio_p80": "default",
            "volume_ratio_p90": "default",
            "turnover_rate_p80": "default",
            "turnover_rate_p90": "default",
        },
        "measured_profile_complete": False,
        "board_bucket": board_bucket,
        "market_cap": round(market_cap, 2) if market_cap > 0 else None,
        "symbol": code or None,
    }


def _normalize_instrument_profile(
    profile: Optional[dict[str, Any]],
    *,
    target_symbols: list[str],
    source_symbol_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    defaults = _default_instrument_profile(
        target_symbols=target_symbols,
        source_symbol_summary=source_symbol_summary,
    )
    payload = dict(profile or {})
    summary = dict(source_symbol_summary or {})
    code = _primary_target_code(target_symbols)
    board_bucket = _normalize_board_bucket(
        payload.get("board_bucket") or defaults.get("board_bucket"),
        code=code,
    )
    annual_volatility_realized_252d, annual_volatility_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("annual_volatility_realized_252d", "annual_volatility_realized", "realized_annual_volatility"),
        legacy_keys=("annual_volatility",),
        minimum=0.12,
        maximum=0.80,
        default=_safe_float(defaults["annual_volatility"], defaults["annual_volatility"]),
    )
    atr14_pct_realized, atr14_pct_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("atr14_pct_realized", "atr14_pct_14d_realized", "realized_atr14_pct"),
        legacy_keys=("atr14_pct",),
        minimum=0.01,
        maximum=0.12,
        default=_safe_float(defaults["atr14_pct"], defaults["atr14_pct"]),
    )
    gap_p95_realized, gap_p95_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("gap_p95_realized", "gap_p95", "open_gap_p95"),
        legacy_keys=("gap_p95",),
        minimum=0.005,
        maximum=0.15,
        default=_safe_float(defaults["gap_p95"], defaults["gap_p95"]),
    )
    intraday_range_p90, intraday_range_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("intraday_range_p90", "intraday_range_p90_realized"),
        legacy_keys=(),
        minimum=0.01,
        maximum=0.20,
        default=_safe_float(defaults["intraday_range_p90"], defaults["intraday_range_p90"]),
    )
    trend_efficiency_60d_realized, trend_efficiency_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("trend_efficiency_60d_realized",),
        legacy_keys=("trend_efficiency_60d",),
        minimum=0.0,
        maximum=0.9,
        default=_safe_float(defaults["trend_efficiency_60d"], defaults["trend_efficiency_60d"]),
    )
    turnover_median, turnover_median_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("turnover_median_realized",),
        legacy_keys=("turnover_median",),
        minimum=0.1,
        maximum=8.0,
        default=_safe_float(defaults["turnover_median"], defaults["turnover_median"]),
    )
    volume_ratio_p80, volume_ratio_p80_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("volume_ratio_p80",),
        legacy_keys=(),
        minimum=1.0,
        maximum=2.5,
        default=_safe_float(defaults["volume_ratio_p80"], defaults["volume_ratio_p80"]),
    )
    volume_ratio_p90, volume_ratio_p90_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("volume_ratio_p90",),
        legacy_keys=(),
        minimum=1.02,
        maximum=3.0,
        default=_safe_float(defaults["volume_ratio_p90"], defaults["volume_ratio_p90"]),
    )
    turnover_rate_p80, turnover_rate_p80_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("turnover_rate_p80",),
        legacy_keys=(),
        minimum=0.1,
        maximum=8.0,
        default=_safe_float(defaults["turnover_rate_p80"], defaults["turnover_rate_p80"]),
    )
    turnover_rate_p90, turnover_rate_p90_source = _resolve_profile_metric(
        profile=payload,
        summary=summary,
        measured_keys=("turnover_rate_p90",),
        legacy_keys=(),
        minimum=0.1,
        maximum=10.0,
        default=_safe_float(defaults["turnover_rate_p90"], defaults["turnover_rate_p90"]),
    )
    measured_sources = {
        "annual_volatility_realized_252d": annual_volatility_source,
        "atr14_pct_realized": atr14_pct_source,
        "gap_p95_realized": gap_p95_source,
        "intraday_range_p90": intraday_range_source,
        "trend_efficiency_60d_realized": trend_efficiency_source,
        "turnover_median": turnover_median_source,
        "volume_ratio_p80": volume_ratio_p80_source,
        "volume_ratio_p90": volume_ratio_p90_source,
        "turnover_rate_p80": turnover_rate_p80_source,
        "turnover_rate_p90": turnover_rate_p90_source,
    }
    measured_profile_complete = all(
        measured_sources[key] != "default"
        for key in (
            "annual_volatility_realized_252d",
            "atr14_pct_realized",
            "gap_p95_realized",
            "intraday_range_p90",
            "trend_efficiency_60d_realized",
            "volume_ratio_p80",
            "volume_ratio_p90",
            "turnover_rate_p80",
            "turnover_rate_p90",
        )
    )
    measurement_source = "measured" if measured_profile_complete else (
        "partial_measured"
        if any(source != "default" for source in measured_sources.values())
        else "default_board_profile"
    )
    normalized = {
        "annual_volatility_realized_252d": annual_volatility_realized_252d,
        "annual_volatility": annual_volatility_realized_252d,
        "atr14_pct_realized": atr14_pct_realized,
        "atr14_pct": atr14_pct_realized,
        "gap_p95_realized": gap_p95_realized,
        "gap_p95": gap_p95_realized,
        "intraday_range_p90": intraday_range_p90,
        "trend_efficiency_60d_realized": trend_efficiency_60d_realized,
        "trend_efficiency_60d": trend_efficiency_60d_realized,
        "turnover_median": turnover_median,
        "volume_ratio_p80": volume_ratio_p80,
        "volume_ratio_p90": volume_ratio_p90,
        "turnover_rate_p80": turnover_rate_p80,
        "turnover_rate_p90": turnover_rate_p90,
        "measurement_source": measurement_source,
        "measurement_sources": measured_sources,
        "measured_profile_complete": measured_profile_complete,
        "board_bucket": board_bucket,
        "market_cap": round(
            _safe_float(
                payload.get("market_cap"),
                defaults.get("market_cap") or 0.0,
            ),
            2,
        ) if _safe_float(payload.get("market_cap"), defaults.get("market_cap") or 0.0) > 0 else defaults.get("market_cap"),
        "symbol": code or payload.get("symbol") or defaults.get("symbol"),
    }
    for extra_key in (
        "asset_class",
        "underlying",
        "curve_legs",
        "roll_rule",
    ):
        value = payload.get(extra_key)
        if value not in _EMPTY_VALUES:
            normalized[extra_key] = value
    if str(normalized.get("asset_class") or "").strip().lower() == "futures":
        normalized["board_bucket"] = "futures"
    return normalized
