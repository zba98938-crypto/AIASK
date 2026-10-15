"""策略模拟盘孵化：账户绑定、信号下发、指标沉淀。"""


from __future__ import annotations

import inspect
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from strategy_factory.api.semantic_contract import build_signal_evidence_records
from aiask_quant_core.incubation_stages import normalize_incubation_stage

from aiask_quant_core.storage.trade_audit_writer import record_trade_fill_from_order_and_trade

logger = logging.getLogger(__name__)


def _safe_rules_dict(value) -> dict:
    """将 risk_rules 字段安全地转换为 dict，防止反复 json.dumps 造成多层嵌套。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}

DEFAULT_INCUBATION_CAPITAL = 100000.0
DEFAULT_INCUBATION_RULES = {
    'max_position_pct': 25.0,
    'max_drawdown_pct': 18.0,
    'stop_loss_pct': 8.0,
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _runtime_playbook_for_strategy(strategy: dict) -> dict:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    return dict(
        payload.get("runtime_playbook")
        or params.get("runtime_playbook")
        or {}
    )


def _runtime_execution_guard(strategy: dict) -> dict:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    strategy_type = str(payload.get("strategy_type") or "").strip().lower()
    instrument_profile = dict(
        payload.get("instrument_profile")
        or params.get("instrument_profile")
        or {}
    )
    measurement_source = str(
        instrument_profile.get("measurement_source") or "default_board_profile"
    ).strip().lower() or "default_board_profile"
    measured_profile_complete = bool(instrument_profile.get("measured_profile_complete"))
    target_symbols = list(
        payload.get("target_symbols")
        or params.get("target_symbols")
        or []
    )
    single_name_trend = strategy_type in {"ma_cross", "momentum", "volatility_breakout", "event_structure_breakout"} and len(target_symbols) == 1
    proxy_runtime_used = bool(payload.get("proxy_runtime_used") or params.get("proxy_runtime_used"))
    runtime_family_data_source = str(
        payload.get("runtime_family_data_source")
        or params.get("runtime_family_data_source")
        or (
            "price_proxy_runtime"
            if strategy_type in {"quality_factor", "value_factor", "growth_factor"}
            else "market_data_runtime"
        )
    ).strip().lower() or "market_data_runtime"
    if strategy_type in {"quality_factor", "value_factor", "growth_factor"} and runtime_family_data_source != "fundamental_runtime":
        proxy_runtime_used = True
    semantic_runtime_match = (
        bool(payload.get("semantic_runtime_match"))
        if payload.get("semantic_runtime_match") is not None
        else bool(params.get("semantic_runtime_match"))
        if params.get("semantic_runtime_match") is not None
        else not proxy_runtime_used
    )
    execution_readiness_tier = str(
        payload.get("execution_readiness_tier")
        or params.get("execution_readiness_tier")
        or ""
    ).strip().lower()
    default_profile_runtime_blocked = single_name_trend and (
        measurement_source == "default_board_profile" or not measured_profile_complete
    )
    diagnostic_only = bool(payload.get("diagnostic_only") or params.get("diagnostic_only"))
    semantic_contract_missing_fields = list(
        payload.get("semantic_contract_missing_fields")
        or params.get("semantic_contract_missing_fields")
        or []
    )
    if proxy_runtime_used or default_profile_runtime_blocked or semantic_contract_missing_fields:
        diagnostic_only = True
    if not execution_readiness_tier:
        execution_readiness_tier = (
            "observe_diagnostic_only"
            if diagnostic_only or proxy_runtime_used or default_profile_runtime_blocked
            else "formal_runtime_ready"
        )
    allow_signal_entries = (
        not diagnostic_only
        and not proxy_runtime_used
        and semantic_runtime_match
        and execution_readiness_tier == "formal_runtime_ready"
    )
    # observe 诊断性纸面交易闸门:打破"要证据才能 formal、但只有 formal 才准下单产证据"的死锁。
    # observe 样本(零资本模拟)允许下诊断纸面单以积累 forward returns / effective_n,
    # 但仅限结构合法的样本——契约缺字段等结构性损坏仍禁止。不改变 formal 资格判定。
    import os as _os
    observe_paper_enabled = str(
        _os.getenv("INCUBATION_OBSERVE_PAPER_ENTRIES_ENABLED", "1")
    ).strip().lower() in {"1", "true", "yes", "on"}
    allow_observe_paper_entries = (
        observe_paper_enabled
        and not allow_signal_entries  # formal 走正常通道,无需此档
        and not semantic_contract_missing_fields  # 结构性损坏不放行
    )
    reasons: list[str] = []
    if semantic_contract_missing_fields:
        reasons.append("final_strategy_missing_semantic_contract")
    if proxy_runtime_used:
        reasons.append("proxy_runtime_not_allowed_for_formal_incubation")
    if default_profile_runtime_blocked:
        reasons.append("default_profile_not_allowed_for_single_name_runtime")
    if diagnostic_only and not reasons:
        reasons.append("diagnostic_only")
    return {
        "allow_signal_entries": allow_signal_entries,
        "allow_observe_paper_entries": allow_observe_paper_entries,
        "diagnostic_only": diagnostic_only,
        "proxy_runtime_used": proxy_runtime_used,
        "semantic_runtime_match": semantic_runtime_match,
        "execution_readiness_tier": execution_readiness_tier,
        "runtime_family_data_source": runtime_family_data_source,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _strategy_account_risk_rules(strategy: dict) -> dict:
    params = dict(dict(strategy or {}).get("params") or {})
    runtime_playbook = _runtime_playbook_for_strategy(strategy)
    exit_policy = dict(runtime_playbook.get("exit_policy") or {})
    position_policy = dict(runtime_playbook.get("position_policy") or {})
    risk_rules = _safe_rules_dict(
        dict(strategy or {}).get("risk_rules")
        or params.get("risk_rules")
        or {}
    )
    merged = dict(DEFAULT_INCUBATION_RULES)
    merged.update(risk_rules)
    stop_loss_pct = exit_policy.get("initial_stop_loss_pct")
    if stop_loss_pct is not None:
        merged["stop_loss_pct"] = round(_safe_float(stop_loss_pct, 0.08) * 100.0, 4)
    max_position_pct = position_policy.get("max_position_pct")
    if max_position_pct is not None:
        merged["max_position_pct"] = round(_safe_float(max_position_pct, 0.25) * 100.0, 4)
    return merged


def _parse_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_async_db_method(db, name: str):
    """Only treat explicitly provided async methods as adapter overrides.

    ``MagicMock``/``Mock`` synthesizes arbitrary attributes on access, so
    ``hasattr`` is too permissive here and can leak un-awaited child mocks
    into fallback branches during tests.
    """
    method = getattr(db, name, None)
    if method is None or not callable(method):
        return None
    if inspect.iscoroutinefunction(method):
        return method
    if hasattr(method, "await_count"):
        return method
    return None


def _get_db_acquire(db):
    """Return a real acquire() hook, not a lazily synthesized mock child."""
    acquire = getattr(db, "acquire", None)
    if not callable(acquire):
        return None

    raw = getattr(db, "raw", None)
    target = getattr(raw, "acquire", None) if raw is not None else acquire
    if target is None or not callable(target):
        return None
    if type(target).__module__.startswith("unittest.mock"):
        return None
    return acquire


def _build_signal_id(strategy_id: str, signal: dict, signal_date: date, code: str, direction: str) -> str:
    explicit = str(signal.get("signal_id") or signal.get("id") or "").strip()
    if explicit:
        return explicit
    seed = f"{strategy_id}:{signal_date}:{code}:{direction}"
    return f"sig_{uuid5(NAMESPACE_URL, seed).hex[:16]}"


def _build_position_id(strategy_id: str, account_id: str, code: str, signal_id: str) -> str:
    seed = f"{strategy_id}:{account_id}:{code}:{signal_id}:{uuid4().hex}"
    return f"pos_{uuid5(NAMESPACE_URL, seed).hex[:16]}"


def _resolve_strategy_target_codes(strategy: dict) -> set[str]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    ordered: list[str] = []
    seen: set[str] = set()
    filter_payloads: list[dict] = []

    def _push(value) -> None:
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
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)

    def _collect_codes(value) -> list[str]:
        resolved_codes: list[str] = []
        local_seen: set[str] = set()

        def _visit(item) -> None:
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
            if text and text not in local_seen:
                local_seen.add(text)
                resolved_codes.append(text)

        _visit(value)
        return resolved_codes

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
        except Exception:
            continue

    resolved = [code for code in ordered if code not in excluded_symbols]
    if prioritized_symbols:
        priority_order = [code for code in prioritized_symbols if code in resolved]
        remaining = [code for code in resolved if code not in set(priority_order)]
        resolved = [*priority_order, *remaining]
    if max_active_symbols > 0:
        resolved = resolved[:max_active_symbols]
    return set(resolved)


def _as_dict(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _dedup_strings(values) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def _runtime_action_lineage(strategy: dict, reason: str) -> dict:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    runtime_playbook = _runtime_playbook_for_strategy(payload)
    runtime_provenance = dict(runtime_playbook.get("_provenance") or {})
    semantic_lineage = {
        "claim_to_trade_plan_map": _as_dict(
            payload.get("claim_to_trade_plan_map") or params.get("claim_to_trade_plan_map")
        ),
        "trade_plan_to_dsl_map": _as_dict(
            payload.get("trade_plan_to_dsl_map") or params.get("trade_plan_to_dsl_map")
        ),
    }
    claim_map = _as_dict(semantic_lineage["claim_to_trade_plan_map"])
    trade_map = _as_dict(semantic_lineage["trade_plan_to_dsl_map"])
    trade_step_to_claim_ids = _as_dict(claim_map.get("trade_step_to_claim_ids"))
    trade_step_sections = _as_dict(trade_map.get("trade_step_to_dsl_sections"))
    source_trade_step_ids = _dedup_strings(
        list(runtime_playbook.get("source_trade_step_ids") or [])
        + list(runtime_provenance.get("source_trade_step_ids") or [])
    )
    exit_trade_step_ids = _dedup_strings(
        [
            step_id
            for step_id, sections in trade_step_sections.items()
            if "exit" in [str(section).strip().lower() for section in list(sections or [])]
        ]
    )
    if source_trade_step_ids:
        exit_trade_step_ids = _dedup_strings(
            [step_id for step_id in source_trade_step_ids if step_id in set(exit_trade_step_ids)]
            or exit_trade_step_ids
            or source_trade_step_ids
        )

    normalized_reason = str(reason or "").strip().lower()
    runtime_action_reason = normalized_reason
    runtime_action_source = "unmapped_runtime_action"
    label_token = ""
    if normalized_reason == "runtime_playbook_stop_loss":
        runtime_action_reason = "stop_loss"
        runtime_action_source = "runtime_playbook.exit_policy.initial_stop_loss_pct"
    elif normalized_reason == "runtime_playbook_time_stop":
        runtime_action_reason = "time_stop"
        runtime_action_source = "runtime_playbook.exit_policy.time_stop_days"
    elif normalized_reason.startswith("runtime_playbook_"):
        label_token = normalized_reason.removeprefix("runtime_playbook_")
        loss_bands = list(_as_dict(runtime_playbook.get("adverse_move_policy")).get("loss_bands") or [])
        for index, item in enumerate(loss_bands):
            band = dict(item or {})
            band_label = str(band.get("label") or "").strip().lower()
            band_action = str(band.get("action") or "").strip().lower()
            if label_token not in {band_label, band_action}:
                continue
            runtime_action_reason = (
                "reduce"
                if band_action == "reduce"
                else "freeze_reentry"
                if band_action == "freeze_reentry"
                else "stop_loss"
            )
            runtime_action_source = (
                f"runtime_playbook.adverse_move_policy.loss_bands[{index}]"
                f".{band_label or band_action or 'band'}"
            )
            break

    def _token_set(value: str) -> set[str]:
        return {
            token
            for token in str(value or "").replace("-", "_").split("_")
            if token
        }

    match_tokens = _token_set(label_token or runtime_action_reason)
    candidate_trade_step_ids = list(exit_trade_step_ids)
    token_matches = [
        step_id
        for step_id in candidate_trade_step_ids
        if match_tokens and _token_set(step_id).intersection(match_tokens)
    ]
    if len(token_matches) == 1:
        applied_trade_step_id = token_matches[0]
    elif len(candidate_trade_step_ids) == 1:
        applied_trade_step_id = candidate_trade_step_ids[0]
    else:
        applied_trade_step_id = None

    claim_candidates = _dedup_strings(
        trade_step_to_claim_ids.get(applied_trade_step_id) or []
    ) if applied_trade_step_id else []
    applied_claim_id = claim_candidates[0] if len(claim_candidates) == 1 else None
    return {
        "applied_trade_step_id": applied_trade_step_id,
        "applied_claim_id": applied_claim_id,
        "runtime_action_reason": runtime_action_reason,
        "runtime_action_source": runtime_action_source,
        "lineage_status": "mapped_runtime_action" if applied_trade_step_id else "unmapped_runtime_action",
        "trade_step_candidates": candidate_trade_step_ids,
        "claim_candidates": claim_candidates,
    }


async def _find_open_trade_position(db, strategy_id: str, account_id: str, code: str) -> Optional[dict]:
    list_positions_method = _get_async_db_method(db, "list_strategy_trade_positions")
    if list_positions_method is None:
        return None
    rows = await list_positions_method(strategy_id=strategy_id, account_id=account_id, code=code, limit=20)
    for row in list(rows or []):
        status = str((row or {}).get("status") or "").strip().lower()
        if status in {"pending_entry", "open"}:
            return dict(row)
    return None


async def _save_trade_position_seed(db, payload: dict) -> Optional[dict]:
    save_method = _get_async_db_method(db, "save_strategy_trade_position")
    if save_method is None:
        return None
    return await save_method(payload)


async def _record_trade_audit_fill(db, order: dict, trade: dict) -> Optional[dict]:
    return await record_trade_fill_from_order_and_trade(
        db,
        order,
        trade,
        source="incubation_settlement",
    )


def _per_symbol_regime_enabled() -> bool:
    """P0-3: 是否对信号标的逐股推断 trend_regime / vol_regime。

    默认 OFF：保持历史行为（trend/vol 维度恒 "unknown"，仅 sentiment 由 fear_greed 推断）。
    ON：按标的最新 K 线推断 trend（MA20 斜率 + 20 日动量）与 vol（20 日已实现波动率分位）。
    """
    raw = os.getenv("STRATEGY_FACTORY_PER_SYMBOL_REGIME_ENABLED")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _infer_symbol_regime(closes: list[float]) -> dict:
    """从收盘价序列推断 trend_regime / vol_regime（自包含，无外部依赖）。

    返回的标签为消费侧（ForwardVerifier）按字符串分组的自由 token：
    - trend_regime: trend_up / trend_down / range
    - vol_regime:   high_vol / normal_vol / low_vol
    数据不足时对应维度返回 "unknown"（不阻断）。
    """
    labels = {"trend_regime": "unknown", "vol_regime": "unknown"}
    try:
        series = [float(c) for c in closes if c is not None and float(c) > 0]
    except (TypeError, ValueError):
        return labels
    n = len(series)
    if n < 20:
        return labels

    # ── trend：20 日动量 + MA20 斜率方向 ──
    ret_20 = (series[-1] - series[-20]) / series[-20] if series[-20] else 0.0
    ma20_now = sum(series[-20:]) / 20.0
    if n >= 25:
        ma20_prev = sum(series[-25:-5]) / 20.0
    else:
        ma20_prev = sum(series[:20]) / 20.0
    ma_slope = (ma20_now - ma20_prev) / ma20_prev if ma20_prev else 0.0
    if ret_20 > 0.05 and ma_slope > 0:
        labels["trend_regime"] = "trend_up"
    elif ret_20 < -0.05 and ma_slope < 0:
        labels["trend_regime"] = "trend_down"
    else:
        labels["trend_regime"] = "range"

    # ── vol：最近 20 日已实现年化波动率，按绝对阈值分档 ──
    window = series[-21:] if n >= 21 else series
    rets = [
        (window[i] - window[i - 1]) / window[i - 1]
        for i in range(1, len(window))
        if window[i - 1]
    ]
    if len(rets) >= 5:
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
        ann_vol = (var ** 0.5) * (252 ** 0.5)
        if ann_vol >= 0.45:
            labels["vol_regime"] = "high_vol"
        elif ann_vol <= 0.20:
            labels["vol_regime"] = "low_vol"
        else:
            labels["vol_regime"] = "normal_vol"
    return labels


async def _infer_symbol_regime_from_db(db, code: str) -> dict:
    """P0-3: 取标的最新 K 线并推断 trend/vol regime。任何异常都回退 unknown（不阻断）。"""
    labels = {"trend_regime": "unknown", "vol_regime": "unknown"}
    if not code:
        return labels
    try:
        get_klines = getattr(db, "get_klines", None)
        if get_klines is None:
            return labels
        klines = await get_klines(code, limit=60)
        if not klines:
            return labels
        # 统一为时间升序的收盘价序列
        rows = list(klines)
        try:
            dates = [str((row or {}).get("date") or "") for row in rows]
            # Duplicate/missing dates cannot define a total order. Preserve
            # the provider's sequence in that case instead of grouping equal
            # calendar labels and corrupting the price path.
            if all(dates) and len(set(dates)) == len(dates):
                rows.sort(key=lambda row: str((row or {}).get("date") or ""))
        except Exception:
            pass
        closes = [
            (r or {}).get("close")
            for r in rows
            if isinstance(r, dict) and (r or {}).get("close") is not None
        ]
        return _infer_symbol_regime(closes)
    except Exception:
        return labels


def _resolve_signal_regime(strategy: dict, regime: Optional[dict]) -> dict:
    """INVERT-DESIGN P1 改动D：解析信号当日市场状态标签。

    优先用显式传入的 regime；否则从策略快照的 fear_greed 推断 sentiment_regime。
    trend_regime / vol_regime 由调用方（_persist_signal_evidence，持有 db+code）逐标的推断后经
    regime 传入；未传入时缺省为 unknown。任何异常都不阻断信号落库。
    """
    labels = {"trend_regime": "unknown", "vol_regime": "unknown", "sentiment_regime": "unknown"}
    try:
        if regime:
            for key in labels:
                value = str(dict(regime).get(key) or "").strip().lower()
                if value:
                    labels[key] = value
        if labels["sentiment_regime"] == "unknown":
            payload = dict(strategy or {})
            params = dict(payload.get("params") or {})
            snapshot = dict(payload.get("snapshot") or params.get("snapshot") or {})
            fg = (
                snapshot.get("fear_greed_index")
                or snapshot.get("fear_greed")
                or payload.get("fear_greed_index")
                or params.get("fear_greed_index")
            )
            if fg is not None:
                try:
                    fg_val = float(fg)
                    labels["sentiment_regime"] = (
                        "fear" if fg_val < 30 else "greed" if fg_val > 70 else "neutral"
                    )
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return labels


async def _persist_signal_evidence(
    db,
    strategy: dict,
    *,
    signal_id: str,
    position_id: str,
    account_id: str,
    signal_date: date,
    code: str,
    regime: Optional[dict] = None,
) -> None:
    save_method = _get_async_db_method(db, "save_strategy_signal_evidence")
    if save_method is None:
        return
    # regime 缺省时由 _resolve_signal_regime 兜底（各维度 unknown），不阻断主流程。
    # P0-3: toggle ON 时，对信号标的逐股推断 trend/vol regime，合并进显式 regime（不覆盖已传入的非空值）。
    effective_regime = dict(regime or {})
    if _per_symbol_regime_enabled():
        inferred = await _infer_symbol_regime_from_db(db, code)
        for dim in ("trend_regime", "vol_regime"):
            existing = str(effective_regime.get(dim) or "").strip().lower()
            if existing in ("", "unknown") and inferred.get(dim) not in (None, "unknown"):
                effective_regime[dim] = inferred[dim]
    regime_labels = _resolve_signal_regime(strategy, effective_regime)
    for evidence in build_signal_evidence_records(
        strategy,
        signal_id=signal_id,
        position_id=position_id,
        account_id=account_id,
        signal_date=signal_date,
        code=code,
        regime=regime_labels,
    ):
        try:
            await save_method(
                {
                    "id": evidence.get("id") or f"{signal_id}:{evidence.get('evidence_id')}",
                    "signal_id": signal_id,
                    "strategy_id": strategy.get("id"),
                    "signal_date": signal_date,
                    "signal_ts": evidence.get("signal_ts"),
                    "code": code,
                    "candidate_artifact_id": evidence.get("candidate_artifact_id"),
                    "experiment_id": evidence.get("experiment_id"),
                    "evidence_id": evidence.get("evidence_id"),
                    "applied_claim_id": evidence.get("applied_claim_id"),
                    "applied_trade_step_id": evidence.get("applied_trade_step_id"),
                    "source_type": evidence.get("source_type") or evidence.get("evidence_type"),
                    "direction": evidence.get("direction"),
                    "horizon_days": evidence.get("horizon_days"),
                    "raw_confidence": evidence.get("raw_confidence"),
                    "calibrated_confidence": evidence.get("calibrated_confidence"),
                    "proxy_only": evidence.get("proxy_only"),
                    "doc_uid": evidence.get("doc_uid"),
                    "headline_label_id": evidence.get("headline_label_id"),
                    # 改动D：regime 标签随 payload(JSON) 落库，避免依赖表新增列；
                    # ForwardVerifier 从 evidence_payload 读取分 regime 聚合。
                    "payload": evidence.get("evidence_payload") or evidence,
                }
            )
        except Exception as exc:
            logger.warning(
                "StrategyIncubationService: save signal evidence failed for %s/%s: %s",
                strategy.get("id"),
                signal_id,
                exc,
            )


async def _persist_runtime_signal_evidence(
    db,
    strategy: dict,
    *,
    signal_id: str,
    position_id: str,
    account_id: str,
    signal_date: date,
    code: str,
    reason: str,
) -> None:
    save_method = _get_async_db_method(db, "save_strategy_signal_evidence")
    if save_method is None:
        return
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    lineage = _runtime_action_lineage(payload, reason)
    candidate_artifact_id = str(
        payload.get("candidate_artifact_id")
        or payload.get("source_candidate_artifact_id")
        or params.get("source_candidate_artifact_id")
        or payload.get("hypothesis_artifact_id")
        or params.get("hypothesis_artifact_id")
        or ""
    ).strip() or None
    experiment_id = str(payload.get("experiment_id") or params.get("experiment_id") or "").strip() or None
    evidence_id = f"runtime_action:{str(lineage.get('runtime_action_reason') or reason).strip().lower()}"
    runtime_payload = {
        "strategy_id": payload.get("id"),
        "signal_id": signal_id,
        "position_id": position_id,
        "account_id": account_id,
        "signal_date": str(signal_date),
        "code": code,
        "raw_reason": reason,
        "runtime_action_reason": lineage.get("runtime_action_reason"),
        "runtime_action_source": lineage.get("runtime_action_source"),
        "applied_claim_id": lineage.get("applied_claim_id"),
        "applied_trade_step_id": lineage.get("applied_trade_step_id"),
        "lineage_status": lineage.get("lineage_status"),
        "trade_step_candidates": lineage.get("trade_step_candidates") or [],
        "claim_candidates": lineage.get("claim_candidates") or [],
    }
    try:
        await save_method(
            {
                "id": (
                    f"{signal_id}:{evidence_id}:"
                    f"{str(lineage.get('applied_claim_id') or 'unclaimed').strip()}:"
                    f"{str(lineage.get('applied_trade_step_id') or 'unmapped_step').strip()}"
                ),
                "signal_id": signal_id,
                "strategy_id": payload.get("id"),
                "signal_date": signal_date,
                "signal_ts": datetime.combine(signal_date, datetime.min.time(), tzinfo=timezone.utc),
                "code": code,
                "candidate_artifact_id": candidate_artifact_id,
                "experiment_id": experiment_id,
                "evidence_id": evidence_id,
                "applied_claim_id": lineage.get("applied_claim_id"),
                "applied_trade_step_id": lineage.get("applied_trade_step_id"),
                "source_type": "runtime_playbook",
                "direction": "down",
                "runtime_action_reason": lineage.get("runtime_action_reason"),
                "runtime_action_source": lineage.get("runtime_action_source"),
                "payload": runtime_payload,
            }
        )
    except Exception as exc:
        logger.warning(
            "StrategyIncubationService: save runtime signal evidence failed for %s/%s: %s",
            payload.get("id"),
            signal_id,
            exc,
        )


class StrategyIncubationService:
# --- incubation_parts/context.py ---
    async def _get_strategy_account(self, db, strategy_id: str) -> Optional[dict]:
        method = _get_async_db_method(db, 'get_paper_account_by_strategy')
        if method is not None:
            return await method(strategy_id)
        acquire = _get_db_acquire(db)
        if acquire is None:
            return None
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM paper_accounts WHERE strategy_id=$1 ORDER BY created_at LIMIT 1",
                strategy_id,
            )
        return dict(row) if row else None

    async def _save_strategy_account(self, db, account: dict) -> dict:
        method = _get_async_db_method(db, 'save_paper_account')
        if method is not None:
            return await method(account)
        acquire = _get_db_acquire(db)
        if acquire is None:
            return dict(account)
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_accounts
                    (id, user_id, name, initial_capital, current_capital, total_value, risk_rules,
                     strategy_id, account_type, incubation_stage, promotion_candidate, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    risk_rules = EXCLUDED.risk_rules,
                    strategy_id = EXCLUDED.strategy_id,
                    account_type = EXCLUDED.account_type,
                    incubation_stage = EXCLUDED.incubation_stage,
                    promotion_candidate = EXCLUDED.promotion_candidate,
                    status = EXCLUDED.status,
                    total_value = EXCLUDED.total_value,
                    current_capital = EXCLUDED.current_capital
                RETURNING *
                """,
                account['id'],
                account.get('user_id') or 'strategy_factory',
                account['name'],
                float(account.get('initial_capital') or DEFAULT_INCUBATION_CAPITAL),
                float(account.get('current_capital') or DEFAULT_INCUBATION_CAPITAL),
                float(account.get('total_value') or DEFAULT_INCUBATION_CAPITAL),
                json.dumps(_safe_rules_dict(account.get('risk_rules')) or DEFAULT_INCUBATION_RULES),
                account.get('strategy_id'),
                account.get('account_type') or 'incubation',
                account.get('incubation_stage') or 'warmup',
                bool(account.get('promotion_candidate')),
                account.get('status') or 'active',
            )
        return dict(row)

    async def _record_domain_event(self, db, strategy_id: Optional[str], event_type: str, payload: dict, *, source: str = 'incubation', severity: str = 'info', correlation_id: Optional[str] = None):
        method = _get_async_db_method(db, 'save_strategy_domain_event')
        if method is not None:
            await method({
                'strategy_id': strategy_id,
                'aggregate_type': 'strategy',
                'aggregate_id': strategy_id,
                'event_type': event_type,
                'source': source,
                'severity': severity,
                'correlation_id': correlation_id,
                'payload': payload,
            })

    async def ensure_account(self, db, strategy: dict, stage: str = 'warmup', source_run_id: Optional[str] = None) -> dict:
        strategy_id = strategy['id']
        stage = normalize_incubation_stage(stage)
        trace_metadata = dict(strategy.get('_closure_trace') or {})
        binding_method = _get_async_db_method(db, 'get_strategy_incubation_account')
        binding = await binding_method(strategy_id) if binding_method is not None else None
        account = None
        created = False
        # 2026-05-28: 防 stage 降级 — 如果已存在 binding 且新 stage='warmup'(默认值,
        # 通常来自 SignalTracker.process_strategies 或 IncubationIntake 的兜底调用),
        # 保留已有 stage 而不是覆盖。stage 升级方向为:
        #   warmup → paper → candidate → listed → promoted
        # 严格只允许向更高 stage 升级,默认 warmup 不能降级 paper/candidate。
        _STAGE_RANK = {
            'diagnostic': 0,
            'warmup': 0,
            'paper': 1,
            'candidate': 2,
            'listed': 3,
            'failed': 4,
            'retired': 4,
        }
        existing_stage = None
        if binding:
            existing_raw_stage = str(dict(binding).get('stage') or '').strip().lower()
            existing_stage = normalize_incubation_stage(existing_raw_stage) if existing_raw_stage else None
            if existing_stage and existing_stage != stage:
                if _STAGE_RANK.get(existing_stage, 0) > _STAGE_RANK.get(stage, 0):
                    # 新 stage 比已有的低,保留已有的(避免被降级覆盖)
                    stage = existing_stage
        if binding:
            account = await self._get_strategy_account(db, strategy_id)
        if not account:
            account = await self._get_strategy_account(db, strategy_id)
        if not account:
            account = await self._save_strategy_account(db, {
                'id': f'inc_{uuid4().hex[:8]}',
                'user_id': 'strategy_factory',
                'name': f"孵化_{str(strategy.get('name') or strategy_id)[:24]}",
                'initial_capital': DEFAULT_INCUBATION_CAPITAL,
                'current_capital': DEFAULT_INCUBATION_CAPITAL,
                'total_value': DEFAULT_INCUBATION_CAPITAL,
                'risk_rules': _strategy_account_risk_rules(strategy),
                'strategy_id': strategy_id,
                'account_type': 'incubation',
                'incubation_stage': stage,
                'promotion_candidate': False,
                'status': 'active',
            })
            created = True

        # 仅当账户新建或 stage 实际发生跃迁时才发 account_bound 事件。
        # 历史问题:每轮孵化对所有已绑定账户重复发 account_bound(created=False 且 stage 未变),
        # 使 strategy_domain_events 表里该事件膨胀到 86 万行(13406 账户 × ~64 轮),
        # 绝大多数是无变化的重复噪声。幂等 re-bind 不应留痕。
        stage_changed = bool(existing_stage) and existing_stage != stage
        emit_bound_event = created or stage_changed

        bind = await db.save_strategy_incubation_account(
            strategy_id,
            account['id'],
            stage=stage,
            status='active',
            source_run_id=source_run_id,
            metadata={
                'strategy_name': strategy.get('name'),
                'strategy_type': strategy.get('strategy_type'),
                **trace_metadata,
            },
        )
        if emit_bound_event:
            await self._record_domain_event(
                db,
                strategy_id,
                'incubation.account_bound',
                {
                    'account_id': account['id'],
                    'stage': stage,
                    'created': created,
                    'stage_changed': stage_changed,
                    'previous_stage': existing_stage,
                    'source_run_id': source_run_id,
                    'trace': trace_metadata,
                },
                correlation_id=trace_metadata.get('correlation_id') or source_run_id,
            )
        return {'created': created, 'account': account, 'binding': bind}

    async def _latest_price(self, db, code: str) -> Optional[float]:
        try:
            klines = await db.get_klines(code, limit=1)
            if klines:
                return float(klines[-1].get('close') or 0) or None
        except Exception:
            return None
        return None

    async def _price_on_or_before(self, db, code: str, as_of_date: Optional[date] = None) -> Optional[float]:
        code_token = str(code or '').strip()
        if not code_token:
            return None
        if as_of_date and hasattr(db, 'get_klines'):
            try:
                klines = await db.get_klines(
                    code_token,
                    end_date=str(as_of_date),
                    limit=1,
                )
                if klines:
                    return float((klines[-1] or {}).get('close') or 0.0) or None
            except Exception:
                pass
        return await self._latest_price(db, code_token)

    @staticmethod
    def _execution_timestamp(signal_date: Optional[date]) -> datetime:
        resolved_date = signal_date if isinstance(signal_date, date) else date.today()
        return datetime.combine(resolved_date, datetime.min.time(), tzinfo=timezone.utc)

    async def _list_positions(self, db, account_id: str) -> list[dict]:
        method = _get_async_db_method(db, 'list_paper_positions')
        if method is not None:
            return await method(account_id)
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM paper_positions WHERE account_id = $1 ORDER BY stock_code",
                account_id,
            )
        return [dict(row) for row in rows]

    async def _save_position(self, db, position: dict) -> dict:
        method = _get_async_db_method(db, 'save_paper_position')
        if method is not None:
            return await method(position)
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_positions
                    (account_id, stock_code, stock_name, quantity, cost_price, current_price, market_value, profit_rate, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (account_id, stock_code) DO UPDATE SET
                    stock_name = EXCLUDED.stock_name,
                    quantity = EXCLUDED.quantity,
                    cost_price = EXCLUDED.cost_price,
                    current_price = EXCLUDED.current_price,
                    market_value = EXCLUDED.market_value,
                    profit_rate = EXCLUDED.profit_rate,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                position.get('account_id'),
                position.get('stock_code'),
                position.get('stock_name') or position.get('stock_code') or '',
                int(position.get('quantity') or 0),
                float(position.get('cost_price') or 0.0),
                position.get('current_price'),
                position.get('market_value'),
                position.get('profit_rate'),
            )
        return dict(row)

    async def _save_trade(self, db, trade: dict) -> dict:
        method = _get_async_db_method(db, 'save_paper_trade')
        if method is not None:
            return await method(trade)
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_trades
                    (id, account_id, stock_code, stock_name, trade_type, price, quantity, amount, commission,
                     trade_time, reason, strategy_id, source_order_id, signal_id, position_id, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                trade.get('id'),
                trade.get('account_id'),
                trade.get('stock_code'),
                trade.get('stock_name') or trade.get('stock_code') or '',
                trade.get('trade_type'),
                float(trade.get('price') or 0.0),
                int(trade.get('quantity') or 0),
                float(trade.get('amount') or 0.0),
                float(trade.get('commission') or 0.0),
                trade.get('trade_time'),
                trade.get('reason'),
                trade.get('strategy_id'),
                trade.get('source_order_id'),
                trade.get('signal_id'),
                trade.get('position_id'),
            )
        return dict(row)

    async def _update_order(self, db, order_id: int, updates: dict) -> Optional[dict]:
        method = _get_async_db_method(db, 'update_paper_order')
        if method is not None:
            return await method(order_id, updates)
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE paper_orders
                SET price = COALESCE($2, price),
                    shares = COALESCE($3, shares),
                    status = COALESCE($4, status),
                    commission = COALESCE($5, commission),
                    reason = COALESCE($6, reason),
                    filled_at = COALESCE($7, filled_at),
                    signal_id = COALESCE($8, signal_id),
                    position_id = COALESCE($9, position_id),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                RETURNING *
                """,
                int(order_id),
                updates.get('price'),
                updates.get('shares'),
                updates.get('status'),
                updates.get('commission'),
                updates.get('reason'),
                updates.get('filled_at'),
                updates.get('signal_id'),
                updates.get('position_id'),
            )
        return dict(row) if row else None

    async def _save_nav_snapshot(self, db, account: dict, nav_date: date, cash: float, market_value: float) -> dict:
        account_id = account['id']
        total_value = round(cash + market_value, 4)
        nav_rows_method = _get_async_db_method(db, 'get_paper_nav_rows')
        rows = await nav_rows_method(account_id, limit=2) if nav_rows_method is not None else []
        prev = next((row for row in rows if str(row.get('nav_date')) != str(nav_date)), None)
        prev_total = float((prev or {}).get('total_value') or account.get('initial_capital') or total_value or DEFAULT_INCUBATION_CAPITAL)
        daily_return = ((total_value - prev_total) / prev_total) if prev_total > 0 else 0.0
        snapshot = {
            'account_id': account_id,
            'nav_date': nav_date,
            'total_value': total_value,
            'cash': round(cash, 4),
            'market_value': round(market_value, 4),
            'daily_return': round(daily_return, 6),
        }
        save_nav_method = _get_async_db_method(db, 'save_paper_nav')
        if save_nav_method is not None:
            await save_nav_method(snapshot)
        else:
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO paper_nav (account_id, nav_date, total_value, cash, market_value, daily_return, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP)
                    ON CONFLICT (account_id, nav_date) DO UPDATE
                    SET total_value=$3, cash=$4, market_value=$5, daily_return=$6
                    """,
                    snapshot['account_id'], snapshot['nav_date'], snapshot['total_value'], snapshot['cash'], snapshot['market_value'], snapshot['daily_return'],
                )
        updated_account = await self._save_strategy_account(db, {
            **account,
            'current_capital': round(cash, 4),
            'total_value': total_value,
        })
        return {'snapshot': snapshot, 'account': updated_account}

    async def _list_settleable_orders(
        self,
        db,
        *,
        strategy_id: str,
        account_id: str,
        signal_date: date,
    ) -> list[dict]:
        acquire = _get_db_acquire(db)
        if acquire is not None:
            async with acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM paper_orders
                    WHERE strategy_id=$1
                      AND account_id=$2
                      AND status IN ('pending', 'submitted')
                      AND (signal_date IS NULL OR signal_date <= $3)
                    ORDER BY signal_date ASC, created_at ASC, id ASC
                    LIMIT 500
                    """,
                    strategy_id,
                    account_id,
                    signal_date,
                )
            return [dict(row) for row in rows]

        list_orders_method = _get_async_db_method(db, 'list_strategy_paper_orders')
        if list_orders_method is None:
            return []
        rows_by_key: dict[str, dict] = {}
        for status in ('pending', 'submitted'):
            try:
                rows = await list_orders_method(strategy_id, status=status, limit=500)
            except TypeError:
                rows = await list_orders_method(strategy_id, signal_date)
            for row in list(rows or []):
                item = dict(row or {})
                order_account_id = str(item.get('account_id') or '').strip()
                if order_account_id and order_account_id != account_id:
                    continue
                raw_signal_date = item.get('signal_date')
                if raw_signal_date is not None:
                    try:
                        if str(raw_signal_date)[:10] > str(signal_date):
                            continue
                    except Exception:
                        continue
                if str(item.get('status') or 'pending') not in {'pending', 'submitted'}:
                    continue
                rows_by_key[str(item.get('id') or len(rows_by_key))] = item
        return sorted(
            rows_by_key.values(),
            key=lambda item: (
                str(item.get('signal_date') or ''),
                str(item.get('created_at') or ''),
                str(item.get('id') or ''),
            ),
        )

    async def settle_orders(self, db, strategy: dict, signal_date: Optional[date] = None) -> dict:
        signal_date = signal_date or date.today()
        ensure = await self.ensure_account(db, strategy)
        account = ensure['account']
        account_id = account['id']
        executable = await self._list_settleable_orders(
            db,
            strategy_id=strategy['id'],
            account_id=account_id,
            signal_date=signal_date,
        )
        positions = {str(item.get('stock_code') or ''): dict(item) for item in await self._list_positions(db, account_id)}
        cash = float(account.get('current_capital') or account.get('initial_capital') or DEFAULT_INCUBATION_CAPITAL)
        filled = []
        rejected = []
        execution_ts = self._execution_timestamp(signal_date)

        for order in executable:
            code = str(order.get('code') or '').strip()
            direction = str(order.get('direction') or '').strip().lower()
            shares = int(order.get('shares') or 0)
            if not code or shares <= 0 or direction not in {'buy', 'sell'}:
                rejected.append(await self._update_order(db, order['id'], {'status': 'rejected', 'reason': 'invalid_order'}))
                continue
            exec_price = await self._price_on_or_before(db, code, signal_date) or float(order.get('price') or 0)
            if exec_price <= 0:
                rejected.append(await self._update_order(db, order['id'], {'status': 'rejected', 'reason': 'price_unavailable'}))
                continue
            commission = round(exec_price * shares * 0.0003, 4)
            position = dict(positions.get(code) or {})
            current_qty = int(position.get('quantity') or 0)
            if direction == 'buy':
                amount = round(exec_price * shares, 4)
                total_cost = amount + commission
                if cash + 1e-9 < total_cost:
                    rejected.append(await self._update_order(db, order['id'], {'status': 'rejected', 'reason': 'insufficient_cash', 'price': round(exec_price, 4), 'commission': commission}))
                    continue
                cash = round(cash - total_cost, 4)
                new_qty = current_qty + shares
                avg_cost = float(position.get('cost_price') or 0.0)
                new_cost = ((avg_cost * current_qty) + amount) / max(new_qty, 1)
                latest_price = await self._price_on_or_before(db, code, signal_date) or exec_price
                market_value = round(latest_price * new_qty, 4)
                positions[code] = await self._save_position(db, {
                    'account_id': account_id,
                    'stock_code': code,
                    'stock_name': position.get('stock_name') or code,
                    'quantity': new_qty,
                    'cost_price': round(new_cost, 6),
                    'current_price': round(latest_price, 4),
                    'market_value': market_value,
                    'profit_rate': round(((latest_price - new_cost) / new_cost), 6) if new_cost > 0 else 0.0,
                })
            else:
                if current_qty < shares:
                    rejected.append(await self._update_order(db, order['id'], {'status': 'rejected', 'reason': 'insufficient_position', 'price': round(exec_price, 4)}))
                    continue
                amount = round(exec_price * shares, 4)
                cash = round(cash + amount - commission, 4)
                new_qty = current_qty - shares
                avg_cost = float(position.get('cost_price') or 0.0)
                latest_price = await self._price_on_or_before(db, code, signal_date) or exec_price
                market_value = round(latest_price * new_qty, 4)
                positions[code] = await self._save_position(db, {
                    'account_id': account_id,
                    'stock_code': code,
                    'stock_name': position.get('stock_name') or code,
                    'quantity': new_qty,
                    'cost_price': round(avg_cost, 6),
                    'current_price': round(latest_price, 4),
                    'market_value': market_value,
                    'profit_rate': round(((latest_price - avg_cost) / avg_cost), 6) if avg_cost > 0 else 0.0,
                })
            trade = await self._save_trade(db, {
                'id': f"ptr_{uuid4().hex[:10]}",
                'account_id': account_id,
                'stock_code': code,
                'stock_name': (positions.get(code) or {}).get('stock_name') or code,
                'trade_type': direction,
                'price': round(exec_price, 4),
                'quantity': shares,
                'amount': amount,
                'commission': commission,
                'trade_time': execution_ts,
                'reason': order.get('reason') or order.get('source') or 'strategy_signal',
                'strategy_id': strategy['id'],
                'source_order_id': str(order.get('id')),
                'signal_id': order.get('signal_id'),
                'position_id': order.get('position_id'),
            })
            updated_order = await self._update_order(db, order['id'], {
                'status': 'filled',
                'price': round(exec_price, 4),
                'commission': commission,
                'filled_at': execution_ts,
                'signal_id': order.get('signal_id'),
                'position_id': order.get('position_id'),
            })
            await _record_trade_audit_fill(db, updated_order or order, trade)
            filled.append({'order': updated_order, 'trade': trade})

        market_value = 0.0
        for code, position in list(positions.items()):
            qty = int(position.get('quantity') or 0)
            if qty <= 0:
                continue
            latest_price = await self._price_on_or_before(db, code, signal_date) or float(position.get('current_price') or position.get('cost_price') or 0.0)
            avg_cost = float(position.get('cost_price') or 0.0)
            market_value += latest_price * qty
            positions[code] = await self._save_position(db, {
                **position,
                'account_id': account_id,
                'stock_code': code,
                'current_price': round(latest_price, 4),
                'market_value': round(latest_price * qty, 4),
                'profit_rate': round(((latest_price - avg_cost) / avg_cost), 6) if avg_cost > 0 else 0.0,
            })

        nav_result = await self._save_nav_snapshot(db, account, signal_date, cash, market_value)
        if filled or rejected:
            await self._record_domain_event(
                db,
                strategy['id'],
                'incubation.orders_settled',
                {
                    'account_id': account_id,
                    'signal_date': str(signal_date),
                    'filled_count': len(filled),
                    'rejected_count': len([item for item in rejected if item]),
                    'nav': nav_result['snapshot'],
                },
                correlation_id=str(signal_date),
                severity='warning' if rejected else 'info',
            )
        await self._record_domain_event(
            db,
            strategy['id'],
            'incubation.nav_recorded',
            {
                'account_id': account_id,
                'signal_date': str(signal_date),
                'nav': nav_result['snapshot'],
            },
            correlation_id=str(signal_date),
        )
        return {
            'strategy_id': strategy['id'],
            'account_id': account_id,
            'filled_count': len(filled),
            'rejected_count': len([item for item in rejected if item]),
            'nav_snapshot': nav_result['snapshot'],
            'cash': nav_result['snapshot']['cash'],
            'market_value': nav_result['snapshot']['market_value'],
        }
# --- incubation_parts/specs.py ---

    async def sync_signals_to_orders(self, db, strategy: dict, signal_date: date) -> dict:
        ensure = await self.ensure_account(db, strategy)
        account = ensure['account']
        account_id = account['id']
        runtime_playbook = _runtime_playbook_for_strategy(strategy)
        entry_policy = dict(runtime_playbook.get("entry_policy") or {})
        exit_policy = dict(runtime_playbook.get("exit_policy") or {})
        adverse_move_policy = dict(runtime_playbook.get("adverse_move_policy") or {})
        reentry_policy = dict(runtime_playbook.get("reentry_policy") or {})
        position_policy = dict(runtime_playbook.get("position_policy") or {})
        execution_guard = _runtime_execution_guard(strategy)
        signals = await db.get_signals(strategy['id'], start_date=signal_date, end_date=signal_date, limit=200)
        allowed_codes = _resolve_strategy_target_codes(strategy)
        if allowed_codes:
            signals = [
                item for item in list(signals or [])
                if str((item or {}).get('code') or '').strip() in allowed_codes
            ]
        list_orders_method = _get_async_db_method(db, 'list_strategy_paper_orders')
        if list_orders_method is not None:
            existing_orders = await list_orders_method(strategy['id'], signal_date)
        else:
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM paper_orders WHERE strategy_id=$1 AND signal_date=$2",
                    strategy['id'], signal_date,
                )
            existing_orders = [dict(row) for row in rows]
        existing_keys = {(row.get('code'), row.get('direction')) for row in existing_orders}
        created = []
        skipped = 0
        skip_reason_counts: dict[str, int] = {}
        blocked_by_execution_guard = 0
        capital_scaled_for_min_lot = False
        capital_scale_events: list[dict] = []
        min_lot_capital_scale_enabled = str(
            os.getenv("INCUBATION_PAPER_MIN_LOT_CAPITAL_SCALE_ENABLED", "1")
        ).strip().lower() in {"1", "true", "yes", "on"}

        def _skip(reason: str) -> None:
            nonlocal skipped
            skipped += 1
            token = str(reason or "unknown").strip() or "unknown"
            skip_reason_counts[token] = int(skip_reason_counts.get(token) or 0) + 1

        initial_capital = float(account.get('initial_capital') or DEFAULT_INCUBATION_CAPITAL)
        current_capital = float(account.get('current_capital') or initial_capital or DEFAULT_INCUBATION_CAPITAL)
        total_value = float(account.get('total_value') or current_capital or DEFAULT_INCUBATION_CAPITAL)
        base_budget_pct = max(0.01, _safe_float(position_policy.get("base_budget_pct"), 0.12))
        max_position_pct = max(0.02, _safe_float(position_policy.get("max_position_pct"), 0.25))
        max_concurrent_positions = max(1, _safe_int(position_policy.get("max_concurrent_positions"), 2))
        order_style = str(entry_policy.get("order_style") or "limit").strip().lower() or "limit"
        max_slippage_bps = max(0.0, _safe_float(entry_policy.get("max_slippage_bps"), 0.0))
        cooldown_days = max(0, _safe_int(reentry_policy.get("cooldown_days"), 0))
        initial_stop_loss_pct = max(0.0, _safe_float(exit_policy.get("initial_stop_loss_pct"), 0.0))
        time_stop_days = max(0, _safe_int(exit_policy.get("time_stop_days"), 0))
        loss_bands = sorted(
            [
                dict(item or {})
                for item in list(adverse_move_policy.get("loss_bands") or [])
                if _safe_float((item or {}).get("threshold_pct") or (item or {}).get("loss_pct"), 0.0) > 0
            ],
            key=lambda item: _safe_float(item.get("threshold_pct") or item.get("loss_pct"), 0.0),
        )

        positions = {
            str(item.get('stock_code') or '').strip(): dict(item)
            for item in await self._list_positions(db, account_id)
            if int(item.get('quantity') or 0) > 0
        }
        list_trade_positions = _get_async_db_method(db, "list_strategy_trade_positions")
        trade_positions = (
            await list_trade_positions(strategy_id=strategy['id'], account_id=account_id, limit=200)
            if list_trade_positions is not None
            else []
        )
        open_trade_positions: dict[str, dict] = {}
        latest_closed_by_code: dict[str, dict] = {}
        for row in list(trade_positions or []):
            item = dict(row or {})
            code = str(item.get("code") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if not code:
                continue
            if status in {"pending_entry", "open"}:
                open_trade_positions[code] = item
            elif status == "closed":
                latest_closed_by_code[code] = item

        paper_order_history: list[dict] = []
        if list_orders_method is not None:
            try:
                paper_order_history = list(await list_orders_method(strategy['id'], None, limit=1) or [])
            except TypeError:
                try:
                    paper_order_history = list(await list_orders_method(strategy['id'], None) or [])
                except TypeError:
                    paper_order_history = []
        else:
            acquire = _get_db_acquire(db)
            if acquire is not None:
                async with acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM paper_orders WHERE strategy_id=$1 LIMIT 1",
                        strategy['id'],
                    )
                paper_order_history = [dict(row) for row in rows]

        paper_trade_history: list[dict] = []
        list_trade_history_method = _get_async_db_method(db, "list_strategy_paper_trades")
        if list_trade_history_method is not None:
            try:
                paper_trade_history = list(
                    await list_trade_history_method(strategy['id'], account_id=account_id, limit=1) or []
                )
            except TypeError:
                try:
                    paper_trade_history = list(await list_trade_history_method(strategy['id'], account_id, 1) or [])
                except TypeError:
                    try:
                        paper_trade_history = list(await list_trade_history_method(strategy['id']) or [])
                    except TypeError:
                        paper_trade_history = []
        else:
            acquire = _get_db_acquire(db)
            if acquire is not None:
                async with acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT * FROM paper_trades WHERE strategy_id=$1 LIMIT 1",
                        strategy['id'],
                    )
                paper_trade_history = [dict(row) for row in rows]

        account_has_paper_history = bool(
            paper_order_history
            or paper_trade_history
            or positions
            or trade_positions
        )

        active_position_codes = set(positions.keys()) | set(open_trade_positions.keys())
        current_open_slots = len(active_position_codes)

        async def _persist_order(
            *,
            code: str,
            direction: str,
            shares: int,
            price: float,
            source: str,
            signal_id: str,
            position_id: str,
            reason: Optional[str] = None,
        ) -> Optional[dict]:
            if shares <= 0:
                return None
            order = {
                'account_id': account_id,
                'strategy_id': strategy['id'],
                'signal_date': signal_date,
                'source': source,
                'code': code,
                'direction': direction,
                'shares': shares,
                'price': round(float(price), 4),
                'order_type': order_style if direction == 'buy' else 'marketable_limit',
                'status': 'pending',
                'signal_id': signal_id,
                'position_id': position_id,
            }
            if reason:
                order['reason'] = reason
            save_order_method = _get_async_db_method(db, 'save_paper_order')
            if save_order_method is not None:
                return await save_order_method(order)
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO paper_orders
                        (account_id, strategy_id, signal_date, source, code, direction, shares, price,
                         order_type, status, signal_id, position_id, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING *
                    """,
                    account_id,
                    strategy['id'],
                    signal_date,
                    source,
                    code,
                    direction,
                    shares,
                    round(float(price), 4),
                    order.get('order_type') or 'limit',
                    'pending',
                    signal_id,
                    position_id,
                )
            return dict(row)

        def _order_price(base_price: float, direction: str) -> float:
            if order_style != "marketable_limit" or max_slippage_bps <= 0:
                return round(float(base_price), 4)
            multiplier = 1.0 + (max_slippage_bps / 10000.0)
            if direction == 'sell':
                multiplier = max(0.0, 1.0 - (max_slippage_bps / 10000.0))
            return round(float(base_price) * multiplier, 4)

        def _cooldown_active(code: str) -> bool:
            if cooldown_days <= 0:
                return False
            latest_closed = dict(latest_closed_by_code.get(code) or {})
            closed_at = _parse_datetime(latest_closed.get("closed_at") or latest_closed.get("exit_ts"))
            if closed_at is None:
                return False
            reference = datetime.combine(signal_date, datetime.min.time(), tzinfo=timezone.utc)
            return (reference - closed_at).days < cooldown_days

        async def _maybe_scale_empty_account_for_min_lot(
            *,
            code: str,
            price: float,
            min_lot_cost: float,
            max_affordable_budget: float,
        ) -> bool:
            nonlocal account, initial_capital, current_capital, total_value, capital_scaled_for_min_lot
            if min_lot_cost <= 0 or max_position_pct <= 0:
                return False
            if min_lot_cost <= max_affordable_budget:
                return False
            if not min_lot_capital_scale_enabled:
                return False
            if account_has_paper_history or created:
                return False

            previous_capital = {
                "initial_capital": round(float(initial_capital), 4),
                "current_capital": round(float(current_capital), 4),
                "total_value": round(float(total_value), 4),
            }
            required_total_for_position_cap = float(min_lot_cost) / max(float(max_position_pct), 1e-6)
            required_cash_for_order = float(min_lot_cost) * 1.002
            scaled_capital = round(
                max(
                    DEFAULT_INCUBATION_CAPITAL,
                    float(initial_capital),
                    float(current_capital),
                    float(total_value),
                    required_total_for_position_cap,
                    required_cash_for_order,
                ),
                4,
            )
            if scaled_capital <= max(float(initial_capital), float(current_capital), float(total_value)) + 0.01:
                return False

            updated_account = dict(account or {})
            updated_account["initial_capital"] = scaled_capital
            updated_account["current_capital"] = scaled_capital
            updated_account["total_value"] = scaled_capital
            account = await self._save_strategy_account(db, updated_account)
            initial_capital = float(account.get("initial_capital") or scaled_capital)
            current_capital = float(account.get("current_capital") or scaled_capital)
            total_value = float(account.get("total_value") or scaled_capital)
            capital_scaled_for_min_lot = True
            event_payload = {
                "account_id": account_id,
                "signal_date": str(signal_date),
                "code": code,
                "price": round(float(price), 4),
                "min_lot_cost": round(float(min_lot_cost), 4),
                "max_position_pct": round(float(max_position_pct), 6),
                "previous_capital": previous_capital,
                "scaled_capital": {
                    "initial_capital": round(float(initial_capital), 4),
                    "current_capital": round(float(current_capital), 4),
                    "total_value": round(float(total_value), 4),
                },
                "reason": "empty_paper_account_min_lot_affordability",
            }
            capital_scale_events.append(event_payload)
            await self._record_domain_event(
                db,
                strategy['id'],
                'incubation.paper_account_capital_scaled',
                event_payload,
                correlation_id=str(signal_date),
            )
            return True

        async def _maybe_seed_position(
            *,
            code: str,
            direction: str,
            signal_id: str,
            position_id: str,
            saved_order: dict,
        ) -> None:
            bound_position = open_trade_positions.get(code) if direction == 'sell' else {}
            await _save_trade_position_seed(
                db,
                {
                    'position_id': position_id,
                    'strategy_id': strategy['id'],
                    'account_id': account_id,
                    'signal_id': signal_id,
                    'code': code,
                    'direction': 'long',
                    'status': 'pending_exit' if direction == 'sell' else 'pending_entry',
                    'entry_order_id': str(saved_order.get('id')) if direction == 'buy' else (bound_position or {}).get('entry_order_id'),
                    'exit_order_id': str(saved_order.get('id')) if direction == 'sell' else (bound_position or {}).get('exit_order_id'),
                    'opened_at': (bound_position or {}).get('opened_at'),
                    'last_trade_time': (bound_position or {}).get('last_trade_time'),
                },
            )

        for code, position in list(positions.items()):
            if (code, 'sell') in existing_keys:
                continue
            shares = int(position.get('quantity') or 0)
            if shares <= 0:
                continue
            latest_price = await self._price_on_or_before(db, code, signal_date)
            if latest_price is None or latest_price <= 0:
                continue
            entry_price = _safe_float(position.get('cost_price'), 0.0)
            pnl_ratio = (latest_price / entry_price - 1.0) if entry_price > 0 else 0.0
            open_position = dict(open_trade_positions.get(code) or {})
            prior_exit_shares = int(open_position.get("exit_shares") or 0)
            reason = None
            exit_shares = shares
            if initial_stop_loss_pct > 0 and pnl_ratio <= -initial_stop_loss_pct:
                reason = 'runtime_playbook_stop_loss'
            elif time_stop_days > 0:
                opened_at = _parse_datetime(open_position.get('opened_at') or open_position.get('entry_ts'))
                reference = datetime.combine(signal_date, datetime.min.time(), tzinfo=timezone.utc)
                if opened_at is not None and (reference - opened_at).days >= time_stop_days:
                    reason = 'runtime_playbook_time_stop'
            if reason is None:
                for band in loss_bands:
                    threshold = abs(_safe_float(band.get("threshold_pct") or band.get("loss_pct"), 0.0))
                    action = str(band.get("action") or "").strip().lower()
                    if threshold <= 0 or action in {"", "hold"} or pnl_ratio > -threshold:
                        continue
                    if action == 'reduce':
                        if prior_exit_shares > 0:
                            continue
                        reduced = int((shares * 0.5) / 100) * 100
                        exit_shares = reduced if reduced >= 100 else shares
                    else:
                        exit_shares = shares
                    reason = f"runtime_playbook_{str(band.get('label') or action).strip().lower()}"
                    break
            if not reason:
                continue
            signal_id = _build_signal_id(strategy['id'], {'reason': reason}, signal_date, code, 'sell')
            position_id = str((open_position or {}).get('position_id') or _build_position_id(strategy['id'], account_id, code, signal_id))
            saved_order = await _persist_order(
                code=code,
                direction='sell',
                shares=exit_shares,
                price=_order_price(latest_price, 'sell'),
                source='runtime_playbook',
                signal_id=signal_id,
                position_id=position_id,
                reason=reason,
            )
            if saved_order is None:
                _skip("order_persist_failed")
                continue
            created.append(saved_order)
            await _persist_runtime_signal_evidence(
                db,
                strategy,
                signal_id=signal_id,
                position_id=position_id,
                account_id=account_id,
                signal_date=signal_date,
                code=code,
                reason=reason,
            )
            await _maybe_seed_position(
                code=code,
                direction='sell',
                signal_id=signal_id,
                position_id=position_id,
                saved_order=saved_order,
            )
            existing_keys.add((code, 'sell'))

        for signal in signals:
            code = str(signal.get('code') or '').strip()
            latest_signal = int(signal.get('signal') or 0)
            if not code or latest_signal == 0:
                continue
            direction = 'buy' if latest_signal > 0 else 'sell'
            entry_is_observe_paper = False
            if direction == 'buy' and not execution_guard.get("allow_signal_entries"):
                # observe 诊断纸面交易:允许 observe 样本下零资本诊断单以积累前向证据,
                # 打破"无证据→不能formal、不能交易→无证据"死锁。结构损坏样本仍被 guard 拦。
                if execution_guard.get("allow_observe_paper_entries"):
                    entry_is_observe_paper = True
                else:
                    blocked_by_execution_guard += 1
                    _skip("execution_guard_blocked")
                    continue
            if (code, direction) in existing_keys:
                _skip("duplicate_order")
                continue
            price = await self._price_on_or_before(db, code, signal_date)
            if price is None or price <= 0:
                _skip("price_missing")
                continue
            if direction == 'buy':
                if code in active_position_codes:
                    _skip("active_position_exists")
                    continue
                if _cooldown_active(code):
                    _skip("cooldown_active")
                    continue
                if current_open_slots >= max_concurrent_positions:
                    _skip("max_concurrent_positions_reached")
                    continue
                budget_per_trade = max(
                    min(current_capital * base_budget_pct, total_value * max_position_pct),
                    5000.0,
                )
                min_lot_cost = float(price) * 100.0
                max_affordable_budget = max(
                    0.0,
                    min(float(current_capital), float(total_value) * max_position_pct),
                )
                if budget_per_trade < min_lot_cost <= max_affordable_budget:
                    budget_per_trade = min_lot_cost
                shares = int(budget_per_trade / price / 100) * 100
                if shares < 100 and min_lot_cost > max_affordable_budget:
                    scaled = await _maybe_scale_empty_account_for_min_lot(
                        code=code,
                        price=price,
                        min_lot_cost=min_lot_cost,
                        max_affordable_budget=max_affordable_budget,
                    )
                    if scaled:
                        budget_per_trade = max(
                            min(current_capital * base_budget_pct, total_value * max_position_pct),
                            5000.0,
                        )
                        max_affordable_budget = max(
                            0.0,
                            min(float(current_capital), float(total_value) * max_position_pct),
                        )
                        if budget_per_trade < min_lot_cost <= max_affordable_budget:
                            budget_per_trade = min_lot_cost
                        shares = int(budget_per_trade / price / 100) * 100
                if shares < 100:
                    reason = "min_lot_unaffordable" if min_lot_cost > max_affordable_budget else "shares_lt_100"
                    _skip(reason)
                    continue
            else:
                position = dict(positions.get(code) or {})
                shares = int(position.get('quantity') or 0)
                # P0 FIX: fallback to open_trade_positions if no account-level position
                if shares <= 0:
                    trade_position = dict(open_trade_positions.get(code) or {})
                    shares = int(trade_position.get('quantity') or 0)
                if shares <= 0:
                    _skip("sell_without_position")
                    continue
            signal_id = _build_signal_id(strategy['id'], dict(signal or {}), signal_date, code, direction)
            bound_position = open_trade_positions.get(code) if direction == 'sell' else None
            position_id = str((bound_position or {}).get('position_id') or '').strip()
            if not position_id:
                position_id = _build_position_id(strategy['id'], account_id, code, signal_id)
            saved_order = await _persist_order(
                code=code,
                direction=direction,
                shares=shares,
                price=_order_price(price, direction),
                source='observe_paper_diagnostic' if entry_is_observe_paper else 'strategy_signal',
                signal_id=signal_id,
                position_id=position_id,
            )
            if saved_order is None:
                _skip("order_persist_failed")
                continue
            created.append(saved_order)
            await _maybe_seed_position(
                code=code,
                direction=direction,
                signal_id=signal_id,
                position_id=position_id,
                saved_order=saved_order,
            )
            await _persist_signal_evidence(
                db,
                strategy,
                signal_id=signal_id,
                position_id=position_id,
                account_id=account_id,
                signal_date=signal_date,
                code=code,
            )
            existing_keys.add((code, direction))
            if direction == 'buy':
                active_position_codes.add(code)
                current_open_slots += 1

        if created or skipped:
            await self._record_domain_event(
                db,
                strategy['id'],
                'incubation.orders_synced',
                {
                    'account_id': account_id,
                    'signal_date': str(signal_date),
                    'created_count': len(created),
                    'skipped_count': skipped,
                    'skip_reason_counts': dict(skip_reason_counts),
                    'blocked_by_execution_guard': blocked_by_execution_guard,
                    'execution_guard': execution_guard,
                    'capital_scaled_for_min_lot': capital_scaled_for_min_lot,
                    'capital_scale_events': list(capital_scale_events),
                    'codes': [item.get('code') for item in created if item.get('code')],
                },
                correlation_id=str(signal_date),
            )

        return {
            'strategy_id': strategy['id'],
            'account_id': account_id,
            'created_count': len(created),
            'skipped_count': skipped,
            'skip_reason_counts': dict(skip_reason_counts),
            'blocked_by_execution_guard': blocked_by_execution_guard,
            'execution_guard': execution_guard,
            'capital_scaled_for_min_lot': capital_scaled_for_min_lot,
            'capital_scale_events': list(capital_scale_events),
            'orders': created,
        }

    async def force_close_open_positions(
        self,
        db,
        strategy: dict,
        signal_date: date,
        *,
        reason: str = 'replay_window_end_forced_exit',
        source: str = 'history_replay',
        codes=None,
    ) -> dict:
        ensure = await self.ensure_account(db, strategy)
        account = ensure['account']
        account_id = account['id']
        runtime_playbook = _runtime_playbook_for_strategy(strategy)
        entry_policy = dict(runtime_playbook.get("entry_policy") or {})
        order_style = str(entry_policy.get("order_style") or "marketable_limit").strip().lower() or "marketable_limit"
        max_slippage_bps = max(0.0, _safe_float(entry_policy.get("max_slippage_bps"), 0.0))
        list_orders_method = _get_async_db_method(db, 'list_strategy_paper_orders')
        if list_orders_method is not None:
            existing_orders = await list_orders_method(strategy['id'], signal_date)
        else:
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM paper_orders WHERE strategy_id=$1 AND signal_date=$2",
                    strategy['id'], signal_date,
                )
            existing_orders = [dict(row) for row in rows]
        existing_keys = {(row.get('code'), row.get('direction')) for row in existing_orders}
        positions = {
            str(item.get('stock_code') or '').strip(): dict(item)
            for item in await self._list_positions(db, account_id)
            if int(item.get('quantity') or 0) > 0
        }
        allowed_codes = {
            str(code or '').strip()
            for code in list(codes or [])
            if str(code or '').strip()
        }
        reconciled_before_close = False
        if allowed_codes and not allowed_codes.issubset(set(positions.keys())):
            try:
                from akshare_mcp.tools.managers._paper_trading_manager_support import (
                    _reconcile_account_state,
                )
                reconcile = await _reconcile_account_state(
                    db,
                    account_id,
                    refresh_prices=False,
                    force=True,
                )
                reconciled_before_close = bool((reconcile or {}).get('reconciled'))
                positions = {
                    str(item.get('stock_code') or '').strip(): dict(item)
                    for item in await self._list_positions(db, account_id)
                    if int(item.get('quantity') or 0) > 0
                }
            except Exception as exc:
                logger.debug(
                    "StrategyIncubationService: reconcile before force close failed for %s/%s: %s",
                    strategy.get('id'),
                    account_id,
                    exc,
                )
        if allowed_codes:
            positions = {
                code: position
                for code, position in positions.items()
                if code in allowed_codes
            }
        list_trade_positions = _get_async_db_method(db, "list_strategy_trade_positions")
        trade_positions = (
            await list_trade_positions(strategy_id=strategy['id'], account_id=account_id, limit=200)
            if list_trade_positions is not None
            else []
        )
        open_trade_positions: dict[str, dict] = {}
        for row in list(trade_positions or []):
            item = dict(row or {})
            code = str(item.get("code") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if code and status in {"pending_entry", "open"}:
                open_trade_positions[code] = item

        def _order_price(base_price: float) -> float:
            if order_style != "marketable_limit" or max_slippage_bps <= 0:
                return round(float(base_price), 4)
            multiplier = max(0.0, 1.0 - (max_slippage_bps / 10000.0))
            return round(float(base_price) * multiplier, 4)

        created = []
        skipped = 0
        save_order_method = _get_async_db_method(db, 'save_paper_order')

        for code, position in list(positions.items()):
            if (code, 'sell') in existing_keys:
                skipped += 1
                continue
            shares = int(position.get('quantity') or 0)
            if shares <= 0:
                skipped += 1
                continue
            latest_price = await self._price_on_or_before(db, code, signal_date)
            if latest_price is None or latest_price <= 0:
                skipped += 1
                continue
            open_position = dict(open_trade_positions.get(code) or {})
            signal_id = _build_signal_id(
                strategy['id'],
                {'reason': reason, 'forced_exit': True},
                signal_date,
                code,
                'sell',
            )
            position_id = str(
                (open_position or {}).get('position_id')
                or _build_position_id(strategy['id'], account_id, code, signal_id)
            )
            order = {
                'account_id': account_id,
                'strategy_id': strategy['id'],
                'signal_date': signal_date,
                'source': source,
                'code': code,
                'direction': 'sell',
                'shares': shares,
                'price': _order_price(latest_price),
                'order_type': 'marketable_limit',
                'status': 'pending',
                'signal_id': signal_id,
                'position_id': position_id,
                'reason': reason,
            }
            if save_order_method is not None:
                saved_order = await save_order_method(order)
            else:
                async with db.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO paper_orders
                            (account_id, strategy_id, signal_date, source, code, direction, shares, price,
                             order_type, status, signal_id, position_id, reason, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                                $9, $10, $11, $12, $13, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        RETURNING *
                        """,
                        account_id,
                        strategy['id'],
                        signal_date,
                        source,
                        code,
                        'sell',
                        shares,
                        _order_price(latest_price),
                        'marketable_limit',
                        'pending',
                        signal_id,
                        position_id,
                        reason,
                    )
                saved_order = dict(row) if row else None
            if not saved_order:
                skipped += 1
                continue
            created.append(saved_order)
            await _persist_runtime_signal_evidence(
                db,
                strategy,
                signal_id=signal_id,
                position_id=position_id,
                account_id=account_id,
                signal_date=signal_date,
                code=code,
                reason=reason,
            )
            await _save_trade_position_seed(
                db,
                {
                    'position_id': position_id,
                    'strategy_id': strategy['id'],
                    'account_id': account_id,
                    'signal_id': signal_id,
                    'code': code,
                    'direction': 'long',
                    'status': 'pending_exit',
                    'entry_order_id': (open_position or {}).get('entry_order_id'),
                    'exit_order_id': str(saved_order.get('id')) if saved_order else None,
                    'opened_at': (open_position or {}).get('opened_at') or self._execution_timestamp(signal_date),
                    'last_trade_time': (open_position or {}).get('last_trade_time') or self._execution_timestamp(signal_date),
                },
            )
            existing_keys.add((code, 'sell'))

        if created or skipped:
            await self._record_domain_event(
                db,
                strategy['id'],
                'incubation.replay_window_force_close',
                {
                    'account_id': account_id,
                    'signal_date': str(signal_date),
                    'created_count': len(created),
                    'skipped_count': skipped,
                    'reason': reason,
                    'requested_codes': sorted(allowed_codes),
                    'reconciled_before_close': reconciled_before_close,
                    'codes': [item.get('code') for item in created if item.get('code')],
                },
                correlation_id=f"{signal_date}:force_close",
                severity='warning' if created else 'info',
            )

        return {
            'strategy_id': strategy['id'],
            'account_id': account_id,
            'created_count': len(created),
            'skipped_count': skipped,
            'requested_codes': sorted(allowed_codes),
            'reconciled_before_close': reconciled_before_close,
            'orders': created,
            'reason': reason,
        }

    # Fix #12: 6 阶段孵化映射
    @staticmethod
    def _derive_incubation_stage(overview: dict, open_risk_count: int = 0) -> str:
        """根据信号质量与风险状态推导当前阶段。"""
        from .strategy_lifecycle_shared import resolve_incubation_pipeline_stage

        if str(overview.get('pipeline_stage') or '').strip():
            return str(overview.get('pipeline_stage'))
        return resolve_incubation_pipeline_stage(
            overview.get('signal_quality') or {},
            open_risk_count=open_risk_count,
            execution_audit_gate_status=overview.get('execution_audit_gate_status'),
        )
# --- incubation_parts/runtime.py ---

    async def record_metrics(self, db, strategy: dict, metric_date: Optional[date] = None) -> Optional[dict]:
        metric_date = metric_date or date.today()
        binding = await self.ensure_account(db, strategy)
        account = binding['account']
        account_id = account['id']

        nav_rows_method = _get_async_db_method(db, 'get_paper_nav_rows')
        if nav_rows_method is not None:
            nav_rows = await nav_rows_method(account_id, limit=60)
            order_summary = await db.get_paper_order_summary(account_id)
        else:
            async with db.acquire() as conn:
                nav_rows = [dict(row) for row in await conn.fetch(
                    "SELECT * FROM paper_nav WHERE account_id=$1 ORDER BY nav_date DESC LIMIT 60",
                    account_id,
                )]
                summary = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN status IN ('pending','submitted') THEN 1 ELSE 0 END), 0) AS total_orders,
                        COALESCE(SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END), 0) AS filled_orders
                    FROM paper_orders
                    WHERE account_id=$1
                    """,
                    account_id,
                )
                trade_summary = await conn.fetchrow(
                    "SELECT COALESCE(COUNT(*), 0) AS total_trades, COALESCE(SUM(amount), 0) AS trade_amount FROM paper_trades WHERE account_id=$1",
                    account_id,
                )
                order_summary = {
                    'total_orders': int((summary or {}).get('total_orders') or 0),
                    'filled_orders': int((summary or {}).get('filled_orders') or 0),
                    'total_trades': int((trade_summary or {}).get('total_trades') or 0),
                    'trade_amount': float((trade_summary or {}).get('trade_amount') or 0.0),
                }

        latest_nav = nav_rows[0] if nav_rows else None
        total_value = float((latest_nav or {}).get('total_value') or account.get('total_value') or account.get('initial_capital') or DEFAULT_INCUBATION_CAPITAL)
        cash = float((latest_nav or {}).get('cash') or account.get('current_capital') or 0.0)
        market_value = float((latest_nav or {}).get('market_value') or max(total_value - cash, 0.0))
        daily_return = float((latest_nav or {}).get('daily_return') or 0.0)

        nav_values = [float(row.get('total_value') or 0) for row in reversed(nav_rows)]
        peak = nav_values[0] if nav_values else total_value
        max_drawdown = 0.0
        for value in nav_values:
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - value) / peak)

        returns = [float(row.get('daily_return') or 0) for row in nav_rows if row.get('daily_return') is not None]
        # Fix #9: 至少需要 20 个数据点才能计算有统计意义的 Sharpe
        if len(returns) >= 20:
            mean_r = sum(returns) / len(returns)
            variance = sum((item - mean_r) ** 2 for item in returns) / max(len(returns) - 1, 1)
            std_r = variance ** 0.5
            sharpe_ratio = (mean_r / std_r) * (252 ** 0.5) if std_r > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        signal_stats = await db.get_signal_stats(strategy['id'])
        hit_rate_5d = float((signal_stats.get('hit_rate') or {}).get(5, (signal_stats.get('hit_rate') or {}).get('5', 0)) or 0)
        forward_ic_5d = float((signal_stats.get('forward_ic') or {}).get(5, (signal_stats.get('forward_ic') or {}).get('5', 0)) or 0)
        forward_sharpe_5d = float((signal_stats.get('forward_sharpe') or {}).get(5, (signal_stats.get('forward_sharpe') or {}).get('5', 0)) or 0)
        total_signals = int(signal_stats.get('total_signals') or 0)

        metrics = await db.get_strategy_metrics(strategy['id'])
        backtest = next((item for item in metrics if item.get('period') in ('all', 'backtest')), {})
        baseline_sharpe = float(backtest.get('sharpe_ratio') or 0)
        baseline_mdd = abs(float(backtest.get('max_drawdown') or 0))
        alpha_decay = max(0.0, baseline_sharpe - max(forward_sharpe_5d, 0.0))
        drift_score = (abs(max_drawdown - baseline_mdd) + abs(baseline_sharpe - forward_sharpe_5d)) / 2 if baseline_sharpe or baseline_mdd else 0.0
        exposure_rate = (market_value / total_value) if total_value > 0 else 0.0
        turnover_rate = float(order_summary.get('trade_amount') or 0.0) / total_value if total_value > 0 else 0.0

        from .strategy_lifecycle_shared import build_incubation_overview as _build_incubation_overview
        overview = await _build_incubation_overview(db, strategy)
        signal_quality = dict(overview.get('signal_quality') or {})
        execution_quality = dict(overview.get('execution_quality') or {})
        signal_quality_5d = dict((signal_quality.get('by_horizon') or {}).get('5') or {})
        overview_without_signal_quality = dict(overview)
        overview_without_signal_quality.pop('signal_quality', None)
        overview_without_signal_quality.pop('execution_quality', None)
        decision = 'promote' if overview.get('promotion_ready') else ('observe' if not overview.get('deprecation_risk') else 'halt')
        open_risk_count = 0
        if hasattr(db, 'list_strategy_runtime_risk_events'):
            open_risks = await db.list_strategy_runtime_risk_events(
                strategy_id=str(strategy['id']),
                status='open',
                limit=20,
            )
            open_risk_count = len(list(open_risks or []))
        derived_stage = self._derive_incubation_stage(overview, open_risk_count=open_risk_count)

        metric = await db.save_strategy_incubation_metric(strategy['id'], metric_date, {
            'account_id': account_id,
            # Fix #12: 使用完整的 6 阶段映射替代二元分类
            'stage': derived_stage,
            'total_value': round(total_value, 4),
            'cash': round(cash, 4),
            'market_value': round(market_value, 4),
            'nav': round(total_value / max(float(account.get('initial_capital') or DEFAULT_INCUBATION_CAPITAL), 1.0), 6),
            'daily_return': round(daily_return, 6),
            'max_drawdown': round(max_drawdown, 6),
            'sharpe_ratio': round(sharpe_ratio, 6),
            'hit_rate_5d': round(hit_rate_5d, 6),
            'hit_rate_lcb_5d': round(float(signal_quality_5d.get('hit_rate_lcb') or 0.0), 6) if signal_quality_5d.get('hit_rate_lcb') is not None else None,
            'skill_lcb_5d': round(float(signal_quality_5d.get('skill_lcb') or 0.0), 6) if signal_quality_5d.get('skill_lcb') is not None else None,
            'effective_n_5d': int(signal_quality_5d.get('effective_n') or 0) if signal_quality_5d.get('effective_n') is not None else None,
            'recent_hit_rate_5d': round(float(signal_quality_5d.get('recent_hit_rate') or 0.0), 6) if signal_quality_5d.get('recent_hit_rate') is not None else None,
            'recent_skill_lcb_5d': round(float(signal_quality_5d.get('recent_skill_lcb') or 0.0), 6) if signal_quality_5d.get('recent_skill_lcb') is not None else None,
            'stability_gap_5d': round(float(signal_quality_5d.get('stability_gap') or 0.0), 6) if signal_quality_5d.get('stability_gap') is not None else None,
            'forward_ic_5d': round(forward_ic_5d, 6),
            'forward_sharpe_5d': round(forward_sharpe_5d, 6),
            'total_signals': total_signals,
            'total_orders': int(order_summary.get('total_orders') or 0),
            'total_trades': int(order_summary.get('total_trades') or 0),
            'turnover_rate': round(turnover_rate, 6),
            'exposure_rate': round(exposure_rate, 6),
            'alpha_decay': round(alpha_decay, 6),
            'drift_score': round(drift_score, 6),
            'blockers': overview.get('blockers') or [],
            'risk_flags': overview.get('risk_flags') or [],
            'decision': decision,
            'metadata': {
                'overview': overview_without_signal_quality,
                'signal_quality': signal_quality,
                'execution_quality': execution_quality,
                'binding_created': bool(binding.get('created')),
                'open_risk_count': open_risk_count,
            },
        })
        update_account_status_method = _get_async_db_method(db, 'update_paper_account_status')
        if update_account_status_method is not None:
            await update_account_status_method(
                account_id,
                'active',
                stage=metric.get('stage') or 'warmup',
                promotion_candidate=bool(overview.get('promotion_ready')),
            )
        await self._record_domain_event(
            db,
            strategy['id'],
            'incubation.metric_recorded',
            {
                'account_id': account_id,
                'metric_date': str(metric_date),
                'decision': metric.get('decision'),
                'stage': metric.get('stage'),
                'nav': metric.get('nav'),
                'promotion_candidate': bool(overview.get('promotion_ready')),
            },
            correlation_id=str(metric_date),
        )
        return metric

    async def process_strategies(self, db, strategies: list[dict], signal_date: Optional[date] = None) -> dict:
        signal_date = signal_date or date.today()
        accounts_bound = 0
        orders_created = 0
        orders_filled = 0
        rejected_orders = 0
        nav_snapshots = 0
        metrics_recorded = 0
        skip_reason_counts: dict[str, int] = {}
        items = []
        for strategy in strategies:
            try:
                # Normalize JSON-text params/factor_weights from SQLite rows.
                if not isinstance(strategy, dict):
                    continue
                strategy = dict(strategy)
                for key in ("params", "factor_weights"):
                    value = strategy.get(key)
                    if isinstance(value, str):
                        text = value.strip()
                        if not text:
                            strategy[key] = {}
                        else:
                            try:
                                import json as _json
                                strategy[key] = _json.loads(text)
                            except Exception:
                                strategy[key] = {}
                    elif value is None:
                        strategy[key] = {}
                ensure = await self.ensure_account(db, strategy)
                accounts_bound += 1 if ensure.get('created') else 0
                sync_result = await self.sync_signals_to_orders(db, strategy, signal_date)
                settle_result = await self.settle_orders(db, strategy, signal_date)
                metric = await self.record_metrics(db, strategy, signal_date)
                orders_created += int(sync_result.get('created_count') or 0)
                orders_filled += int(settle_result.get('filled_count') or 0)
                rejected_orders += int(settle_result.get('rejected_count') or 0)
                nav_snapshots += 1 if settle_result.get('nav_snapshot') else 0
                metrics_recorded += 1 if metric else 0
                for reason, count in dict(sync_result.get('skip_reason_counts') or {}).items():
                    token = str(reason or 'unknown').strip() or 'unknown'
                    skip_reason_counts[token] = int(skip_reason_counts.get(token) or 0) + int(count or 0)
                items.append({
                    'strategy_id': strategy.get('id'),
                    'account_id': (ensure.get('account') or {}).get('id'),
                    'orders_created': sync_result.get('created_count', 0),
                    'orders_skipped': sync_result.get('skipped_count', 0),
                    'skip_reason_counts': dict(sync_result.get('skip_reason_counts') or {}),
                    'orders_filled': settle_result.get('filled_count', 0),
                    'rejected_orders': settle_result.get('rejected_count', 0),
                    'nav': (settle_result.get('nav_snapshot') or {}).get('total_value'),
                    'decision': (metric or {}).get('decision'),
                })
            except Exception as exc:
                logger.warning('StrategyIncubationService.process_strategies failed for %s: %s', strategy.get('id'), exc)
                items.append({'strategy_id': strategy.get('id'), 'error': str(exc)})
        return {
            'count': len(strategies),
            'accounts_bound': accounts_bound,
            'orders_created': orders_created,
            'orders_filled': orders_filled,
            'rejected_orders': rejected_orders,
            'skip_reason_counts': dict(skip_reason_counts),
            'nav_snapshots': nav_snapshots,
            'metrics_recorded': metrics_recorded,
            'items': items,
        }

    async def _resolve_replay_dates(
        self,
        db,
        strategy: dict,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        include_market_days: bool = True,
        max_dates: int = 1500,
    ) -> list[date]:
        strategy_id = str(strategy.get('id') or '').strip()
        if not strategy_id:
            return []

        resolved_start = start_date
        resolved_end = end_date
        signal_rows = await db.get_signals(
            strategy_id,
            start_date=start_date,
            end_date=end_date,
            limit=max(1000, int(max_dates or 1500) * 20),
        )
        signal_dates = sorted(
            {
                item.get('signal_date')
                for item in list(signal_rows or [])
                if isinstance(item.get('signal_date'), date)
            }
        )
        if signal_dates and resolved_start is None:
            resolved_start = signal_dates[0]
        if signal_dates and resolved_end is None:
            resolved_end = max(signal_dates[-1], date.today())
        if resolved_start is None:
            resolved_start = date.today()
        if resolved_end is None:
            resolved_end = max(resolved_start, date.today())
        if resolved_end < resolved_start:
            resolved_end = resolved_start

        replay_dates: set[date] = set(signal_dates)
        if include_market_days and hasattr(db, 'get_klines'):
            candidate_codes = list(_resolve_strategy_target_codes(strategy))
            if not candidate_codes:
                candidate_codes = list(
                    {
                        str(item.get('code') or '').strip()
                        for item in list(signal_rows or [])
                        if str(item.get('code') or '').strip()
                    }
                )
            for code in candidate_codes[:3]:
                try:
                    klines = await db.get_klines(
                        code,
                        start_date=str(resolved_start),
                        end_date=str(resolved_end),
                    )
                except Exception as exc:
                    logger.warning(
                        'StrategyIncubationService._resolve_replay_dates kline lookup failed for %s/%s: %s',
                        strategy_id,
                        code,
                        exc,
                    )
                    continue
                kline_dates = []
                for row in list(klines or []):
                    trade_date = row.get('date')
                    if isinstance(trade_date, str):
                        try:
                            trade_date = date.fromisoformat(trade_date[:10])
                        except Exception:
                            trade_date = None
                    if isinstance(trade_date, date):
                        kline_dates.append(trade_date)
                if kline_dates:
                    replay_dates.update(kline_dates)
                    break

        ordered = sorted(
            trade_date
            for trade_date in replay_dates
            if isinstance(trade_date, date) and resolved_start <= trade_date <= resolved_end
        )
        if max_dates and len(ordered) > int(max_dates):
            ordered = ordered[-int(max_dates):]
        return ordered

    async def replay_strategy_history(
        self,
        db,
        strategy: dict,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        include_market_days: bool = True,
        max_dates: int = 1500,
        force_close_open_positions: bool = False,
        run_acceptance: bool = True,
    ) -> dict:
        strategy_id = str(strategy.get('id') or '').strip()
        replay_dates = await self._resolve_replay_dates(
            db,
            strategy,
            start_date=start_date,
            end_date=end_date,
            include_market_days=include_market_days,
            max_dates=max_dates,
        )
        replayed_days = 0
        orders_created = 0
        orders_filled = 0
        rejected_orders = 0
        metrics_recorded = 0
        non_empty_days = 0
        last_day_result = None
        daily_results: list[dict] = []

        for replay_date in replay_dates:
            day_result = await self.process_strategies(
                db,
                [strategy],
                signal_date=replay_date,
            )
            replayed_days += 1
            orders_created += int(day_result.get('orders_created') or 0)
            orders_filled += int(day_result.get('orders_filled') or 0)
            rejected_orders += int(day_result.get('rejected_orders') or 0)
            metrics_recorded += int(day_result.get('metrics_recorded') or 0)
            if any(
                int(day_result.get(key) or 0) > 0
                for key in ('orders_created', 'orders_filled', 'rejected_orders')
            ):
                non_empty_days += 1
            daily_results.append(
                {
                    'signal_date': str(replay_date),
                    'orders_created': int(day_result.get('orders_created') or 0),
                    'orders_filled': int(day_result.get('orders_filled') or 0),
                    'rejected_orders': int(day_result.get('rejected_orders') or 0),
                    'metrics_recorded': int(day_result.get('metrics_recorded') or 0),
                }
            )
            last_day_result = day_result

        if force_close_open_positions and replay_dates:
            close_result = await self.force_close_open_positions(
                db,
                strategy,
                replay_dates[-1],
            )
            created_count = int(close_result.get('created_count') or 0)
            if created_count > 0:
                settle_result = await self.settle_orders(db, strategy, replay_dates[-1])
                metric = await self.record_metrics(db, strategy, replay_dates[-1])
                orders_created += created_count
                orders_filled += int(settle_result.get('filled_count') or 0)
                rejected_orders += int(settle_result.get('rejected_count') or 0)
                metrics_recorded += 1 if metric else 0
                non_empty_days += 1
                last_day_result = {
                    'count': 1,
                    'accounts_bound': 0,
                    'orders_created': created_count,
                    'orders_filled': int(settle_result.get('filled_count') or 0),
                    'rejected_orders': int(settle_result.get('rejected_count') or 0),
                    'nav_snapshots': 1 if settle_result.get('nav_snapshot') else 0,
                    'metrics_recorded': 1 if metric else 0,
                    'items': [
                        {
                            'strategy_id': strategy.get('id'),
                            'forced_window_close': True,
                            'reason': close_result.get('reason'),
                            'orders_created': created_count,
                            'orders_filled': int(settle_result.get('filled_count') or 0),
                            'rejected_orders': int(settle_result.get('rejected_count') or 0),
                        }
                    ],
                }
                daily_results.append(
                    {
                        'signal_date': str(replay_dates[-1]),
                        'orders_created': created_count,
                        'orders_filled': int(settle_result.get('filled_count') or 0),
                        'rejected_orders': int(settle_result.get('rejected_count') or 0),
                        'metrics_recorded': 1 if metric else 0,
                        'window_force_close': True,
                        'reason': close_result.get('reason'),
                    }
                )

        acceptance = None
        if run_acceptance and hasattr(db, 'run_execution_audit_acceptance'):
            acceptance = await db.run_execution_audit_acceptance(
                strategy_id=strategy_id,
                backfill=True,
            )

        return {
            'strategy_id': strategy_id,
            'replayed_days': replayed_days,
            'non_empty_days': non_empty_days,
            'orders_created': orders_created,
            'orders_filled': orders_filled,
            'rejected_orders': rejected_orders,
            'metrics_recorded': metrics_recorded,
            'start_date': str(replay_dates[0]) if replay_dates else None,
            'end_date': str(replay_dates[-1]) if replay_dates else None,
            'daily_results': daily_results,
            'last_day_result': last_day_result,
            'acceptance': acceptance,
        }

    async def replay_strategies_history(
        self,
        db,
        strategies: list[dict],
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        include_market_days: bool = True,
        max_dates: int = 1500,
        force_close_open_positions: bool = False,
        run_acceptance: bool = True,
    ) -> dict:
        items = []
        replayed_days = 0
        non_empty_days = 0
        orders_created = 0
        orders_filled = 0
        rejected_orders = 0
        metrics_recorded = 0
        acceptance_status_counts: dict[str, int] = {}
        execution_audit_gate_status_counts: dict[str, int] = {}
        execution_hard_gate_passed_count = 0
        acceptance_overall_ready_count = 0
        acceptance_sample_gap_count = 0
        acceptance_realized_trade_count_total = 0

        def _count(target: dict[str, int], value: object, *, default: str = "missing") -> None:
            key = str(value or "").strip() or default
            target[key] = target.get(key, 0) + 1

        def _acceptance_has_sample_gap(payload: dict) -> bool:
            if "sample_gap" in {str(item or "").strip() for item in list(payload.get("gap_categories") or [])}:
                return True
            for detail in list(payload.get("blocker_details") or []):
                if isinstance(detail, dict) and str(detail.get("category") or "").strip() == "sample_gap":
                    return True
            return False

        for strategy in list(strategies or []):
            try:
                result = await self.replay_strategy_history(
                    db,
                    strategy,
                    start_date=start_date,
                    end_date=end_date,
                    include_market_days=include_market_days,
                    max_dates=max_dates,
                    force_close_open_positions=force_close_open_positions,
                    run_acceptance=run_acceptance,
                )
            except Exception as exc:
                logger.warning(
                    'StrategyIncubationService.replay_strategies_history failed for %s: %s',
                    strategy.get('id'),
                    exc,
                )
                result = {
                    'strategy_id': strategy.get('id'),
                    'error': str(exc),
                    'replayed_days': 0,
                    'non_empty_days': 0,
                    'orders_created': 0,
                    'orders_filled': 0,
                    'rejected_orders': 0,
                    'metrics_recorded': 0,
                }
            items.append(result)
            replayed_days += int(result.get('replayed_days') or 0)
            non_empty_days += int(result.get('non_empty_days') or 0)
            orders_created += int(result.get('orders_created') or 0)
            orders_filled += int(result.get('orders_filled') or 0)
            rejected_orders += int(result.get('rejected_orders') or 0)
            metrics_recorded += int(result.get('metrics_recorded') or 0)
            acceptance = result.get('acceptance')
            if isinstance(acceptance, dict):
                _count(acceptance_status_counts, acceptance.get('status'))
                trade_audit_summary = dict(acceptance.get('trade_audit_summary') or {})
                gate_status = (
                    str(acceptance.get('execution_audit_gate_status') or '').strip()
                    or str(trade_audit_summary.get('execution_audit_gate_status') or '').strip()
                    or 'missing'
                )
                _count(execution_audit_gate_status_counts, gate_status)
                acceptance_matrix = dict(acceptance.get('acceptance_matrix') or {})
                if bool(acceptance_matrix.get('overall_ready')) or str(acceptance.get('status') or '') == 'ready':
                    acceptance_overall_ready_count += 1
                if bool(acceptance.get('execution_hard_gate_passed')):
                    execution_hard_gate_passed_count += 1
                if _acceptance_has_sample_gap(acceptance):
                    acceptance_sample_gap_count += 1
                acceptance_realized_trade_count_total += int(trade_audit_summary.get('realized_trade_count') or 0)
        return {
            'count': len(list(strategies or [])),
            'replayed_days': replayed_days,
            'non_empty_days': non_empty_days,
            'orders_created': orders_created,
            'orders_filled': orders_filled,
            'rejected_orders': rejected_orders,
            'metrics_recorded': metrics_recorded,
            'acceptance_status_counts': acceptance_status_counts,
            'execution_audit_gate_status_counts': execution_audit_gate_status_counts,
            'execution_hard_gate_passed_count': execution_hard_gate_passed_count,
            'acceptance_overall_ready_count': acceptance_overall_ready_count,
            'acceptance_sample_gap_count': acceptance_sample_gap_count,
            'acceptance_realized_trade_count_total': acceptance_realized_trade_count_total,
            'items': items,
        }


_incubation_service: Optional[StrategyIncubationService] = None


def get_strategy_incubation_service() -> StrategyIncubationService:
    global _incubation_service
    if _incubation_service is None:
        _incubation_service = StrategyIncubationService()
    return _incubation_service
