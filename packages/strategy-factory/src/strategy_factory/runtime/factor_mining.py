"""Canonical factor-mining runtime owned by Strategy Factory.

This module provides the runtime wrapper. The actual mining cycle orchestration
logic has been migrated to the application layer.
"""

from __future__ import annotations

from typing import Any

from ..application.factor_mining import FactorMiningOrchestrator
from ..infrastructure.runtime_services import (
    get_factor_mining_factory,
    get_factor_mining_support_factory,
)
from .provider_adapters import LegacyFactorMiningProvider


class FactorMiningRuntime:
    """Host-neutral factor-mining runtime with Strategy Factory-owned orchestration.

    This class now delegates to the application-layer orchestrator for actual
    cycle execution logic. The support object is wrapped as a provider.
    """

    def __init__(self, support: Any):
        self._support = support
        provider = support
        if support is not None and not callable(getattr(support, "get_db", None)):
            provider = LegacyFactorMiningProvider(support)
        self._provider = provider
        self._orchestrator = FactorMiningOrchestrator(provider) if provider else None

    def preflight(self) -> dict[str, Any]:
        return {
            "available": self._support is not None,
            "runtime_type": type(self._support).__name__ if self._support is not None else None,
        }

    def status(self) -> dict[str, Any]:
        status = getattr(self._support, "status", None)
        if callable(status):
            return dict(status() or {})
        return self.preflight()

    async def run_once(
        self,
        *,
        trigger: str = "scheduled",
        engines: list[str] | None = None,
        candidate_count: int = 30,
        evolution_generations: int = 5,
        codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute one complete factor mining cycle.

        Now delegates to the application-layer orchestrator which owns the
        mining cycle logic.
        """
        if self._orchestrator is None:
            return {
                "success": False,
                "error": "factor_mining_support_missing",
                "trigger": trigger,
            }

        return await self._orchestrator.run_cycle(
            trigger=trigger,
            engines=engines,
            candidate_count=candidate_count,
            evolution_generations=evolution_generations,
            codes=codes,
        )

    async def run_maintenance(self) -> dict[str, Any]:
        """Run maintenance tasks (decay monitoring, QC pipeline).

        Goes through the public provider protocol only. This path previously
        reached through seven private attributes on the support object, which
        made it the one orchestration path with zero protocol coverage — and so
        the one that could not move out of the host.
        """
        provider = self._provider
        if provider is None:
            return {"error": "factor_mining_support_missing"}

        db = await provider.get_db()
        await provider.ensure_persistent_pool(db)

        decay_report = await provider.run_decay_check(db)
        await provider.persist_decay_report(db, decay_report)
        await provider.persist_decay_updates(db, decay_report)

        promotion_report = await provider.promote_quarantine_factors(db)
        qc_report = await provider.run_qc_pipeline(db)

        return {
            "decay_report": decay_report,
            "promotion_report": promotion_report,
            "qc_pipeline_report": qc_report,
            "pool_size": provider.get_active_pool_size(),
        }


def build_factor_mining_runtime(*, support: Any | None = None) -> FactorMiningRuntime:
    resolved_support = support or get_factor_mining_support_factory()()
    return FactorMiningRuntime(resolved_support)


def get_factor_mining_runtime() -> FactorMiningRuntime:
    return build_factor_mining_runtime()


def get_factor_mining_factory() -> FactorMiningRuntime:
    return get_factor_mining_runtime()


__all__ = [
    "FactorMiningRuntime",
    "build_factor_mining_runtime",
    "get_factor_mining_factory",
    "get_factor_mining_runtime",
]
