"""Factory-domain persistence facade owned by Strategy Factory.

Phase 2 of the extraction plan. The goal is for Strategy Factory to own the
*contract* for factory-domain persistence without owning a database driver.

Why a facade rather than a physical move of the mixins: the repository mixins in
``aiask-quant-core`` are built on host infrastructure (``self.acquire()``,
``self._decode_json_field``, the connection pool). Moving them would drag that
infrastructure across the boundary, and the host reaches these methods by duck
typing across ~121 call sites, so a one-shot move is hard to roll back.

Instead this facade:

* declares which methods belong to the factory domain (single source of truth,
  derived from the shared contract);
* forwards to an injected connection, so no SQL is duplicated;
* reports missing capabilities structurally instead of raising ``AttributeError``
  from deep inside an orchestration path;
* keeps market-data reads out — those stay owned by the data provider.

Once every host exposes the full factory-domain surface, the mixins can be
retired behind this facade without any caller changing.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)


try:  # pragma: no cover - exercised by both installed and standalone layouts
    from aiask_quant_core.storage.contracts import (
        FACTORY_DOMAIN_REPOSITORY_METHODS,
        MARKET_DATA_REPOSITORY_METHODS,
    )
except ModuleNotFoundError:  # pragma: no cover - standalone fallback
    MARKET_DATA_REPOSITORY_METHODS = (
        "get_klines",
        "get_limit_up_stats",
        "get_factor_ic_history",
        "list_stock_universe",
    )
    FACTORY_DOMAIN_REPOSITORY_METHODS = ()


class FactoryRepositoryError(RuntimeError):
    """Raised when a required factory-domain capability is absent."""


class MissingCapability(FactoryRepositoryError):
    """The injected connection does not implement a required method."""

    def __init__(self, method: str):
        super().__init__(
            f"factory-domain repository capability missing: {method}. "
            "The host connection must implement it, or the caller must degrade."
        )
        self.method = method


class FactoryRepository:
    """Typed facade over the factory-domain surface of an injected connection.

    Deliberately thin: it forwards rather than reimplements, so there is exactly
    one SQL implementation and no chance of the two drifting.
    """

    #: Methods this facade is willing to forward. Market-data reads are excluded
    #: by construction so the facade cannot become a backdoor to them.
    domain_methods: tuple[str, ...] = tuple(FACTORY_DOMAIN_REPOSITORY_METHODS)

    def __init__(self, connection: Any):
        if connection is None:
            raise FactoryRepositoryError("FactoryRepository requires a connection")
        self._connection = connection

    # --- capability inspection ------------------------------------------------

    @property
    def connection(self) -> Any:
        """The underlying connection, for code that still needs direct access."""
        return self._connection

    def supports(self, method: str) -> bool:
        return callable(getattr(self._connection, method, None))

    def missing_capabilities(self) -> list[str]:
        """Factory-domain methods the injected connection does not implement."""
        return sorted(name for name in self.domain_methods if not self.supports(name))

    def capability_report(self) -> dict[str, Any]:
        """Structured readiness summary, suitable for a preflight payload."""
        missing = self.missing_capabilities()
        return {
            "connection_type": type(self._connection).__name__,
            "declared": len(self.domain_methods),
            "available": len(self.domain_methods) - len(missing),
            "missing": missing,
            "ready": not missing,
        }

    # --- forwarding ----------------------------------------------------------

    def _resolve(self, method: str) -> Any:
        if method in MARKET_DATA_REPOSITORY_METHODS:
            raise FactoryRepositoryError(
                f"{method} reads market data and is not part of the factory domain; "
                "use the market-data provider directly"
            )
        if self.domain_methods and method not in self.domain_methods:
            raise FactoryRepositoryError(
                f"{method} is not a declared factory-domain method"
            )
        bound = getattr(self._connection, method, None)
        if not callable(bound):
            raise MissingCapability(method)
        return bound

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Await a factory-domain method on the injected connection."""
        return await self._resolve(method)(*args, **kwargs)

    async def try_call(
        self,
        method: str,
        *args: Any,
        default: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Await a method, returning ``default`` when the host lacks it.

        For optional capabilities. A required capability should use :meth:`call`
        so its absence surfaces instead of silently yielding a default.
        """
        try:
            bound = self._resolve(method)
        except MissingCapability:
            logger.debug("factory repository: optional capability absent: %s", method)
            return default
        return await bound(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Expose declared domain methods as attributes for ergonomic use.

        ``repo.save_strategy_factory_run(run)`` reads better than
        ``repo.call("save_strategy_factory_run", run)`` and keeps existing
        duck-typed call sites working when a repository is substituted for a raw
        connection. Undeclared names still fail loudly.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return self._resolve(name)


def build_factory_repository(connection: Any) -> FactoryRepository:
    """Construct a repository facade over ``connection``."""
    return FactoryRepository(connection)


def factory_domain_methods() -> tuple[str, ...]:
    """The declared factory-domain surface."""
    return tuple(FACTORY_DOMAIN_REPOSITORY_METHODS)


def assert_factory_domain_ready(connection: Any, *, required: Iterable[str] | None = None) -> None:
    """Raise unless ``connection`` implements the required factory-domain surface.

    Intended for preflight: a missing provider should be reported before a cycle
    starts, not discovered midway through one.
    """
    repository = FactoryRepository(connection)
    names = tuple(required) if required is not None else repository.domain_methods
    missing = sorted(name for name in names if not repository.supports(name))
    if missing:
        raise FactoryRepositoryError(
            "connection is missing factory-domain capabilities: " + ", ".join(missing)
        )


__all__ = [
    "FactoryRepository",
    "FactoryRepositoryError",
    "MissingCapability",
    "assert_factory_domain_ready",
    "build_factory_repository",
    "factory_domain_methods",
]
