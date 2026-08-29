"""Shared strategy explanation helpers.

The helpers in this module turn the many low-level generation fields carried by
AIASK strategies into a compact, stable, human-facing explanation contract.
They intentionally avoid any MCP, Agent, database, or Strategy Factory imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


EXPLANATION_VERSION = "strategy_explanation.v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _first(*values: Any) -> Any:
    for value in values:
        if _non_empty(value):
            return value
    return None


def _text(value: Any, *, limit: int = 360) -> str | None:
    if not _non_empty(value):
        return None
    if isinstance(value, Mapping):
        for key in ("summary", "rationale", "reason", "description", "thesis", "note"):
            nested = _text(value.get(key), limit=limit)
            if nested:
                return nested
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _string_list(*values: Any, limit: int = 12) -> list[str]:
    result: list[str] = []

    def visit(value: Any) -> None:
        if not _non_empty(value) or len(result) >= limit:
            return
        if isinstance(value, Mapping):
            for key in ("symbols", "codes", "stock_codes", "target_symbols", "tags", "labels"):
                visit(value.get(key))
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        token = str(value).strip()
        if token and token not in result:
            result.append(token)

    for value in values:
        visit(value)
    return result


def _compact(value: Any, *, depth: int = 2, max_items: int = 10, max_list: int = 8) -> Any:
    if depth <= 0:
        if isinstance(value, Mapping):
            return {"_omitted_keys": len(value)}
        if isinstance(value, (list, tuple, set)):
            return {"_omitted_items": len(value)}
        return _text(value, limit=160) if isinstance(value, str) else value
    if isinstance(value, Mapping):
        compacted: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                compacted["_omitted_keys"] = len(value) - max_items
                break
            if not _non_empty(item):
                continue
            compacted[str(key)] = _compact(item, depth=depth - 1, max_items=max_items, max_list=max_list)
        return compacted
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        compacted_list = [
            _compact(item, depth=depth - 1, max_items=max_items, max_list=max_list)
            for item in items[:max_list]
            if _non_empty(item)
        ]
        if len(items) > max_list:
            compacted_list.append({"_omitted_items": len(items) - max_list})
        return compacted_list
    return _text(value, limit=240) if isinstance(value, str) else value


def _compact_keys(value: Any, keys: tuple[str, ...], *, depth: int = 2) -> dict[str, Any]:
    payload = _mapping(value)
    return {
        key: _compact(payload.get(key), depth=depth)
        for key in keys
        if _non_empty(payload.get(key))
    }


def _metric_summary(metrics: Any) -> dict[str, Any]:
    payload = _mapping(metrics)
    if not payload:
        return {}
    keys = (
        "sharpe_ratio",
        "total_return",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "trade_count",
        "trades_count",
        "validation_grade",
        "validation_total_score",
    )
    return {key: payload.get(key) for key in keys if _non_empty(payload.get(key))}


def _format_pct(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:.1%}"


def _format_float(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:.2f}"


def _label(prefix: str, value: Any) -> str | None:
    token = str(value or "").strip().lower().replace(" ", "_")
    return f"{prefix}:{token}" if token else None


def _resolve_horizon_label(holding_horizon: dict[str, Any]) -> str | None:
    max_days = holding_horizon.get("max_days") or holding_horizon.get("max_holding_days")
    min_days = holding_horizon.get("min_days")
    if max_days and min_days:
        return f"horizon:{min_days}-{max_days}d"
    if max_days:
        return f"horizon:max_{max_days}d"
    return None


def _build_summary(
    *,
    name: str | None,
    strategy_type: str | None,
    thesis: str | None,
    target_symbols: list[str],
    generator_type: str | None,
) -> str:
    if thesis:
        return thesis
    subject = name or strategy_type or "strategy"
    target = f" for {len(target_symbols)} target symbol(s)" if target_symbols else ""
    generator = f" generated by {generator_type}" if generator_type else ""
    return f"{subject} is a {strategy_type or 'trading'} candidate{target}{generator}."


def _build_why(
    *,
    generation_reason: dict[str, Any],
    research_task: dict[str, Any],
    event_context: dict[str, Any],
    thesis: str | None,
    target_symbols: list[str],
    generator_type: str | None,
    source: str | None,
) -> str:
    parts: list[str] = []
    source_value = _first(generation_reason.get("source"), source, generator_type)
    if source_value:
        parts.append(f"source={source_value}")
    provider = generation_reason.get("provider")
    if provider and provider != source_value:
        parts.append(f"provider={provider}")
    model = generation_reason.get("model")
    if model:
        parts.append(f"model={model}")
    category = _first(generation_reason.get("category"), research_task.get("candidate_family"))
    if category:
        parts.append(f"category={category}")
    opportunity = _first(research_task.get("opportunity_type"), event_context.get("event_type"))
    if opportunity:
        parts.append(f"opportunity={opportunity}")
    theme = _first(research_task.get("theme"), research_task.get("theme_name"), event_context.get("theme_name"))
    if theme:
        parts.append(f"theme={theme}")
    rationale = _text(
        _first(
            generation_reason.get("rationale"),
            research_task.get("rationale"),
            event_context.get("event_summary"),
            thesis,
        ),
        limit=220,
    )
    if rationale:
        parts.append(f"rationale={rationale}")
    fallback = generation_reason.get("fallback_reason")
    if fallback:
        parts.append(f"fallback={fallback}")
    if target_symbols:
        parts.append(f"targets={','.join(target_symbols[:6])}")
    return "; ".join(parts)


def build_strategy_explanation(
    strategy: Mapping[str, Any] | None,
    *,
    metrics: Mapping[str, Any] | None = None,
    existing: Mapping[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Build a compact explanation contract for a generated strategy.

    The returned shape is append-only by convention: callers can safely persist
    it under ``params.strategy_explanation`` and surface it in domain events.
    """

    payload = _mapping(strategy)
    params = _mapping(payload.get("params"))
    existing_payload = _mapping(existing)
    existing_params = _mapping(existing_payload.get("params"))
    inherited = _mapping(
        _first(
            payload.get("strategy_explanation"),
            params.get("strategy_explanation"),
            existing_payload.get("strategy_explanation"),
            existing_params.get("strategy_explanation"),
        )
    )
    generation_reason = _mapping(
        _first(payload.get("generation_reason"), params.get("generation_reason"), inherited.get("generation_reason"))
    )
    research_task = _mapping(_first(payload.get("research_task"), params.get("research_task")))
    event_context = _mapping(_first(payload.get("event_context"), params.get("event_context")))
    trade_plan = _mapping(_first(payload.get("trade_plan"), params.get("trade_plan")))
    risk_rules = _mapping(_first(payload.get("risk_rules"), params.get("risk_rules")))
    holding_horizon = _mapping(_first(payload.get("holding_horizon"), params.get("holding_horizon")))
    validation_profile = _mapping(_first(payload.get("validation_profile"), params.get("validation_profile")))
    constraint_check = _mapping(_first(payload.get("constraint_check"), params.get("constraint_check")))
    stock_pool = _mapping(_first(payload.get("stock_pool"), params.get("stock_pool"), generation_reason.get("stock_pool")))
    metrics_summary = _metric_summary(metrics or payload.get("backtest_metrics") or params.get("backtest_metrics"))

    name = _text(_first(payload.get("name"), existing_payload.get("name")), limit=120)
    description = _text(_first(payload.get("description"), existing_payload.get("description")), limit=360)
    strategy_type = _text(_first(payload.get("strategy_type"), params.get("strategy_type")), limit=80)
    generator_type = _text(
        _first(
            payload.get("generator_type"),
            params.get("generator_type"),
            generation_reason.get("engine"),
            generation_reason.get("provider"),
            source,
        ),
        limit=120,
    )
    thesis = _text(
        _first(
            payload.get("hypothesis"),
            params.get("hypothesis"),
            generation_reason.get("rationale"),
            research_task.get("rationale"),
            description,
        ),
        limit=360,
    )
    candidate_family = _text(
        _first(
            payload.get("candidate_family"),
            params.get("candidate_family"),
            research_task.get("candidate_family"),
            strategy_type,
        ),
        limit=80,
    )
    risk_level = _text(
        _first(payload.get("risk_level"), params.get("risk_level"), generation_reason.get("risk_level")),
        limit=80,
    )
    target_symbols = _string_list(
        payload.get("target_symbols"),
        params.get("target_symbols"),
        stock_pool,
        generation_reason.get("target_symbols"),
        limit=12,
    )
    selection_logic = _string_list(
        payload.get("selection_logic"),
        params.get("selection_logic"),
        generation_reason.get("selection_logic"),
        limit=8,
    )

    summary = _text(inherited.get("summary"), limit=360) or description or _build_summary(
        name=name,
        strategy_type=strategy_type,
        thesis=thesis,
        target_symbols=target_symbols,
        generator_type=generator_type,
    )
    why_generated = _text(inherited.get("why_generated"), limit=420) or _build_why(
        generation_reason=generation_reason,
        research_task=research_task,
        event_context=event_context,
        thesis=thesis,
        target_symbols=target_symbols,
        generator_type=generator_type,
        source=source,
    )

    labels = _string_list(
        inherited.get("labels"),
        payload.get("tags"),
        params.get("tags"),
        existing_payload.get("tags"),
        [
            "strategy_explained",
            _label("type", strategy_type),
            _label("family", candidate_family),
            _label("generator", generator_type),
            _label("risk", risk_level),
            _resolve_horizon_label(holding_horizon),
            "targeted_universe" if target_symbols else None,
            _label("source", source),
        ],
        limit=18,
    )

    result = {
        **inherited,
        "version": EXPLANATION_VERSION,
        "summary": summary,
        "labels": labels,
        "strategy_type": strategy_type,
        "strategy_family": candidate_family,
        "generator": {
            key: value
            for key, value in {
                "type": generator_type,
                "source": source,
                "provider": generation_reason.get("provider"),
                "model": generation_reason.get("model"),
            }.items()
            if _non_empty(value)
        },
        "why_generated": why_generated,
        "thesis": thesis,
        "target_scope": {
            key: value
            for key, value in {
                "symbols": target_symbols,
                "symbol_count": len(target_symbols),
                "stock_pool": _compact_keys(stock_pool, ("selection_mode", "symbols", "filters", "rationale")),
                "theme": _first(research_task.get("theme"), research_task.get("theme_name"), event_context.get("theme_name")),
                "opportunity_type": research_task.get("opportunity_type"),
                "validation_focus": validation_profile.get("validation_focus"),
            }.items()
            if _non_empty(value)
        },
        "signal_logic": {
            key: value
            for key, value in {
                "selection_logic": selection_logic,
                "entry": _first(trade_plan.get("entry_bias"), trade_plan.get("entry_rule"), trade_plan.get("entry_policy")),
                "exit": _first(trade_plan.get("exit_bias"), trade_plan.get("exit_rule"), trade_plan.get("exit_policy")),
                "rebalance": _compact(_first(payload.get("rebalance_rule"), params.get("rebalance_rule")), depth=1),
                "holding_horizon": _compact(holding_horizon, depth=1),
            }.items()
            if _non_empty(value)
        },
        "risk_notes": {
            key: value
            for key, value in {
                "risk_level": risk_level,
                "risk_rules": _compact(risk_rules, depth=1),
                "failure_mode": _compact(_first(payload.get("failure_mode"), params.get("failure_mode")), depth=1),
                "constraints": _compact_keys(constraint_check, ("status", "reason", "reject_reasons", "warnings")),
            }.items()
            if _non_empty(value)
        },
        "evidence": {
            key: value
            for key, value in {
                "generation_reason": _compact_keys(
                    generation_reason,
                    (
                        "source",
                        "provider",
                        "model",
                        "category",
                        "formula",
                        "rationale",
                        "fallback_reason",
                        "target_symbols",
                        "selection_logic",
                    ),
                ),
                "research_task": _compact_keys(
                    research_task,
                    (
                        "task_id",
                        "task_source",
                        "theme",
                        "theme_name",
                        "opportunity_type",
                        "direction",
                        "horizon",
                        "candidate_family",
                    ),
                ),
                "validation": _compact_keys(
                    {
                        **validation_profile,
                        "spec_completeness": _first(payload.get("spec_completeness"), params.get("spec_completeness")),
                        "completion_issues": _first(payload.get("completion_issues"), params.get("completion_issues")),
                    },
                    ("profile", "validation_focus", "primary_validation_layer", "spec_completeness", "completion_issues"),
                ),
                "metrics": metrics_summary,
            }.items()
            if _non_empty(value)
        },
    }
    return {key: value for key, value in result.items() if _non_empty(value) or key == "version"}


