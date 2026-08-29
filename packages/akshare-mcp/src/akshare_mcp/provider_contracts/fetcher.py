"""Provider/fetcher lifecycle wrappers over existing AIASK tool functions."""

from __future__ import annotations

import inspect
import os
from copy import deepcopy
from typing import Any, Awaitable, Callable

from .base import FetcherExecutionResult, ProviderContract
from .metadata import attach_tool_provider_contract_meta
from .quality import evaluate_provider_quality_gate
from .registry import get_provider_tool_contract, provider_tool_contracts

FetcherCallable = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _validate_required_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(name) for name in required if arguments.get(str(name)) in (None, "")]


class ProviderFetcher:
    """A compatibility wrapper that adds lifecycle metadata around old tools."""

    def __init__(self, contract: ProviderContract | dict[str, Any], executor: FetcherCallable | None = None) -> None:
        contract_payload = contract.model_dump(mode="json") if isinstance(contract, ProviderContract) else dict(contract)
        self.contract = ProviderContract(
            tool_name=str(contract_payload.get("name") or contract_payload.get("tool_name") or ""),
            standard_model=str(contract_payload.get("standard_model") or contract_payload.get("name") or "ProviderContract"),
            input_schema=_dict_or_empty(contract_payload.get("input_schema")),
            output_schema=_dict_or_empty(contract_payload.get("output_schema")),
            freshness=_dict_or_empty(contract_payload.get("freshness")),
            source_policy=_dict_or_empty(contract_payload.get("source_policy")),
            provider_choices=list(contract_payload.get("provider_choices") or []),
            provider_status=_dict_or_empty(contract_payload.get("provider_status")),
            quality_gate=_dict_or_empty(contract_payload.get("quality_gate")),
            reconciliation=_dict_or_empty(contract_payload.get("reconciliation")),
            form_schema=_dict_or_empty(contract_payload.get("form_schema")),
            contract_version=str(contract_payload.get("contract_version") or "ai_tool_contract_v1"),
            contract_source=str(contract_payload.get("contract_source") or "akshare_mcp.tool_catalog"),
        )
        self.executor = executor

    async def execute(self, arguments: dict[str, Any] | None = None) -> FetcherExecutionResult:
        payload = dict(arguments or {})
        missing = _validate_required_arguments(self.contract.input_schema, payload)
        if missing:
            quality_gate = {
                "status": "failed",
                "mode": self.contract.quality_gate.get("mode", "report_only"),
                "failed_checks": ["required_arguments"],
                "checks": [
                    {
                        "name": "required_arguments",
                        "passed": False,
                        "severity": "error",
                        "missing": missing,
                    }
                ],
            }
            return FetcherExecutionResult(
                success=False,
                tool_name=self.contract.tool_name,
                standard_model=self.contract.standard_model,
                error=f"missing required arguments: {', '.join(missing)}",
                meta={
                    "provider_contract": self.contract.model_dump(mode="json", exclude_none=True),
                    "quality_gate": quality_gate,
                    "provider_status": self.contract.provider_status,
                },
            )
        if self.executor is None:
            return FetcherExecutionResult(
                success=False,
                tool_name=self.contract.tool_name,
                standard_model=self.contract.standard_model,
                error="provider fetcher executor is not configured",
                meta={
                    "provider_contract": self.contract.model_dump(mode="json", exclude_none=True),
                    "quality_gate": {
                        "status": "failed",
                        "mode": self.contract.quality_gate.get("mode", "report_only"),
                        "failed_checks": ["executor_missing"],
                    },
                    "provider_status": self.contract.provider_status,
                },
            )
        result = self.executor(payload)
        if inspect.isawaitable(result):
            result = await result
        tool_result = dict(result or {})
        if str(os.getenv("AIASK_PROVIDER_CONTRACT_DIAGNOSTICS", "1")).strip().lower() in {"0", "false", "no", "off"}:
            return FetcherExecutionResult(
                success=bool(tool_result.get("success")),
                tool_name=self.contract.tool_name,
                standard_model=self.contract.standard_model,
                data=deepcopy(tool_result.get("data")),
                error=tool_result.get("error"),
                meta=dict(tool_result.get("meta") or {}),
            )
        quality_gate = evaluate_provider_quality_gate(tool_result, self.contract.model_dump(mode="json"))
        response = attach_tool_provider_contract_meta(
            tool_result,
            tool_name=self.contract.tool_name,
            standard_model=self.contract.standard_model,
            quality=tool_result.get("data_quality") if isinstance(tool_result.get("data_quality"), dict) else None,
        )
        meta = dict(response.get("meta") or {})
        meta["quality_gate"] = quality_gate
        meta["reconciliation"] = quality_gate.get("reconciliation") or self.contract.reconciliation
        meta["provider_status"] = self.contract.provider_status
        response["meta"] = meta
        return FetcherExecutionResult(
            success=bool(response.get("success")),
            tool_name=self.contract.tool_name,
            standard_model=self.contract.standard_model,
            data=deepcopy(response.get("data")),
            error=response.get("error"),
            meta=meta,
        )


