"""Provider contract layer for AIASK financial data tools."""

from .base import (
    PROVIDER_CONTRACT_VERSION,
    TOOL_CONTRACT_VERSION,
    FreshnessPolicy,
    FetcherExecutionResult,
    ProviderCapability,
    ProviderContractFetchResult,
    ProviderContractMeta,
    ProviderContract,
    ProviderCredentialRequirement,
    SourcePolicy,
    attach_provider_contract_meta,
)
from .fetcher import ProviderFetcher, ProviderRegistryMap
from .metadata import attach_tool_provider_contract_meta
from .quality import evaluate_provider_quality_gate
from .registry import get_provider_tool_contract, provider_tool_contracts
from .result import AIASKFinancialResult

__all__ = [
    "PROVIDER_CONTRACT_VERSION",
    "TOOL_CONTRACT_VERSION",
    "FreshnessPolicy",
    "FetcherExecutionResult",
    "ProviderCapability",
    "ProviderContract",
    "ProviderContractFetchResult",
    "ProviderContractMeta",
    "ProviderCredentialRequirement",
    "ProviderFetcher",
    "ProviderRegistryMap",
    "SourcePolicy",
    "AIASKFinancialResult",
    "attach_provider_contract_meta",
    "attach_tool_provider_contract_meta",
    "evaluate_provider_quality_gate",
    "get_provider_tool_contract",
    "provider_tool_contracts",
]
