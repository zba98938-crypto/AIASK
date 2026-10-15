"""策略 DSL：规范化、编译与条件求值。"""
from __future__ import annotations

# --- strategy_dsl_parts/context.py ---
from copy import deepcopy
from typing import Any, Optional

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def audit_candidate_semantic_contract(candidate: dict[str, Any]) -> dict[str, Any]:
    """Local quant-core semantic audit fallback.

    Strategy Factory performs richer semantic checks through its own ports. The
    core DSL compiler keeps only a dependency-free integrity envelope.
    """
    using_new_contract = bool(dict((candidate or {}).get("prediction_contract") or {}))
    return {
        "using_new_contract": using_new_contract,
        "legacy_semantic_contract": not using_new_contract,
        "semantic_integrity_score": 1.0,
        "evidence_alignment_score": 1.0,
        "evidence_alignment_audit": {},
        "hard_fail_reasons": [],
    }


def inspect_strategy_dsl_support(candidate: dict[str, Any]) -> dict[str, Any]:
    return {"supported": True, "reasons": []}

SUPPORTED_DSL_VERSION = "1.0"
SUPPORTED_FIELDS = {"open", "high", "low", "close", "volume"}
SUPPORTED_INDICATORS = {
    "sma", "ema", "roc", "rsi", "stddev", "zscore",
    "highest", "lowest", "volume_ratio", "atr",
    "adx", "turnover_rate", "upper_shadow_ratio", "rolling_count", "slope",
}
SUPPORTED_COMPARE_OPS = {"gt", "gte", "lt", "lte", "eq", "ne", "cross_above", "cross_below"}
SUPPORTED_BINARY_OPS = {"add", "sub", "mul", "div", "max", "min"}


def _normalize_code_list(values: Any, limit: int = 12) -> list[str]:
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
            normalized = raw.replace(";", ",").replace("|", ",").replace("\n", ",").replace("\t", ",").replace(" ", ",")
            for part in normalized.split(","):
                visit(part)
            return
        code = raw.split(".")[0].strip()
        if not code or code in seen:
            return
        seen.add(code)
        codes.append(code)

    visit(values)
    return codes[: max(1, min(int(limit or 12), 40))]


def _structured_payload(value: Any, *, field: str = "summary") -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, "", [], {}):
        return {}
    return {field: str(value)}


def _factory_contract_required(payload: dict[str, Any]) -> bool:
    return any(
        payload.get(key) not in (None, "", [], {})
        for key in ("research_task", "target_symbols", "stock_pool", "hypothesis", "holding_horizon", "tags", "rationale")
    )


def _is_event_blueprint(payload: dict[str, Any]) -> bool:
    research_task = dict(payload.get("research_task") or {})
    event_context = dict(payload.get("event_context") or {})
    return bool(
        str(research_task.get("task_source") or "").strip().lower() == "event_driven"
        or research_task.get("event_id")
        or research_task.get("theme_code")
        or event_context.get("event_id")
        or event_context.get("theme_code")
    )


def _requires_stock_pool_rationale(payload: dict[str, Any]) -> bool:
    research_task = dict(payload.get("research_task") or {})
    research_symbols = set(
        _normalize_code_list(
            [
                research_task.get("target_symbols"),
                research_task.get("stock_pool"),
                (research_task.get("event_context") or {}).get("target_symbols"),
            ],
            limit=16,
        )
    )
    if not research_symbols:
        return False
    candidate_symbols = set(
        _normalize_code_list(
            [
                payload.get("target_symbols"),
                payload.get("stock_pool"),
                ((payload.get("dsl") or {}).get("metadata") or {}).get("target_symbols"),
                ((payload.get("dsl") or {}).get("metadata") or {}).get("stock_pool"),
            ],
            limit=16,
        )
    )
    return bool(candidate_symbols and not candidate_symbols.issubset(research_symbols))


def _validate_factory_blueprint_contract(payload: dict[str, Any], normalized_dsl: Optional[dict[str, Any]] = None) -> None:
    if not _factory_contract_required(payload):
        return
    risk_rules = dict(payload.get("risk_rules") or (normalized_dsl or {}).get("risk_rules") or {})
    if not risk_rules:
        raise ValueError("factory blueprint requires dsl risk_rules")
    if _is_event_blueprint(payload) and not dict(payload.get("holding_horizon") or {}):
        raise ValueError("event factory blueprint requires holding_horizon")
    if _requires_stock_pool_rationale(payload):
        stock_pool = dict(payload.get("stock_pool") or {})
        if not str(stock_pool.get("rationale") or "").strip():
            raise ValueError("expanded factory stock_pool requires rationale")


def build_ohlcv_frame(klines: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(klines or []))
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    aliases = {
        "open": ["open", "开盘"],
        "high": ["high", "最高"],
        "low": ["low", "最低"],
        "close": ["close", "收盘", "close_price"],
        "volume": ["volume", "vol", "成交量"],
    }
    normalized = pd.DataFrame(index=frame.index)
    lower_map = {str(column).lower(): str(column) for column in frame.columns}
    for target, options in aliases.items():
        source = None
        for alias in options:
            source = lower_map.get(str(alias).lower())
            if source is not None:
                break
        if source is not None:
            normalized[target] = pd.to_numeric(frame[source], errors="coerce")
    if "close" not in normalized:
        normalized["close"] = 0.0
    normalized["open"] = normalized.get("open", normalized["close"]).fillna(normalized["close"])
    normalized["high"] = normalized.get("high", normalized["close"]).fillna(normalized["close"])
    normalized["low"] = normalized.get("low", normalized["close"]).fillna(normalized["close"])
    normalized["volume"] = normalized.get("volume", 0.0).fillna(0.0)
    if "turnover_rate" in frame.columns:
        normalized["turnover_rate"] = pd.to_numeric(frame["turnover_rate"], errors="coerce")
    elif "turnover" in frame.columns:
        normalized["turnover_rate"] = pd.to_numeric(frame["turnover"], errors="coerce")
    return normalized.astype(float)