class ProviderRegistryMap:
    """Read-only registry map for provider contracts and compatible fetchers."""

    def __init__(self, contracts: dict[str, dict[str, Any]] | None = None) -> None:
        self._contracts = contracts or provider_tool_contracts()
        self._fetchers: dict[str, ProviderFetcher] = {}

    def names(self) -> list[str]:
        return sorted(self._contracts)

    def get_contract(self, tool_name: str) -> dict[str, Any] | None:
        contract = self._contracts.get(str(tool_name or "").strip())
        return deepcopy(contract) if contract else None

    def fetcher_for(self, tool_name: str, executor: FetcherCallable | None = None) -> ProviderFetcher | None:
        contract = self.get_contract(tool_name)
        if not contract:
            return None
        if executor is None and tool_name in self._fetchers:
            return self._fetchers[tool_name]
        fetcher = ProviderFetcher(contract, executor=executor)
        if executor is None:
            self._fetchers[tool_name] = fetcher
        return fetcher

    def register_fetcher(self, tool_name: str, executor: FetcherCallable) -> ProviderFetcher:
        contract = get_provider_tool_contract(tool_name)
        if contract is None:
            raise KeyError(f"provider contract is not registered: {tool_name}")
        fetcher = ProviderFetcher(contract, executor=executor)
        self._fetchers[str(tool_name)] = fetcher
        return fetcher

    def provider_capabilities(self) -> list[dict[str, Any]]:
        capabilities: dict[str, dict[str, Any]] = {}
        for contract in self._contracts.values():
            for choice in contract.get("provider_choices") or []:
                provider = str(choice.get("provider") or "unknown")
                item = capabilities.setdefault(
                    provider,
                    {
                        "provider": provider,
                        "sources": [],
                        "standard_models": [],
                        "tools": [],
                    },
                )
                item["sources"].append(choice.get("source"))
                item["standard_models"].append(contract.get("standard_model"))
                item["tools"].append(contract.get("name"))
        return [
            {
                **item,
                "sources": sorted(set(str(v) for v in item["sources"] if v)),
                "standard_models": sorted(set(str(v) for v in item["standard_models"] if v)),
                "tools": sorted(set(str(v) for v in item["tools"] if v)),
            }
            for item in sorted(capabilities.values(), key=lambda row: row["provider"])
        ]

    def coverage_report(self, *, known_tools: list[str] | None = None) -> dict[str, Any]:
        explicit = set(self._contracts)
        known = set(known_tools or explicit)
        missing = sorted(known - explicit)
        return {
            "explicit_contract_count": len(explicit),
            "known_tool_count": len(known),
            "coverage": 1.0 if not known else round((len(known) - len(missing)) / len(known), 4),
            "missing_explicit_contracts": missing,
            "runtime_inference_fallback": bool(missing),
        }
