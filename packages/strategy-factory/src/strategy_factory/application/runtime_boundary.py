"""Runtime boundary preflight for Strategy Factory host integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from aiask_quant_core.storage.contracts import REQUIRED_REPOSITORY_METHODS


RUNTIME_BOUNDARY_CONTRACT_VERSION = "strategy_factory.runtime_boundary.v1"

REQUIRED_RUNTIME_ADAPTERS: tuple[str, ...] = (
    "repository",
    "vector_search",
    "autonomy",
    "factor_research",
    "incubation",
    "validation",
    "risk",
)

@dataclass(frozen=True)
class RuntimeBoundaryReport:
    """Stable runtime-provider preflight result."""

    ok: bool
    status: str
    contract_version: str = RUNTIME_BOUNDARY_CONTRACT_VERSION
    missing_repository_methods: tuple[str, ...] = field(default_factory=tuple)
    missing_runtime_adapters: tuple[str, ...] = field(default_factory=tuple)
    checked_repository_methods: tuple[str, ...] = field(default_factory=tuple)
    checked_runtime_adapters: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocking_reason_codes(self) -> list[str]:
        reasons: list[str] = []
        reasons.extend(f"missing_repository_method:{name}" for name in self.missing_repository_methods)
        reasons.extend(f"missing_runtime_adapter:{name}" for name in self.missing_runtime_adapters)
        return reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "ok": bool(self.ok),
            "status": self.status,
            "missing_repository_methods": list(self.missing_repository_methods),
            "missing_runtime_adapters": list(self.missing_runtime_adapters),
            "checked_repository_methods": list(self.checked_repository_methods),
            "checked_runtime_adapters": list(self.checked_runtime_adapters),
            "blocking_reason_codes": self.blocking_reason_codes,
        }


def _callable_attr(target: Any, name: str) -> bool:
    method = getattr(target, name, None)
    return callable(method)


def _missing_methods(target: Any, method_names: Iterable[str]) -> tuple[str, ...]:
    if target is None:
        return tuple(method_names)
    return tuple(name for name in method_names if not _callable_attr(target, name))


def validate_strategy_factory_runtime(
    db: Any = None,
    runtime_adapters: Any = None,
    *,
    require_runtime_adapters: bool = True,
    required_repository_methods: Iterable[str] = REQUIRED_REPOSITORY_METHODS,
) -> RuntimeBoundaryReport:
    """Validate that a full Strategy Factory cycle has its host contracts."""

    checked_methods = tuple(required_repository_methods)
    repository_target = db
    runtime_repository = getattr(runtime_adapters, "repository", None) if runtime_adapters is not None else None
    if repository_target is None and runtime_repository is not None:
        repository_target = getattr(runtime_repository, "raw", runtime_repository)

    missing_repository_methods = _missing_methods(repository_target, checked_methods)
    checked_adapters = REQUIRED_RUNTIME_ADAPTERS if require_runtime_adapters else tuple()
    missing_runtime_adapters: tuple[str, ...] = tuple()
    if require_runtime_adapters:
        if runtime_adapters is None:
            missing_runtime_adapters = checked_adapters
        else:
            missing_runtime_adapters = tuple(
                name
                for name in checked_adapters
                if getattr(runtime_adapters, name, None) is None
                or getattr(getattr(runtime_adapters, name, None), "runtime_provider_missing", None)
            )

    ok = not missing_repository_methods and not missing_runtime_adapters
    return RuntimeBoundaryReport(
        ok=ok,
        status="ok" if ok else "runtime_boundary_failed",
        missing_repository_methods=missing_repository_methods,
        missing_runtime_adapters=missing_runtime_adapters,
        checked_repository_methods=checked_methods,
        checked_runtime_adapters=checked_adapters,
    )


__all__ = [
    "REQUIRED_REPOSITORY_METHODS",
    "REQUIRED_RUNTIME_ADAPTERS",
    "RUNTIME_BOUNDARY_CONTRACT_VERSION",
    "RuntimeBoundaryReport",
    "validate_strategy_factory_runtime",
]