def build_close_volume_frame(closes: np.ndarray, volumes: Optional[np.ndarray] = None) -> pd.DataFrame:
    close_arr = np.asarray([] if closes is None else closes, dtype=float)
    volume_arr = np.asarray(volumes if volumes is not None else np.zeros(len(close_arr)), dtype=float)
    if len(volume_arr) != len(close_arr):
        volume_arr = np.resize(volume_arr, len(close_arr)) if len(close_arr) else np.array([], dtype=float)
    frame = pd.DataFrame({
        "open": close_arr,
        "high": close_arr,
        "low": close_arr,
        "close": close_arr,
        "volume": volume_arr,
        "turnover_rate": np.zeros(len(close_arr), dtype=float),
    })
    return frame.astype(float)


def normalize_strategy_dsl(dsl: dict[str, Any]) -> dict[str, Any]:
    payload = dict(dsl or {})
    entry_payload = payload.get("entry")
    exit_payload = payload.get("exit")
    signals_payload = dict(payload.get("signals") or {})
    if not entry_payload and isinstance(signals_payload, dict):
        entry_payload = _coerce_open_dsl_signal(signals_payload.get("entry"))
    if not exit_payload and isinstance(signals_payload, dict):
        exit_payload = _coerce_open_dsl_signal(signals_payload.get("exit"))
    entry = _normalize_condition(_coerce_open_dsl_signal(entry_payload))
    if not entry:
        raise ValueError("dsl.entry is required")
    exit_rule = _normalize_condition(_coerce_open_dsl_signal(exit_payload) or {
        "any": [{
            "op": "cross_below",
            "left": {"indicator": "sma", "field": "close", "window": 5},
            "right": {"indicator": "sma", "field": "close", "window": 20},
        }],
    })
    return {
        "version": str(payload.get("version") or SUPPORTED_DSL_VERSION),
        "timeframe": str(payload.get("timeframe") or "daily"),
        "entry": entry,
        "exit": exit_rule,
        "metadata": dict(payload.get("metadata") or {}),
        "risk_rules": dict(payload.get("risk_rules") or {}),
    }


