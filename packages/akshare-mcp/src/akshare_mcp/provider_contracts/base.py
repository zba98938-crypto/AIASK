"""Shared provider contract models and metadata helpers."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TOOL_CONTRACT_VERSION = "ai_tool_contract_v1"
PROVIDER_CONTRACT_VERSION = "aiask.provider_contract.v1"
CONTRACT_SOURCE = "akshare_mcp.tool_catalog"


class ContractBaseModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class FreshnessPolicy(ContractBaseModel):
    expectation: str = Field(description="Human-readable freshness expectation.")
    max_stale_seconds: int | None = Field(default=None, ge=0)
    data_timestamp_field: str | None = None


class SourcePolicy(ContractBaseModel):
    priority: list[str] = Field(default_factory=list)
    local_only_env: str | None = None
    online_fallback: str | None = None
    notes: list[str] = Field(default_factory=list)


class ProviderContractMeta(ContractBaseModel):
    contract_version: str = PROVIDER_CONTRACT_VERSION
    contract_source: str = "akshare_mcp.provider_contracts"
    standard_model: str
    provider_requested: str | None = None
    provider_used: str | None = None
    source_chain: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str | list[str] | None = None
    data_timestamp: str | None = None
    freshness: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    quality_gate: dict[str, Any] = Field(default_factory=dict)
    reconciliation: dict[str, Any] = Field(default_factory=dict)
    provider_status: dict[str, Any] = Field(default_factory=dict)


class ProviderContractFetchResult(ContractBaseModel):
    success: bool
    model: str
    data: Any = None
    error: str | None = None
    meta: ProviderContractMeta


class ProviderCredentialRequirement(ContractBaseModel):
    provider: str
    env_vars: list[str] = Field(default_factory=list)
    required: bool = False
    description: str | None = None


class ProviderCapability(ContractBaseModel):
    provider: str
    capability: str
    standard_models: list[str] = Field(default_factory=list)
    priority: int | None = None
    status: str = "unknown"
    credential_requirements: list[ProviderCredentialRequirement] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProviderContract(ContractBaseModel):
    tool_name: str
    standard_model: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    freshness: dict[str, Any] = Field(default_factory=dict)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    provider_choices: list[dict[str, Any]] = Field(default_factory=list)
    provider_status: dict[str, Any] = Field(default_factory=dict)
    quality_gate: dict[str, Any] = Field(default_factory=dict)
    reconciliation: dict[str, Any] = Field(default_factory=dict)
    form_schema: dict[str, Any] = Field(default_factory=dict)
    contract_version: str = TOOL_CONTRACT_VERSION
    contract_source: str = CONTRACT_SOURCE


class FetcherExecutionResult(ContractBaseModel):
    success: bool
    tool_name: str
    standard_model: str
    data: Any = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def dedupe_text(items: list[Any] | tuple[Any, ...] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items or []:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    return deepcopy(model.model_json_schema())


def envelope_schema(data_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "data": data_schema,
            "error": {"type": ["string", "null"]},
            "source": {"type": "string"},
            "cached": {"type": "boolean"},
            "timestamp": {"type": "string"},
            "meta": {
                "type": "object",
                "properties": {
                    "provider_contract": ProviderContractMeta.model_json_schema(),
                    "contract_meta": {"type": "object"},
                    "quality": {"type": "object"},
                    "quality_gate": {"type": "object"},
                    "reconciliation": {"type": "object"},
                    "provider_status": {"type": "object"},
                    "source_chain": {"type": "array", "items": {"type": "string"}},
                    "degraded": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
        "required": ["success", "data", "error"],
        "additionalProperties": True,
    }


def _fallback_used_from_result(result: dict[str, Any], source_chain: list[str]) -> bool:
    if "fallback_used" in result:
        return bool(result.get("fallback_used"))
    return len(source_chain) > 1


def attach_provider_contract_meta(
    result: dict[str, Any],
    *,
    tool_name: str,
    standard_model: str,
    provider_requested: str | None = None,
    provider_used: str | None = None,
    source_chain: list[str] | None = None,
    fallback_reason: str | list[str] | None = None,
    data_timestamp: str | None = None,
    freshness: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    quality_gate: dict[str, Any] | None = None,
    reconciliation: dict[str, Any] | None = None,
    provider_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach contract metadata without changing the existing ``data`` shape."""

    if not isinstance(result, dict):
        return result
    if str(os.getenv("AIASK_PROVIDER_CONTRACT_DIAGNOSTICS", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return result
    chain = dedupe_text(source_chain or result.get("source_chain") or [])
    if not chain:
        chain = dedupe_text([provider_used or result.get("source") or provider_requested or "unknown"])
    resolved_provider = str(provider_used or result.get("source") or (chain[-1] if chain else "") or "unknown")
    meta = result.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    quality_meta = quality if isinstance(quality, dict) else dict(meta.get("quality") or {})
    contract_meta = ProviderContractMeta(
        standard_model=standard_model,
        provider_requested=provider_requested or (chain[0] if chain else None),
        provider_used=resolved_provider,
        source_chain=chain,
        fallback_used=_fallback_used_from_result(result, chain),
        fallback_reason=fallback_reason if fallback_reason is not None else result.get("fallback_reason"),
        data_timestamp=data_timestamp or result.get("data_timestamp"),
        freshness=dict(freshness or {}),
        quality=quality_meta,
        quality_gate=dict(quality_gate or {}),
        reconciliation=dict(reconciliation or {}),
        provider_status=dict(provider_status or {}),
    ).model_dump(mode="json", exclude_none=True)
    contract_meta["tool"] = str(tool_name or "").strip()

    meta["provider_contract"] = contract_meta
    meta["contract_meta"] = {
        "contract_version": PROVIDER_CONTRACT_VERSION,
        "contract_source": "akshare_mcp.provider_contracts",
        "standard_model": standard_model,
        "tool": str(tool_name or "").strip(),
        "provider_used": resolved_provider,
    }
    if quality_gate:
        meta["quality_gate"] = dict(quality_gate)
    if reconciliation:
        meta["reconciliation"] = dict(reconciliation)
    if provider_status:
        meta["provider_status"] = dict(provider_status)
    result["meta"] = meta
    return result


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
