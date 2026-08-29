"""AI-facing MCP tool contract catalog (package facade).

Re-exports STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract, TOOL_CONTRACTS, WORKFLOW_GUIDES
and the public accessor functions, preserving the original tool_catalog module API.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ...provider_contracts import provider_tool_contracts
from ._helpers import STANDARD_ENVELOPE_OUTPUT_SCHEMA, _contract
from .contracts_research import CONTRACTS as _CONTRACTS_RESEARCH
from .contracts_quant import CONTRACTS as _CONTRACTS_QUANT
from .contracts_strategy import CONTRACTS as _CONTRACTS_STRATEGY
from .contracts_data_sync import CONTRACTS as _CONTRACTS_DATA_SYNC
from .contracts_search import CONTRACTS as _CONTRACTS_SEARCH
from .contracts_screening import CONTRACTS as _CONTRACTS_SCREENING
from .contracts_governance import CONTRACTS as _CONTRACTS_GOVERNANCE
from .contracts_skills import CONTRACTS as _CONTRACTS_SKILLS
from .contracts_risk import CONTRACTS as _CONTRACTS_RISK
from .contracts_execution import CONTRACTS as _CONTRACTS_EXECUTION
from .contracts_market import CONTRACTS as _CONTRACTS_MARKET
from .workflow_guides import WORKFLOW_GUIDES

TOOL_CONTRACTS: dict[str, dict[str, Any]] = {}
TOOL_CONTRACTS.update(_CONTRACTS_RESEARCH)
TOOL_CONTRACTS.update(_CONTRACTS_QUANT)
TOOL_CONTRACTS.update(_CONTRACTS_STRATEGY)
TOOL_CONTRACTS.update(_CONTRACTS_DATA_SYNC)
TOOL_CONTRACTS.update(_CONTRACTS_SEARCH)
TOOL_CONTRACTS.update(_CONTRACTS_SCREENING)
TOOL_CONTRACTS.update(_CONTRACTS_GOVERNANCE)
TOOL_CONTRACTS.update(_CONTRACTS_SKILLS)
TOOL_CONTRACTS.update(_CONTRACTS_RISK)
TOOL_CONTRACTS.update(_CONTRACTS_EXECUTION)
TOOL_CONTRACTS.update(_CONTRACTS_MARKET)
TOOL_CONTRACTS.update(provider_tool_contracts())


def get_tool_contract(name: str) -> dict[str, Any] | None:
    item = TOOL_CONTRACTS.get(str(name or "").strip())
    return deepcopy(item) if item else None


def list_tool_contracts() -> list[dict[str, Any]]:
    return [deepcopy(TOOL_CONTRACTS[name]) for name in sorted(TOOL_CONTRACTS)]


def get_workflow_guide(name: str) -> dict[str, Any] | None:
    item = WORKFLOW_GUIDES.get(str(name or "").strip())
    return deepcopy(item) if item else None


def build_tool_meta(name: str) -> dict[str, Any]:
    contract = get_tool_contract(name)
    if not contract:
        return {"contract_version": "ai_tool_contract_v1"}
    return {
        "contract_version": contract.get("contract_version"),
        "contract_source": contract.get("contract_source"),
        "required_params": contract.get("required_params"),
        "side_effect": contract.get("side_effect"),
        "freshness": contract.get("freshness"),
        "source_policy": contract.get("source_policy"),
        "standard_model": contract.get("standard_model"),
        "provider_choices": contract.get("provider_choices"),
        "provider_status": contract.get("provider_status"),
        "quality_gate": contract.get("quality_gate"),
        "reconciliation": contract.get("reconciliation"),
        "form_schema": contract.get("form_schema"),
        "tags": contract.get("tags"),
        "examples": contract.get("examples"),
        "input_schema": contract.get("input_schema"),
        "output_schema": contract.get("output_schema"),
    }


__all__ = [
    "STANDARD_ENVELOPE_OUTPUT_SCHEMA",
    "TOOL_CONTRACTS",
    "WORKFLOW_GUIDES",
    "get_tool_contract",
    "list_tool_contracts",
    "get_workflow_guide",
    "build_tool_meta",
]