def _coerce_open_dsl_signal(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    op = str(node.get("op") or "").strip().lower()
    conditions = list(node.get("conditions") or [])
    if op in {"all", "any"} and conditions:
        return {
            op: [
                _normalize_condition(dict(item or {}))
                for item in conditions
                if _normalize_condition(dict(item or {}))
            ]
        }
    return dict(node)


def _normalize_claims_for_mapping(prediction_contract: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for item in list(dict(prediction_contract or {}).get("claims") or []):
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claim_id") or item.get("id") or "").strip()
        if not claim_id:
            continue
        claims.append(
            {
                **item,
                "claim_id": claim_id,
                "evidence_ids": [str(value).strip() for value in list(item.get("evidence_ids") or []) if str(value).strip()],
            }
        )
    return claims


def _normalize_trade_plan_nodes_for_mapping(trade_plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def _append(node: Any, *, phase: str, index: int = 0) -> None:
        if not isinstance(node, dict):
            return
        node_id = str(
            node.get("node_id")
            or node.get("plan_node_id")
            or node.get("trade_plan_node_id")
            or node.get("trade_plan_step_id")
            or node.get("id")
            or f"{phase}_{index}"
        ).strip()
        claim_ids = [str(item).strip() for item in list(node.get("claim_ids") or []) if str(item).strip()]
        nodes.append(
            {
                **node,
                "node_id": node_id,
                "phase": phase,
                "claim_ids": list(dict.fromkeys(claim_ids)),
            }
        )

    payload = dict(trade_plan or {})
    if isinstance(payload.get("entry"), dict):
        _append(payload.get("entry"), phase="entry")
    if isinstance(payload.get("exit"), dict):
        _append(payload.get("exit"), phase="exit")
    for phase_name in ("entries", "exits", "nodes", "steps"):
        for index, item in enumerate(list(payload.get(phase_name) or [])):
            resolved_phase = "entry" if phase_name == "entries" else "exit" if phase_name == "exits" else phase_name
            _append(item, phase=resolved_phase, index=index)
    if not nodes and payload:
        _append(payload, phase="node")
    return nodes


def _collect_trade_plan_refs_from_dsl(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        for key in (
            "trade_plan_node_id",
            "trade_plan_step_id",
            "plan_node_id",
            "mapped_trade_plan_node_id",
            "node_id",
        ):
            token = str(value.get(key) or "").strip()
            if token:
                refs.append(token)
        for child in value.values():
            refs.extend(_collect_trade_plan_refs_from_dsl(child))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_trade_plan_refs_from_dsl(item))
    return list(dict.fromkeys(refs))


def _build_claim_to_trade_plan_map(
    *,
    prediction_contract: dict[str, Any],
    trade_plan: dict[str, Any],
) -> dict[str, Any]:
    claims = _normalize_claims_for_mapping(prediction_contract)
    trade_plan_nodes = _normalize_trade_plan_nodes_for_mapping(trade_plan)
    claim_to_trade_step_ids: dict[str, list[str]] = {}
    trade_step_to_claim_ids: dict[str, list[str]] = {}
    for node in trade_plan_nodes:
        node_id = str(node.get("node_id") or "").strip()
        node_claim_ids = [str(item).strip() for item in list(node.get("claim_ids") or []) if str(item).strip()]
        if not node_id:
            continue
        trade_step_to_claim_ids[node_id] = list(dict.fromkeys(node_claim_ids))
        for claim_id in node_claim_ids:
            claim_to_trade_step_ids.setdefault(claim_id, []).append(node_id)
    claim_ids = [str(item.get("claim_id") or "").strip() for item in claims if str(item.get("claim_id") or "").strip()]
    for claim_id in claim_ids:
        claim_to_trade_step_ids.setdefault(claim_id, [])
    return {
        "claim_to_trade_step_ids": {
            key: list(dict.fromkeys(value))
            for key, value in claim_to_trade_step_ids.items()
        },
        "trade_step_to_claim_ids": trade_step_to_claim_ids,
        "mapped_claim_count": sum(1 for value in claim_to_trade_step_ids.values() if value),
        "unmapped_claim_ids": [key for key, value in claim_to_trade_step_ids.items() if not value],
        "trade_step_count": len(trade_step_to_claim_ids),
    }


def _build_trade_plan_to_dsl_map(
    *,
    trade_plan: dict[str, Any],
    dsl: dict[str, Any],
) -> dict[str, Any]:
    trade_plan_nodes = _normalize_trade_plan_nodes_for_mapping(trade_plan)
    entry_refs = set(_collect_trade_plan_refs_from_dsl(dict(dsl or {}).get("entry")))
    exit_refs = set(_collect_trade_plan_refs_from_dsl(dict(dsl or {}).get("exit")))
    trade_step_to_dsl_sections: dict[str, list[str]] = {}
    for node in trade_plan_nodes:
        node_id = str(node.get("node_id") or "").strip()
        if not node_id:
            continue
        sections: list[str] = []
        phase = str(node.get("phase") or "").strip().lower()
        if node_id in entry_refs or (phase == "entry" and isinstance(trade_plan.get("entry"), dict)):
            sections.append("entry")
        if node_id in exit_refs or (phase == "exit" and isinstance(trade_plan.get("exit"), dict)):
            sections.append("exit")
        trade_step_to_dsl_sections[node_id] = sections
    return {
        "trade_step_to_dsl_sections": trade_step_to_dsl_sections,
        "dsl_entry_trade_step_ids": sorted(entry_refs),
        "dsl_exit_trade_step_ids": sorted(exit_refs),
        "mapped_trade_step_count": sum(1 for value in trade_step_to_dsl_sections.values() if value),
        "unmapped_trade_step_ids": [key for key, value in trade_step_to_dsl_sections.items() if not value],
    }


def _build_semantic_contract_bundle(
    *,
    payload: dict[str, Any],
    dsl: dict[str, Any],
    dsl_support_audit: dict[str, Any],
    claim_to_trade_plan_map: dict[str, Any],
    trade_plan_to_dsl_map: dict[str, Any],
) -> dict[str, Any]:
    candidate = {
        **dict(payload or {}),
        "dsl": dict(dsl or {}),
        "dsl_support_audit": dict(dsl_support_audit or {}),
        "claim_to_trade_plan_map": dict(claim_to_trade_plan_map or {}),
        "trade_plan_to_dsl_map": dict(trade_plan_to_dsl_map or {}),
    }
    audit = dict(audit_candidate_semantic_contract(candidate) or {})
    return {
        "evidence_alignment_audit": audit,
        "evidence_alignment_score": float(audit.get("evidence_alignment_score") or 0.0),
        "semantic_integrity_score": float(audit.get("semantic_integrity_score") or 0.0),
        "hard_fail_reasons": [
            str(item).strip()
            for item in list(audit.get("hard_fail_reasons") or [])
            if str(item).strip()
        ],
    }


def compile_strategy_blueprint(
    blueprint: dict[str, Any],
    market_frame: Optional[pd.DataFrame] = None,
    tune_for_factory: bool = False,
) -> dict[str, Any]:
    payload = dict(blueprint or {})
    dsl_support_audit = inspect_strategy_dsl_support(payload.get("dsl") or payload)
    trade_plan = dict(payload.get("trade_plan") or {})
    prediction_contract = dict(payload.get("prediction_contract") or {})
    claim_to_trade_plan_map = _build_claim_to_trade_plan_map(
        prediction_contract=prediction_contract,
        trade_plan=trade_plan,
    )
    if payload.get("dsl") or payload.get("entry"):
        dsl = payload.get("dsl") or {
            "version": payload.get("version"),
            "timeframe": payload.get("timeframe"),
            "entry": payload.get("entry"),
            "exit": payload.get("exit"),
            "metadata": payload.get("metadata") or {},
            "risk_rules": payload.get("risk_rules") or {},
        }
        normalized = normalize_strategy_dsl(dsl)
        if tune_for_factory:
            _validate_factory_blueprint_contract(payload, normalized)
        tuning = {
            "applied": False,
            "selected_variant": "original",
            "before": summarize_dsl_activity(market_frame, normalized),
            "after": summarize_dsl_activity(market_frame, normalized),
            "variants_evaluated": 1,
        }
        if tune_for_factory and market_frame is not None and not market_frame.empty:
            normalized, tuning = tune_strategy_dsl(normalized, market_frame)
        risk_rules = dict(payload.get("risk_rules") or normalized.get("risk_rules") or {})
        trade_plan_to_dsl_map = _build_trade_plan_to_dsl_map(
            trade_plan=trade_plan,
            dsl=normalized,
        )
        semantic_contract_bundle = _build_semantic_contract_bundle(
            payload=payload,
            dsl=normalized,
            dsl_support_audit=dsl_support_audit,
            claim_to_trade_plan_map=claim_to_trade_plan_map,
            trade_plan_to_dsl_map=trade_plan_to_dsl_map,
        )
        compile_failure_reasons = []
        if int(dsl_support_audit.get("unsupported_rule_count") or 0) > 0:
            compile_failure_reasons.append("dsl_contains_unsupported_rules")
        if semantic_contract_bundle["hard_fail_reasons"]:
            compile_failure_reasons.extend(
                [
                    reason
                    for reason in list(semantic_contract_bundle["hard_fail_reasons"])
                    if reason not in compile_failure_reasons
                ]
            )
        return {
            "strategy_type": "dsl_rule",
            "params": {
                "dsl": normalized,
                "risk_rules": risk_rules,
            },
            "name": str(payload.get("name") or "外部 AI DSL 策略"),
            "description": str(payload.get("description") or payload.get("rationale") or "外部 AI 生成的 DSL 策略"),
            "tags": list(dict.fromkeys(list(payload.get("tags") or []) + ["dsl_rule"])),
            "metadata": {
                "rationale": payload.get("rationale"),
                "hypothesis": payload.get("hypothesis"),
                "holding_horizon": dict(payload.get("holding_horizon") or {}),
                "trade_plan": _structured_payload(payload.get("trade_plan")),
                "risk_rules": risk_rules,
                "position_sizing": _structured_payload(payload.get("position_sizing")),
                "execution_notes": payload.get("execution_notes"),
                "stock_pool": dict(payload.get("stock_pool") or {}),
                "target_symbols": list(_normalize_code_list(payload.get("target_symbols"), limit=12)),
                "dsl": normalized,
                "dsl_tuning": tuning,
                "dsl_activity": tuning.get("after") or summarize_dsl_activity(market_frame, normalized),
                "dsl_support_audit": dsl_support_audit,
                "claim_to_trade_plan_map": claim_to_trade_plan_map,
                "trade_plan_to_dsl_map": trade_plan_to_dsl_map,
                "evidence_alignment_audit": semantic_contract_bundle["evidence_alignment_audit"],
                "evidence_alignment_score": semantic_contract_bundle["evidence_alignment_score"],
                "semantic_integrity_score": semantic_contract_bundle["semantic_integrity_score"],
                "hard_fail_reasons": semantic_contract_bundle["hard_fail_reasons"],
                "unsupported_rule_count": int(dsl_support_audit.get("unsupported_rule_count") or 0),
                "compile_failure_reasons": compile_failure_reasons,
            },
        }

    strategy_type = str(payload.get("strategy_type") or "").strip()
    params = dict(payload.get("params") or {})
    if not strategy_type or not params:
        raise ValueError("strategy blueprint must contain dsl or strategy_type+params")
    if tune_for_factory:
        _validate_factory_blueprint_contract(payload)
    semantic_contract_bundle = _build_semantic_contract_bundle(
        payload=payload,
        dsl=dict(payload.get("dsl") or {}),
        dsl_support_audit=dsl_support_audit,
        claim_to_trade_plan_map=claim_to_trade_plan_map,
        trade_plan_to_dsl_map={},
    )
    return {
        "strategy_type": strategy_type,
        "params": params,
        "name": str(payload.get("name") or f"外部 AI {strategy_type} 策略"),
        "description": str(payload.get("description") or payload.get("rationale") or "外部 AI 生成策略"),
        "tags": list(payload.get("tags") or []),
        "metadata": {
            "rationale": payload.get("rationale"),
            "hypothesis": payload.get("hypothesis"),
            "holding_horizon": dict(payload.get("holding_horizon") or {}),
            "trade_plan": _structured_payload(payload.get("trade_plan")),
            "risk_rules": dict(payload.get("risk_rules") or {}),
            "position_sizing": _structured_payload(payload.get("position_sizing")),
            "execution_notes": payload.get("execution_notes"),
            "stock_pool": dict(payload.get("stock_pool") or {}),
            "target_symbols": list(_normalize_code_list(payload.get("target_symbols"), limit=12)),
            "dsl_support_audit": dsl_support_audit,
            "claim_to_trade_plan_map": claim_to_trade_plan_map,
            "trade_plan_to_dsl_map": {},
            "evidence_alignment_audit": semantic_contract_bundle["evidence_alignment_audit"],
            "evidence_alignment_score": semantic_contract_bundle["evidence_alignment_score"],
            "semantic_integrity_score": semantic_contract_bundle["semantic_integrity_score"],
            "hard_fail_reasons": semantic_contract_bundle["hard_fail_reasons"],
            "unsupported_rule_count": int(dsl_support_audit.get("unsupported_rule_count") or 0),
            "compile_failure_reasons": list(semantic_contract_bundle["hard_fail_reasons"]),
        },
    }


def evaluate_dsl_masks(frame: pd.DataFrame, dsl: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    normalized = normalize_strategy_dsl(dsl)
    entry_mask = _eval_condition(frame, normalized["entry"]).fillna(False).to_numpy(dtype=bool)
    exit_mask = _eval_condition(frame, normalized["exit"]).fillna(False).to_numpy(dtype=bool)
    return entry_mask, exit_mask


def summarize_dsl_activity(frame: Optional[pd.DataFrame], dsl: dict[str, Any]) -> dict[str, Any]:
    if frame is None or frame.empty:
        return {
            "entry_count": 0,
            "exit_count": 0,
            "active_days": 0,
            "overlap_count": 0,
            "score": 0.0,
        }
    normalized = normalize_strategy_dsl(dsl)
    entry_mask, exit_mask = evaluate_dsl_masks(frame, normalized)
    overlap = entry_mask & exit_mask
    entry_count = int(np.count_nonzero(entry_mask))
    exit_count = int(np.count_nonzero(exit_mask))
    active_days = int(np.count_nonzero(entry_mask | exit_mask))
    overlap_count = int(np.count_nonzero(overlap))
    return {
        "entry_count": entry_count,
        "exit_count": exit_count,
        "active_days": active_days,
        "overlap_count": overlap_count,
        "score": round(_dsl_activity_score(entry_count, exit_count, active_days, overlap_count), 4),
    }

# --- strategy_dsl_parts/specs.py ---
def tune_strategy_dsl(dsl: dict[str, Any], market_frame: Optional[pd.DataFrame]) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = normalize_strategy_dsl(dsl)
    before = summarize_dsl_activity(market_frame, normalized)
    if market_frame is None or market_frame.empty:
        return normalized, {
            "applied": False,
            "selected_variant": "original",
            "before": before,
            "after": before,
            "variants_evaluated": 1,
            "selection_basis": "activity_fallback",
            "primary_horizon": 5,
            "overall_skill": None,
            "recent_skill": None,
            "trade_expectancy": None,
            "sample_count": 0,
            "stability_gap": None,
        }

    variants: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("original", normalized, {"window_scale": 1.0, "threshold_scale": 1.0}),
    ]
    for scale in (0.85, 0.7, 0.55):
        scaled = _scale_dsl_windows(normalized, scale)
        variants.append((f"window_scale_{scale:.2f}", scaled, {"window_scale": scale, "threshold_scale": 1.0}))
    base_variants = list(variants)
    for base_name, base_dsl, base_meta in base_variants:
        for scale in (0.9, 0.75):
            relaxed = _relax_dsl_thresholds(base_dsl, scale)
            variants.append((
                f"{base_name}_threshold_scale_{scale:.2f}",
                relaxed,
                {**base_meta, "threshold_scale": scale},
            ))

    structural_variants = list(variants)
    for base_name, base_dsl, base_meta in structural_variants:
        softened = _soften_cross_operators(base_dsl)
        if softened != base_dsl:
            variants.append((
                f"{base_name}_soft_cross",
                softened,
                {**base_meta, "cross_mode": "state"},
            ))
            relaxed_groups = _soften_condition_groups(softened)
            if relaxed_groups != softened:
                variants.append((
                    f"{base_name}_soft_cross_relaxed_group",
                    relaxed_groups,
                    {**base_meta, "cross_mode": "state", "group_mode": "relaxed_any"},
                ))

    primary_horizon = _resolve_primary_horizon(dsl)
    predictive_ranked: list[
        tuple[tuple[float, float, float, float, int, float], str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]
    ] = []
    activity_ranked: list[tuple[tuple[float, int, int, int], str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for name, variant, meta in variants:
        stats = summarize_dsl_activity(market_frame, variant)
        predictive_stats = _summarize_variant_predictive_edge(
            market_frame,
            variant,
            primary_horizon=primary_horizon,
        )
        activity_rank = (
            float(stats.get("score") or 0.0),
            int(min(stats.get("entry_count") or 0, stats.get("exit_count") or 0)),
            int(stats.get("active_days") or 0),
            -int(stats.get("overlap_count") or 0),
        )
        activity_ranked.append((activity_rank, name, variant, meta, stats))
        predictive_rank = (
            float(min(
                predictive_stats.get("recent_skill")
                if predictive_stats.get("recent_skill") is not None
                else -999.0,
                predictive_stats.get("overall_skill")
                if predictive_stats.get("overall_skill") is not None
                else -999.0,
            )),
            float(predictive_stats.get("overall_skill") if predictive_stats.get("overall_skill") is not None else -999.0),
            float(predictive_stats.get("trade_expectancy") if predictive_stats.get("trade_expectancy") is not None else -999.0),
            -float(predictive_stats.get("stability_gap") if predictive_stats.get("stability_gap") is not None else 999.0),
            -int(stats.get("overlap_count") or 0),
            float(stats.get("score") or 0.0),
        )
        predictive_ranked.append((predictive_rank, name, variant, meta, stats, predictive_stats))
    activity_ranked.sort(key=lambda item: item[0], reverse=True)
    predictive_ranked.sort(key=lambda item: item[0], reverse=True)
    _, fallback_name, fallback_variant, fallback_meta, fallback_after = activity_ranked[0]
    fallback_metadata = {
        "applied": False,
        "selected_variant": "original",
        "before": before,
        "after": before,
        "variants_evaluated": len(activity_ranked),
        "selection_basis": "activity_fallback",
        "primary_horizon": primary_horizon,
        "overall_skill": None,
        "recent_skill": None,
        "trade_expectancy": None,
        "sample_count": 0,
        "stability_gap": None,
        **fallback_meta,
    }
    if not predictive_ranked:
        return normalized, fallback_metadata
    _, selected_name, selected_variant, selected_meta, after, predictive_after = predictive_ranked[0]
    if int(predictive_after.get("sample_count") or 0) < 20:
        return normalized, fallback_metadata
    return selected_variant, {
        "applied": selected_name != "original",
        "selected_variant": selected_name,
        "before": before,
        "after": after,
        "variants_evaluated": len(predictive_ranked),
        "selection_basis": "predictive_edge",
        "primary_horizon": primary_horizon,
        "overall_skill": predictive_after.get("overall_skill"),
        "recent_skill": predictive_after.get("recent_skill"),
        "trade_expectancy": predictive_after.get("trade_expectancy"),
        "sample_count": predictive_after.get("sample_count"),
        "stability_gap": predictive_after.get("stability_gap"),
        **selected_meta,
    }


def _resolve_primary_horizon(dsl: dict[str, Any]) -> int:
    metadata = dict((dsl or {}).get("metadata") or {})
    raw_horizon = metadata.get("holding_horizon_days")
    if raw_horizon is None and isinstance(metadata.get("holding_horizon"), dict):
        raw_horizon = dict(metadata.get("holding_horizon") or {}).get("max_days")
    horizon = int(raw_horizon or 5)
    candidates = [5, 10, 20]
    return min(candidates, key=lambda item: abs(item - horizon))


def _summarize_variant_predictive_edge(
    frame: Optional[pd.DataFrame],
    dsl: dict[str, Any],
    *,
    primary_horizon: int,
) -> dict[str, Any]:
    if frame is None or frame.empty or "close" not in frame.columns:
        return {
            "overall_skill": None,
            "recent_skill": None,
            "trade_expectancy": None,
            "sample_count": 0,
            "stability_gap": None,
        }
    normalized = normalize_strategy_dsl(dsl)
    entry_mask, _exit_mask = evaluate_dsl_masks(frame, normalized)
    closes = pd.to_numeric(frame["close"], errors="coerce")
    forward_returns = (closes.shift(-int(primary_horizon)) / closes) - 1.0
    valid_mask = entry_mask & forward_returns.notna().to_numpy(dtype=bool)
    samples = [float(value) for value in forward_returns[valid_mask].tolist() if pd.notna(value)]
    sample_count = len(samples)
    if sample_count <= 0:
        return {
            "overall_skill": None,
            "recent_skill": None,
            "trade_expectancy": None,
            "sample_count": 0,
            "stability_gap": None,
        }
    split = int(max(1, np.floor(sample_count * 0.7)))
    recent_samples = samples[split:] if split < sample_count else samples[-max(1, min(sample_count, int(np.ceil(sample_count * 0.3)))) :]
    overall_skill = round(float(np.mean(samples)), 6)
    recent_skill = round(float(np.mean(recent_samples)), 6) if recent_samples else overall_skill
    trade_expectancy = overall_skill
    stability_gap = round(abs(recent_skill - overall_skill), 6)
    return {
        "overall_skill": overall_skill,
        "recent_skill": recent_skill,
        "trade_expectancy": trade_expectancy,
        "sample_count": sample_count,
        "stability_gap": stability_gap,
    }


def _dsl_activity_score(entry_count: int, exit_count: int, active_days: int, overlap_count: int) -> float:
    return (
        _count_band_score(entry_count)
        + _count_band_score(exit_count)
        + min(active_days, 24) / 24.0
        - min(overlap_count, 6) * 0.25
    )


def _count_band_score(count: int) -> float:
    if count <= 0:
        return 0.0
    if 2 <= count <= 18:
        return 2.5
    if count < 2:
        return float(count)
    return max(0.5, 18.0 / float(count))


def _scale_dsl_windows(dsl: dict[str, Any], scale: float) -> dict[str, Any]:
    payload = deepcopy(normalize_strategy_dsl(dsl))
    payload["entry"] = _transform_condition(payload.get("entry") or {}, expr_transform=lambda expr: _scale_expr_window(expr, scale))
    payload["exit"] = _transform_condition(payload.get("exit") or {}, expr_transform=lambda expr: _scale_expr_window(expr, scale))
    return normalize_strategy_dsl(payload)


def _relax_dsl_thresholds(dsl: dict[str, Any], scale: float) -> dict[str, Any]:
    payload = deepcopy(normalize_strategy_dsl(dsl))
    payload["entry"] = _transform_condition(payload.get("entry") or {}, condition_transform=lambda cond: _relax_condition_threshold(cond, scale))
    payload["exit"] = _transform_condition(payload.get("exit") or {}, condition_transform=lambda cond: _relax_condition_threshold(cond, scale))
    return normalize_strategy_dsl(payload)


def _transform_condition(
    node: dict[str, Any],
    *,
    expr_transform=None,
    condition_transform=None,
) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    if "all" in node:
        return {"all": [_transform_condition(item, expr_transform=expr_transform, condition_transform=condition_transform) for item in list(node.get("all") or [])]}
    if "any" in node:
        return {"any": [_transform_condition(item, expr_transform=expr_transform, condition_transform=condition_transform) for item in list(node.get("any") or [])]}
    if "not" in node:
        return {"not": _transform_condition(dict(node.get("not") or {}), expr_transform=expr_transform, condition_transform=condition_transform)}
    transformed = {
        "op": str(node.get("op") or "").strip().lower(),
        "left": _transform_expr(dict(node.get("left") or {}), transform=expr_transform),
        "right": _transform_expr(dict(node.get("right") or {}), transform=expr_transform),
    }
    return condition_transform(transformed) if callable(condition_transform) else transformed


def _transform_expr(node: dict[str, Any], *, transform=None) -> dict[str, Any]:
    expr = dict(node or {})
    binary = expr.get("binary")
    if isinstance(binary, dict):
        expr["binary"] = {
            "op": str(binary.get("op") or "").strip().lower(),
            "left": _transform_expr(dict(binary.get("left") or {}), transform=transform),
            "right": _transform_expr(dict(binary.get("right") or {}), transform=transform),
        }
    if callable(transform):
        expr = transform(expr)
    return expr


def _scale_expr_window(expr: dict[str, Any], scale: float) -> dict[str, Any]:
    payload = dict(expr or {})
    indicator = str(payload.get("indicator") or "").strip().lower()
    if indicator in SUPPORTED_INDICATORS:
        window = int(payload.get("window") or 14)
        min_window, max_window = _indicator_window_bounds(indicator)
        scaled = int(round(window * float(scale or 1.0)))
        payload["window"] = max(min_window, min(max_window, scaled))
    return payload


def _indicator_window_bounds(indicator: str) -> tuple[int, int]:
    if indicator == "rsi":
        return 5, 21
    if indicator in {"volume_ratio", "turnover_rate"}:
        return 3, 20
    if indicator in {"roc", "stddev", "zscore", "atr", "adx", "rolling_count", "slope"}:
        return 3, 30
    if indicator in {"highest", "lowest"}:
        return 5, 40
    return 3, 40


def _relax_condition_threshold(cond: dict[str, Any], scale: float) -> dict[str, Any]:
    payload = dict(cond or {})
    op = str(payload.get("op") or "").strip().lower()
    right = dict(payload.get("right") or {})
    if "value" in right:
        baseline = _expr_neutral_value(payload.get("left") or {})
        right["value"] = round(_relax_threshold_value(float(right.get("value") or 0.0), baseline, op, scale), 6)
        payload["right"] = right
    return payload


def _soften_cross_operators(dsl: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(normalize_strategy_dsl(dsl))
    payload["entry"] = _transform_condition(payload.get("entry") or {}, condition_transform=_soften_cross_condition)
    payload["exit"] = _transform_condition(payload.get("exit") or {}, condition_transform=_soften_cross_condition)
    return normalize_strategy_dsl(payload)


def _soften_cross_condition(cond: dict[str, Any]) -> dict[str, Any]:
    payload = dict(cond or {})
    op = str(payload.get("op") or "").strip().lower()
    if op == "cross_above":
        payload["op"] = "gt"
    elif op == "cross_below":
        payload["op"] = "lt"
    return payload


def _soften_condition_groups(dsl: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(normalize_strategy_dsl(dsl))
    payload["entry"] = _soften_group_node(payload.get("entry") or {})
    payload["exit"] = _soften_group_node(payload.get("exit") or {})
    return normalize_strategy_dsl(payload)


def _soften_group_node(node: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    if "all" in node:
        items = [_soften_group_node(item) for item in list(node.get("all") or [])]
        if len(items) > 1:
            return {"any": items}
        return {"all": items}
    if "any" in node:
        return {"any": [_soften_group_node(item) for item in list(node.get("any") or [])]}
    if "not" in node:
        return {"not": _soften_group_node(dict(node.get("not") or {}))}
    return dict(node)


def _expr_neutral_value(expr: dict[str, Any]) -> float:
    indicator = str((expr or {}).get("indicator") or "").strip().lower()
    if indicator in {"volume_ratio", "turnover_rate"}:
        return 1.0
    if indicator == "rsi":
        return 50.0
    if indicator == "adx":
        return 20.0
    if indicator in {"roc", "zscore", "stddev", "atr", "upper_shadow_ratio", "slope", "rolling_count"}:
        return 0.0
    return 0.0


def _relax_threshold_value(value: float, baseline: float, op: str, scale: float) -> float:
    factor = float(scale or 1.0)
    if op in {"gt", "gte"} and value > baseline:
        return baseline + (value - baseline) * factor
    if op in {"lt", "lte"} and value < baseline:
        return baseline + (value - baseline) * factor
    return value


def _expand_shorthand_condition(node: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    for op in SUPPORTED_COMPARE_OPS:
        if op not in node:
            continue
        payload = node.get(op)
        if isinstance(payload, (list, tuple)) and len(payload) >= 2:
            return {
                'op': op,
                'left': _normalize_expr(payload[0]),
                'right': _normalize_expr(payload[1]),
            }
        if isinstance(payload, dict):
            left = payload.get('left') if 'left' in payload else payload.get('a')
            right = payload.get('right') if 'right' in payload else payload.get('b')
            return {
                'op': op,
                'left': _normalize_expr(left),
                'right': _normalize_expr(right),
            }
    return {}


def _expand_shorthand_expr(node: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    for indicator in SUPPORTED_INDICATORS:
        if indicator not in node:
            continue
        payload = node.get(indicator)
        if isinstance(payload, dict):
            field = str(payload.get('field') or 'close').strip().lower() or 'close'
            if field not in SUPPORTED_FIELDS:
                field = 'close'
            result = {
                'indicator': indicator,
                'field': field,
                'window': max(1, int(payload.get('window') or payload.get('period') or 14)),
            }
            if indicator == "slope":
                result["lookback"] = max(1, int(payload.get("lookback") or payload.get("lag") or 5))
            if indicator == "rolling_count":
                result["condition"] = _normalize_condition(payload.get("condition"))
            return result
        if isinstance(payload, (int, float)):
            return {
                'indicator': indicator,
                'field': 'close',
                'window': max(1, int(payload or 14)),
            }
        if isinstance(payload, str):
            try:
                window = max(1, int(float(payload)))
                return {'indicator': indicator, 'field': 'close', 'window': window}
            except Exception as _swallowed:
                logger.warning("packages/aiask-quant-core/src/aiask_quant_core/strategy_dsl.py:976: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
            field = payload.strip().lower()
            if field in SUPPORTED_FIELDS:
                return {'indicator': indicator, 'field': field, 'window': 14}
    for op in SUPPORTED_BINARY_OPS:
        if op not in node:
            continue
        payload = node.get(op)
        if isinstance(payload, (list, tuple)) and len(payload) >= 2:
            return {
                'binary': {
                    'op': op,
                    'left': _normalize_expr(payload[0]),
                    'right': _normalize_expr(payload[1]),
                }
            }
        if isinstance(payload, dict):
            left = payload.get('left') if 'left' in payload else payload.get('a')
            right = payload.get('right') if 'right' in payload else payload.get('b')
            return {
                'binary': {
                    'op': op,
                    'left': _normalize_expr(left),
                    'right': _normalize_expr(right),
                }
            }
    return {}


def _normalize_condition(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    if "all" in node:
        items = []
        for item in list(node.get("all") or []):
            normalized = _normalize_condition(item)
            if normalized:
                items.append(normalized)
        return {"all": items}
    if "any" in node:
        items = []
        for item in list(node.get("any") or []):
            normalized = _normalize_condition(item)
            if normalized:
                items.append(normalized)
        return {"any": items}
    if "not" in node:
        child = _normalize_condition(node.get("not"))
        return {"not": child} if child else {}
    op = str(node.get("op") or "").strip().lower()
    if op in SUPPORTED_COMPARE_OPS:
        return {
            "op": op,
            "left": _normalize_expr(node.get("left")),
            "right": _normalize_expr(node.get("right")),
        }
    return _expand_shorthand_condition(node)


def _normalize_expr(node: Any) -> dict[str, Any]:
    if isinstance(node, (int, float)):
        return {"value": float(node)}
    if isinstance(node, str):
        text = node.strip().lower()
        if text in SUPPORTED_FIELDS:
            return {"field": text}
        try:
            return {"value": float(text)}
        except Exception:
            return {"field": "close"}
    if not isinstance(node, dict):
        return {"value": 0.0}
    if "value" in node:
        return {"value": float(node.get("value") or 0.0)}
    indicator = str(node.get("indicator") or "").strip().lower()
    if indicator in SUPPORTED_INDICATORS:
        field_name = str(node.get("field") or "close").strip().lower() or "close"
        if field_name not in SUPPORTED_FIELDS:
            field_name = "close"
        normalized = {
            "indicator": indicator,
            "field": field_name,
            "window": max(1, int(node.get("window") or node.get("period") or 14)),
        }
        if indicator == "slope":
            normalized["lookback"] = max(1, int(node.get("lookback") or node.get("lag") or 5))
        if indicator == "rolling_count":
            normalized["condition"] = _normalize_condition(node.get("condition"))
        return normalized
    field = str(node.get("field") or node.get("column") or "").strip().lower()
    if field in SUPPORTED_FIELDS:
        return {"field": field}
    shorthand_expr = _expand_shorthand_expr(node)
    if shorthand_expr:
        return shorthand_expr
    if "binary" in node and isinstance(node.get("binary"), dict):
        node = node.get("binary")
    op = str(node.get("op") or "").strip().lower()
    if op in SUPPORTED_BINARY_OPS and "left" in node and "right" in node:
        return {
            "binary": {
                "op": op,
                "left": _normalize_expr(node.get("left")),
                "right": _normalize_expr(node.get("right")),
            }
        }
    return {"field": "close"}


def _eval_condition(frame: pd.DataFrame, node: dict[str, Any]) -> pd.Series:
    if not node:
        return pd.Series(False, index=frame.index)
    if "all" in node:
        items = list(node.get("all") or [])
        if not items:
            return pd.Series(False, index=frame.index)
        result = pd.Series(True, index=frame.index)
        for item in items:
            result &= _eval_condition(frame, item)
        return result
    if "any" in node:
        items = list(node.get("any") or [])
        if not items:
            return pd.Series(False, index=frame.index)
        result = pd.Series(False, index=frame.index)
        for item in items:
            result |= _eval_condition(frame, item)
        return result
    if "not" in node:
        return ~_eval_condition(frame, dict(node.get("not") or {}))

    left = _eval_expr(frame, dict(node.get("left") or {}))
    right = _eval_expr(frame, dict(node.get("right") or {}))
    op = str(node.get("op") or "").strip().lower()
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "eq":
        return pd.Series(np.isclose(left, right), index=frame.index)
    if op == "ne":
        return pd.Series(~np.isclose(left, right), index=frame.index)
    if op == "cross_above":
        return (left.shift(1) <= right.shift(1)) & (left > right)
    if op == "cross_below":
        return (left.shift(1) >= right.shift(1)) & (left < right)
    return pd.Series(False, index=frame.index)

# --- strategy_dsl_parts/runtime.py ---
def _eval_expr(frame: pd.DataFrame, node: dict[str, Any]) -> pd.Series:
    if "value" in node:
        return pd.Series(float(node.get("value") or 0.0), index=frame.index, dtype=float)
    indicator = str(node.get("indicator") or "").strip().lower()
    if indicator in SUPPORTED_INDICATORS:
        series = _eval_expr(frame, {"field": node.get("field") or "close"})
        window = max(1, int(node.get("window") or 14))
        if indicator == "sma":
            return series.rolling(window).mean()
        if indicator == "ema":
            return series.ewm(span=window, adjust=False).mean()
        if indicator == "roc":
            return series.pct_change(window)
        if indicator == "rsi":
            delta = series.diff()
            up = delta.clip(lower=0.0)
            down = -delta.clip(upper=0.0)
            avg_gain = up.rolling(window).mean()
            avg_loss = down.rolling(window).mean()
            rs = avg_gain / np.maximum(avg_loss, 1e-9)
            return 100.0 - (100.0 / (1.0 + rs))
        if indicator == "stddev":
            return series.rolling(window).std()
        if indicator == "zscore":
            mean = series.rolling(window).mean()
            std = series.rolling(window).std()
            return (series - mean) / np.maximum(std, 1e-9)
        if indicator == "highest":
            return series.rolling(window).max()
        if indicator == "lowest":
            return series.rolling(window).min()
        if indicator == "volume_ratio":
            volume = _eval_expr(frame, {"field": "volume"})
            return volume / np.maximum(volume.rolling(window).mean(), 1e-9)
        if indicator == "turnover_rate":
            turnover = pd.to_numeric(frame.get("turnover_rate", pd.Series(np.nan, index=frame.index)), errors="coerce")
            if turnover.notna().any():
                return turnover.fillna(0.0)
            volume = _eval_expr(frame, {"field": "volume"})
            baseline = volume.rolling(window).median()
            return volume / np.maximum(baseline, 1e-9)
        if indicator == "upper_shadow_ratio":
            high = _eval_expr(frame, {"field": "high"})
            open_ = _eval_expr(frame, {"field": "open"})
            close = _eval_expr(frame, {"field": "close"})
            low = _eval_expr(frame, {"field": "low"})
            spread = (high - low).abs().clip(lower=1e-9)
            upper_shadow = (high - pd.concat([open_, close], axis=1).max(axis=1)).clip(lower=0.0)
            return upper_shadow / spread
        if indicator == "atr":
            high = _eval_expr(frame, {"field": "high"})
            low = _eval_expr(frame, {"field": "low"})
            close = _eval_expr(frame, {"field": "close"})
            prev_close = close.shift(1)
            tr = pd.concat([
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1)
            return tr.rolling(window).mean()
        if indicator == "adx":
            high = _eval_expr(frame, {"field": "high"})
            low = _eval_expr(frame, {"field": "low"})
            close = _eval_expr(frame, {"field": "close"})
            up_move = high.diff()
            down_move = -low.diff()
            plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
            minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
            prev_close = close.shift(1)
            tr = pd.concat([
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr = tr.rolling(window).mean().replace(0.0, np.nan)
            plus_di = 100.0 * plus_dm.rolling(window).sum() / atr
            minus_di = 100.0 * minus_dm.rolling(window).sum() / atr
            dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)) * 100.0
            return dx.rolling(window).mean().fillna(0.0)
        if indicator == "rolling_count":
            condition = _normalize_condition(node.get("condition"))
            mask = _eval_condition(frame, condition).astype(float)
            return mask.rolling(window).sum().fillna(0.0)
        if indicator == "slope":
            smoothed = series.rolling(window).mean()
            lookback = max(1, int(node.get("lookback") or 5))
            return smoothed - smoothed.shift(lookback)
    field = str(node.get("field") or "").strip().lower()
    if field in SUPPORTED_FIELDS:
        return pd.to_numeric(frame.get(field, pd.Series(0.0, index=frame.index)), errors="coerce").fillna(0.0)
    binary = node.get("binary")
    if isinstance(binary, dict):
        left = _eval_expr(frame, dict(binary.get("left") or {}))
        right = _eval_expr(frame, dict(binary.get("right") or {}))
        op = str(binary.get("op") or "").strip().lower()
        if op == "add":
            return left + right
        if op == "sub":
            return left - right
        if op == "mul":
            return left * right
        if op == "div":
            denom = right.abs().clip(lower=1e-9)
            return left / denom
        if op == "max":
            return pd.concat([left, right], axis=1).max(axis=1)
        if op == "min":
            return pd.concat([left, right], axis=1).min(axis=1)
    return pd.Series(0.0, index=frame.index, dtype=float)

