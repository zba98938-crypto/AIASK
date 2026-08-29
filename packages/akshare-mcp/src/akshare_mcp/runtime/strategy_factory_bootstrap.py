"""Compatibility shim for the canonical Strategy Factory runtime bootstrap."""

from __future__ import annotations

from typing import Any


def configure_local_strategy_factory_runtime() -> None:
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services

    ensure_default_runtime_services()


def build_local_strategy_factory_runtime_adapters(db: Any | None = None) -> Any:
    from strategy_factory.runtime.default_bootstrap import build_default_runtime_adapters

    return build_default_runtime_adapters(db=db)


def build_local_strategy_factory_scheduler_kwargs(db: Any | None = None) -> dict[str, Any]:
    from strategy_factory.runtime.default_bootstrap import build_default_scheduler_kwargs

    return build_default_scheduler_kwargs(db=db)


__all__ = [
    "build_local_strategy_factory_runtime_adapters",
    "build_local_strategy_factory_scheduler_kwargs",
    "configure_local_strategy_factory_runtime",
]
