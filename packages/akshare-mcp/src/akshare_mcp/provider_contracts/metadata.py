"""Helpers for attaching catalog-backed provider contract metadata."""

from __future__ import annotations

import os
from typing import Any

from .base import attach_provider_contract_meta, dedupe_text
from .quality import evaluate_provider_quality_gate
from .registry import get_provider_tool_contract


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _data_payload(result: dict[str, Any]) -> Any:
    return result.get("data") if isinstance(result, dict) else None


def _source_from_payload(payload: Any) -> str | None:
    if isinstance(payload, dict):
        source = payload.get("source") or payload.get("backend_used")
        if source:
            return str(source)
        items = payload.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict) and items[0].get("source"):
            return str(items[0].get("source"))
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and payload[0].get("source"):
        return str(payload[0].get("source"))
    return None


def _chain_from_result(result: dict[str, Any], payload: Any) -> list[str]:
    meta = _dict_or_empty(result.get("meta"))
    chain = dedupe_text(result.get("source_chain") or result.get("fallback_chain") or meta.get("source_chain"))
    if chain:
        return chain
    if isinstance(payload, dict):
        chain = dedupe_text(payload.get("source_chain") or payload.get("fallback_chain"))
        if chain:
            return chain
    return []


def _fallback_reason_from_result(result: dict[str, Any], payload: Any) -> str | list[str] | None:
    if result.get("fallback_reason") is not None:
        return result.get("fallback_reason")
    meta = _dict_or_empty(result.get("meta"))
    if meta.get("fallback_reason") is not None:
        return meta.get("fallback_reason")
    if isinstance(payload, dict) and payload.get("fallback_reason") is not None:
        return payload.get("fallback_reason")
    data_quality = result.get("data_quality")
    if isinstance(data_quality, dict) and data_quality.get("fallback_reason") is not None:
        return data_quality.get("fallback_reason")
    return None


def _data_timestamp_from_result(result: dict[str, Any], payload: Any) -> str | None:
    for candidate in (result.get("data_timestamp"), result.get("asof_time")):
        if candidate:
            return str(candidate)
    meta = _dict_or_empty(result.get("meta"))
    for candidate in (meta.get("data_timestamp"), meta.get("asof_time")):
        if candidate:
            return str(candidate)
    if isinstance(payload, dict):
        for key in ("tradeDate", "trade_date", "date", "reportDate", "listDate"):
            if payload.get(key):
                return str(payload.get(key))
        records = payload.get("records")
        if isinstance(records, list) and records and isinstance(records[0], dict):
            if records[0].get("publishDate"):
                return str(records[0].get("publishDate"))
        underlying = payload.get("underlying")
        if isinstance(underlying, dict):
            for key in ("date", "time"):
                if underlying.get(key):
                    return str(underlying.get(key))
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        for key in ("tradeDate", "trade_date", "date", "reportDate"):
            if payload[0].get(key):
                return str(payload[0].get(key))
    return None


def _standard_model_from_contract(contract: dict[str, Any] | None, tool_name: str) -> str:
    if isinstance(contract, dict):
        explicit = contract.get("standard_model")
        if explicit:
            return str(explicit)
        data_schema = _dict_or_empty(_dict_or_empty(contract.get("output_schema")).get("properties")).get("data")
        if isinstance(data_schema, dict):
            for value in (data_schema.get("title"), _dict_or_empty(data_schema.get("items")).get("title")):
                if value:
                    return str(value)
    return str(tool_name or "ProviderContract")


def _priority_from_contract(contract: dict[str, Any] | None) -> list[str]:
    if not isinstance(contract, dict):
        return []
    source_policy = _dict_or_empty(contract.get("source_policy"))
    priority = source_policy.get("priority")
    return dedupe_text(priority if isinstance(priority, list) else [])


def attach_tool_provider_contract_meta(
    result: dict[str, Any],
    *,
    tool_name: str,
    standard_model: str | None = None,
    provider_requested: str | None = None,
    provider_used: str | None = None,
    source_chain: list[str] | None = None,
    fallback_reason: str | list[str] | None = None,
    data_timestamp: str | None = None,
    freshness: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach provider metadata using the explicit tool catalog when available."""

    if not isinstance(result, dict):
        return result
    if str(os.getenv("AIASK_PROVIDER_CONTRACT_DIAGNOSTICS", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return result

    contract = get_provider_tool_contract(tool_name)
    payload = _data_payload(result)
    priority = _priority_from_contract(contract)
    chain = dedupe_text(source_chain or _chain_from_result(result, payload))
    payload_source = _source_from_payload(payload)
    inferred_source = provider_used or result.get("backend_used") or payload_source or result.get("source")
    if not chain:
        chain = dedupe_text([inferred_source] if inferred_source else priority[:1])

    resolved_provider_used = str(inferred_source or (chain[-1] if chain else "unknown"))
    resolved_provider_requested = provider_requested or (chain[0] if chain else (priority[0] if priority else None))
    resolved_freshness = freshness or _dict_or_empty(contract.get("freshness") if isinstance(contract, dict) else None)
    meta = _dict_or_empty(result.get("meta"))
    resolved_quality = quality or _dict_or_empty(meta.get("quality")) or _dict_or_empty(result.get("data_quality"))
    resolved_provider_status = _dict_or_empty(contract.get("provider_status") if isinstance(contract, dict) else None)
    evaluated_quality_gate = evaluate_provider_quality_gate(result, contract)
    resolved_reconciliation = _dict_or_empty(evaluated_quality_gate.get("reconciliation")) or _dict_or_empty(
        contract.get("reconciliation") if isinstance(contract, dict) else None
    )

    response = attach_provider_contract_meta(
        result,
        tool_name=tool_name,
        standard_model=standard_model or _standard_model_from_contract(contract, tool_name),
        provider_requested=resolved_provider_requested,
        provider_used=resolved_provider_used,
        source_chain=chain,
        fallback_reason=fallback_reason if fallback_reason is not None else _fallback_reason_from_result(result, payload),
        data_timestamp=data_timestamp or _data_timestamp_from_result(result, payload),
        freshness=resolved_freshness,
        quality=resolved_quality,
        quality_gate=evaluated_quality_gate,
        reconciliation=resolved_reconciliation,
        provider_status=resolved_provider_status,
    )
    response_meta = _dict_or_empty(response.get("meta"))
    contract_meta = _dict_or_empty(response_meta.get("contract_meta"))
    if isinstance(contract, dict):
        contract_meta.update(
            {
                "contract_version": contract.get("contract_version"),
                "contract_source": contract.get("contract_source"),
                "source_policy": contract.get("source_policy"),
                "freshness": contract.get("freshness"),
                "provider_choices": contract.get("provider_choices"),
                "provider_status": contract.get("provider_status"),
                "quality_gate": contract.get("quality_gate"),
                "reconciliation": contract.get("reconciliation"),
            }
        )
    response_meta["contract_meta"] = {k: v for k, v in contract_meta.items() if v is not None}
    response["meta"] = response_meta
    return response
