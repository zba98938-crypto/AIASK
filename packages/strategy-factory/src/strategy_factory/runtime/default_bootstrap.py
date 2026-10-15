"""Canonical runtime bootstrap for Strategy Factory-owned runtimes.

This module keeps ``strategy-factory`` free of static ``akshare_mcp`` imports
while still allowing the current AKShare host to register concrete runtime
providers through a dynamic configurator hook.
"""

from __future__ import annotations

from importlib import import_module
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

from ..api.runtime import build_scheduler_runtime_kwargs
from ..infrastructure.runtime_services import (
    get_registered_runtime_provider_names,
    has_runtime_provider,
)


DEFAULT_RUNTIME_PROVIDER_CONFIGURATOR = (
    "akshare_mcp.adapters.strategy_factory_runtime:"
    "configure_strategy_factory_runtime_services"
)

DEFAULT_REQUIRED_RUNTIME_PROVIDERS: tuple[str, ...] = (
    "db_provider",
    "factor_scheduler",
    "factor_mining_factory",
    "factor_mining_support_factory",
    "factor_pool_gateway",
    "quant_manager_callable",
    "runtime_warmup_runner",
    "signal_tracker_runtime_factory",
    "signal_tracker_runtime_support_factory",
    "incubation_runtime_factory",
    "incubation_runtime_support_factory",
    "market_event_ingest_support_factory",
    "strategy_promotion_pipeline_service",
    "strategy_runtime_control_service",
    "event_context_builder",
    "strategy_vector_platform_factory",
    "execution_audit_snapshot_builder",
    "closure_review_builder",
    "strategy_llm_provider_loader",
)

RUNTIME_PROVIDER_CONFIGURATOR_ENV_KEY = "AIASK_STRATEGY_FACTORY_RUNTIME_CONFIGURATOR"

#: The in-package provider set. Requires no host, so it is always resolvable.
STANDALONE_RUNTIME_PROVIDER_CONFIGURATOR = (
    "strategy_factory.runtime.standalone_providers:"
    "configure_standalone_runtime_services"
)

#: Configurators tried in order when none is named, first importable wins.
#:
#: A data provider comes first because it can actually mine factors and fetch
#: market data; the standalone set is the honest fallback that keeps the contract
#: executable when no provider is installed. Selection is therefore neutral —
#: driven by what is present, not by a hardcoded vendor.
RUNTIME_PROVIDER_CONFIGURATOR_CANDIDATES: tuple[str, ...] = (
    DEFAULT_RUNTIME_PROVIDER_CONFIGURATOR,
    STANDALONE_RUNTIME_PROVIDER_CONFIGURATOR,
)


def _configurator_importable(path: str) -> bool:
    """Whether ``path``'s module can be found, without importing it."""
    import importlib.util

    module_name = path.partition(":")[0]
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError) as _swallowed:
        logger.warning("packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py:79: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return False


def resolve_configurator_candidates() -> list[dict[str, Any]]:
    """Report each candidate and whether it is available, for diagnostics."""
    return [
        {"path": path, "available": _configurator_importable(path)}
        for path in RUNTIME_PROVIDER_CONFIGURATOR_CANDIDATES
    ]


def _resolve_configurator_path(configurator_path: str | None = None) -> str:
    """Pick a configurator: explicit argument, then env, then availability.

    An explicit choice is never second-guessed — if a caller names a configurator
    that cannot be imported, that must surface as an error rather than being
    silently swapped for another provider set.
    """
    explicit = str(configurator_path or os.getenv(RUNTIME_PROVIDER_CONFIGURATOR_ENV_KEY) or "").strip()
    if explicit:
        if ":" not in explicit:
            raise ValueError(
                "runtime configurator must use 'module.submodule:callable_name' format"
            )
        return explicit

    for candidate in RUNTIME_PROVIDER_CONFIGURATOR_CANDIDATES:
        if _configurator_importable(candidate):
            if candidate is not RUNTIME_PROVIDER_CONFIGURATOR_CANDIDATES[0]:
                logger.info(
                    "strategy-factory: using provider set %s "
                    "(preferred candidates unavailable)",
                    candidate,
                )
            return candidate

    # Unreachable in practice: the standalone set lives in this package.
    raise RuntimeError(
        "no runtime provider configurator is importable; candidates: "
        + ", ".join(RUNTIME_PROVIDER_CONFIGURATOR_CANDIDATES)
    )


def _load_configurator(configurator_path: str | None = None):
    path = _resolve_configurator_path(configurator_path)
    module_name, _, attr_name = path.partition(":")
    module = import_module(module_name)
    configurator = getattr(module, attr_name)
    if not callable(configurator):
        raise TypeError(f"runtime configurator is not callable: {path}")
    return configurator


def get_missing_required_runtime_providers(
    required_providers: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_RUNTIME_PROVIDERS,
) -> list[str]:
    return [
        str(name)
        for name in tuple(required_providers or ())
        if str(name or "").strip() and not has_runtime_provider(str(name))
    ]


def runtime_services_ready(
    required_providers: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_RUNTIME_PROVIDERS,
) -> bool:
    return not get_missing_required_runtime_providers(required_providers)


def _register_owned_schema() -> None:
    """Claim ownership of the factory-domain schema.

    Runs on every bootstrap path so the migration ledger records Strategy Factory
    as the initializer regardless of who started the process. Best-effort: a
    missing quant-core is reported by the provider checks, not here.
    """
    try:
        from ..infrastructure.factory_schema import register_factory_schema

        register_factory_schema()
    except Exception as exc:  # noqa: BLE001 - never block bootstrap on provenance
        logger.debug("factory schema ownership registration skipped: %s", exc)


def ensure_default_runtime_services(
    *,
    configurator_path: str | None = None,
    required_providers: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_RUNTIME_PROVIDERS,
) -> tuple[str, ...]:
    _register_owned_schema()
    missing_before = get_missing_required_runtime_providers(required_providers)
    if not missing_before:
        return get_registered_runtime_provider_names()
    configurator = _load_configurator(configurator_path)
    configurator()
    missing_after = get_missing_required_runtime_providers(required_providers)
    if missing_after:
        raise RuntimeError(
            "strategy-factory canonical bootstrap did not register required providers; "
            f"missing={missing_after}"
        )
    return get_registered_runtime_provider_names()


def build_default_runtime_adapters(db: Any | None = None) -> Any:
    ensure_default_runtime_services()
    return build_scheduler_runtime_kwargs(db=db)["runtime_adapters"]


def build_default_scheduler_kwargs(db: Any | None = None) -> dict[str, Any]:
    ensure_default_runtime_services()
    return build_scheduler_runtime_kwargs(db=db)


__all__ = [
    "DEFAULT_REQUIRED_RUNTIME_PROVIDERS",
    "DEFAULT_RUNTIME_PROVIDER_CONFIGURATOR",
    "RUNTIME_PROVIDER_CONFIGURATOR_CANDIDATES",
    "STANDALONE_RUNTIME_PROVIDER_CONFIGURATOR",
    "resolve_configurator_candidates",
    "RUNTIME_PROVIDER_CONFIGURATOR_ENV_KEY",
    "build_default_runtime_adapters",
    "build_default_scheduler_kwargs",
    "ensure_default_runtime_services",
    "get_missing_required_runtime_providers",
    "runtime_services_ready",
]