def render_strategy_description(
    name: str | None,
    explanation: Mapping[str, Any] | None,
    *,
    metrics: Mapping[str, Any] | None = None,
) -> str:
    """Render a compact multi-line description from an explanation contract."""

    payload = _mapping(explanation)
    lines: list[str] = []

    def add(prefix: str, value: Any) -> None:
        text = _text(value, limit=360)
        if not text:
            return
        line = f"{prefix}: {text}" if prefix else text
        if line not in lines:
            lines.append(line)

    add("", payload.get("summary") or name)
    add("Why", payload.get("why_generated"))
    target_scope = _mapping(payload.get("target_scope"))
    symbols = _string_list(target_scope.get("symbols"), limit=6)
    if symbols:
        add("Targets", ", ".join(symbols))
    signal_logic = _mapping(payload.get("signal_logic"))
    add("Entry", signal_logic.get("entry"))
    add("Exit", signal_logic.get("exit"))
    risk_notes = _mapping(payload.get("risk_notes"))
    add("Risk", risk_notes.get("risk_level"))
    metric_payload = _metric_summary(metrics or _mapping(_mapping(payload.get("evidence")).get("metrics")))
    metric_parts = [
        f"Sharpe {_format_float(metric_payload.get('sharpe_ratio'))}" if metric_payload.get("sharpe_ratio") is not None else None,
        f"Return {_format_pct(metric_payload.get('total_return'))}" if metric_payload.get("total_return") is not None else None,
        f"Drawdown {_format_pct(metric_payload.get('max_drawdown'))}" if metric_payload.get("max_drawdown") is not None else None,
        f"Win {_format_pct(metric_payload.get('win_rate'))}" if metric_payload.get("win_rate") is not None else None,
    ]
    metrics_line = " | ".join(part for part in metric_parts if part)
    add("Backtest", metrics_line)
    labels = _string_list(payload.get("labels"), limit=8)
    if labels:
        add("Labels", ", ".join(labels))
    return "\n".join(lines)


__all__ = [
    "EXPLANATION_VERSION",
    "build_strategy_explanation",
    "render_strategy_description",
]
