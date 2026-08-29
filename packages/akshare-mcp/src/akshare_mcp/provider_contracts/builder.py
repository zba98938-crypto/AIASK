"""Contract builder utilities for the AIASK provider contract registry."""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from .base import (
    CONTRACT_SOURCE,
    TOOL_CONTRACT_VERSION,
    FreshnessPolicy,
    SourcePolicy,
    dedupe_text,
    envelope_schema,
    json_schema,
)

_PROVIDER_CREDENTIAL_ENVS: dict[str, list[str]] = {
    "tushare": ["TUSHARE_TOKEN", "TUSHARE_PRO_TOKEN"],
    "tushare_pro": ["TUSHARE_TOKEN", "TUSHARE_PRO_TOKEN"],
    "tdx_local": ["TDX_ROOT", "AIASK_TDX_ROOT", "TDX_LOCAL_PATH"],
    "tqcenter": [],
    "akshare": [],
    "eastmoney": [],
    "sina": [],
    "hkex": [],
    "db": ["AKSHARE_MCP_SQLITE_PATH", "AIASK_SQLITE_PATH"],
}


def _provider_key(source: str) -> str:
    token = str(source or "").strip()
    if not token:
        return "unknown"
    head = token.split(".", 1)[0].lower()
    if head.startswith("tushare"):
        return "tushare_pro"
    if head in {"data_source", "market_data"}:
        return "data_source"
    if head.startswith("eastmoney"):
        return "eastmoney"
    if head.startswith("sina"):
        return "sina"
    if head.startswith("hkex"):
        return "hkex"
    if head.startswith("tdx"):
        return "tdx_local"
    return head


def provider_status_for_priority(priority: list[str]) -> dict[str, Any]:
    providers = []
    for source in priority:
        provider = _provider_key(source)
        env_vars = _PROVIDER_CREDENTIAL_ENVS.get(provider, [])
        configured = any(bool(os.getenv(name, "").strip()) for name in env_vars) if env_vars else True
        providers.append(
            {
                "source": source,
                "provider": provider,
                "configured": bool(configured),
                "available": bool(configured),
                "degraded": False,
                "credential_env_vars": env_vars,
            }
        )
    return {
        "mode": "diagnostic",
        "providers": providers,
        "local_only": str(os.getenv("TDX_LOCAL_ONLY", "")).strip() == "1",
    }


def provider_choices_for_policy(source_policy: dict[str, Any]) -> list[dict[str, Any]]:
    priority = dedupe_text(source_policy.get("priority") if isinstance(source_policy, dict) else [])
    return [
        {
            "rank": index + 1,
            "source": source,
            "provider": _provider_key(source),
            "local_only_guarded": bool(source_policy.get("local_only_env")) if isinstance(source_policy, dict) else False,
        }
        for index, source in enumerate(priority)
    ]


def build_quality_gate_metadata(
    *,
    standard_model: str,
    freshness: dict[str, Any],
    source_policy: dict[str, Any],
) -> dict[str, Any]:
    mode = str(os.getenv("AIASK_PROVIDER_QUALITY_GATE_MODE", "report_only")).strip().lower() or "report_only"
    blocking = mode in {"block", "blocking", "strict"}
    checks = [
        "schema_completeness",
        "freshness_sla",
        "fallback_degraded_flag",
        "source_availability",
        "numeric_sanity",
    ]
    if len(source_policy.get("priority") or []) > 1:
        checks.append("multi_source_reconciliation")
    return {
        "mode": "blocking" if blocking else "report_only",
        "standard_model": standard_model,
        "checks": checks,
        "freshness_expectation": freshness.get("expectation"),
        "max_stale_seconds": freshness.get("max_stale_seconds"),
        "blocking": blocking,
    }


def build_reconciliation_metadata(source_policy: dict[str, Any]) -> dict[str, Any]:
    priority = dedupe_text(source_policy.get("priority") if isinstance(source_policy, dict) else [])
    return {
        "mode": "sampled_report_only",
        "enabled": len(priority) > 1,
        "primary_source": priority[0] if priority else None,
        "comparison_sources": priority[1:4],
        "mismatch_policy": "metadata_only",
    }


def build_form_schema(input_schema: dict[str, Any], examples: list[dict[str, Any]]) -> dict[str, Any]:
    schema = deepcopy(input_schema or {"type": "object", "properties": {}})
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    example_args = {}
    for example in examples or []:
        args = example.get("arguments") if isinstance(example, dict) else None
        if isinstance(args, dict):
            example_args = deepcopy(args)
            break
    return {
        "type": "object",
        "properties": properties if isinstance(properties, dict) else {},
        "required": required if isinstance(required, list) else [],
        "examples": [example_args] if example_args else [],
        "submit_policy": "read_only_only",
    }


def build_contract(
    *,
    name: str,
    title: str,
    category: str,
    description: str,
    required_params: list[str],
    query_model: type[BaseModel] | dict[str, Any],
    data_model: type[BaseModel] | dict[str, Any],
    freshness: FreshnessPolicy | dict[str, Any],
    source_policy: SourcePolicy | dict[str, Any],
    examples: list[dict[str, Any]],
    tags: list[str],
    standard_model: str | None = None,
) -> dict[str, Any]:
    input_schema = deepcopy(query_model) if isinstance(query_model, dict) else json_schema(query_model)
    data_schema = deepcopy(data_model) if isinstance(data_model, dict) else json_schema(data_model)
    freshness_payload = freshness.model_dump(mode="json", exclude_none=True) if isinstance(freshness, FreshnessPolicy) else dict(freshness or {})
    source_policy_payload = (
        source_policy.model_dump(mode="json", exclude_none=True)
        if isinstance(source_policy, SourcePolicy)
        else dict(source_policy or {})
    )
    if standard_model is None:
        standard_model = str(data_schema.get("title") or data_schema.get("items", {}).get("title") or name)
    provider_choices = provider_choices_for_policy(source_policy_payload)
    provider_status = provider_status_for_priority([item["source"] for item in provider_choices])
    quality_gate = build_quality_gate_metadata(
        standard_model=standard_model,
        freshness=freshness_payload,
        source_policy=source_policy_payload,
    )
    reconciliation = build_reconciliation_metadata(source_policy_payload)
    form_schema = build_form_schema(input_schema, examples)
    return {
        "name": name,
        "title": title,
        "category": category,
        "description": description,
        "required_params": required_params,
        "standard_model": standard_model,
        "input_schema": input_schema,
        "output_schema": envelope_schema(data_schema),
        "side_effect": {
            "level": "read_only",
            "confirmation_required": False,
        },
        "freshness": freshness_payload,
        "source_policy": source_policy_payload,
        "provider_choices": provider_choices,
        "provider_status": provider_status,
        "quality_gate": quality_gate,
        "reconciliation": reconciliation,
        "form_schema": form_schema,
        "examples": examples,
        "tags": tags,
        "contract_version": TOOL_CONTRACT_VERSION,
        "contract_source": CONTRACT_SOURCE,
    }
