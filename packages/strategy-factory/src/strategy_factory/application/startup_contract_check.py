"""Startup contract checks for strategy factory (§11.3).

Validates feature flag dependencies and data prerequisites before
the scheduler begins accepting work. Failures set the circuit breaker
rather than calling sys.exit().
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


class StartupContractViolation:
    """Represents a single contract violation."""

    def __init__(self, code: str, message: str, *, blocking: bool = True):
        self.code = code
        self.message = message
        self.blocking = blocking

    def __repr__(self) -> str:
        severity = "BLOCKING" if self.blocking else "WARNING"
        return f"[{severity}] {self.code}: {self.message}"


async def check_startup_contracts(db: Any = None) -> list[StartupContractViolation]:
    """Run all startup contract checks.

    Args:
        db: Optional database adapter for data checks.

    Returns:
        List of violations. Empty list means all checks passed.
    """
    violations: list[StartupContractViolation] = []

    # Check 1: Feature flag dependency matrix (§11.1)
    manual_event_enabled = _env_bool("STRATEGY_FACTORY_MANUAL_EVENT_ENABLED")
    theme_graph_enabled = _env_bool("STRATEGY_FACTORY_THEME_GRAPH_ENABLED")
    theme_graph_readonly = _env_bool("STRATEGY_FACTORY_THEME_GRAPH_READONLY_ENABLED")
    dynamic_target_enabled = _env_bool("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED")
    regression_enabled = _env_bool("STRATEGY_FACTORY_THEME_REGRESSION_ENABLED")

    if theme_graph_enabled and not theme_graph_readonly:
        violations.append(StartupContractViolation(
            "FLAG_DEPENDENCY_THEME_GRAPH",
            "THEME_GRAPH_ENABLED=true requires THEME_GRAPH_READONLY_ENABLED=true",
        ))

    if manual_event_enabled and not theme_graph_enabled:
        violations.append(StartupContractViolation(
            "FLAG_DEPENDENCY_MANUAL_EVENT",
            "MANUAL_EVENT_ENABLED=true requires THEME_GRAPH_ENABLED=true",
        ))

    if regression_enabled and not theme_graph_enabled:
        violations.append(StartupContractViolation(
            "FLAG_DEPENDENCY_REGRESSION",
            "THEME_REGRESSION_ENABLED=true requires THEME_GRAPH_ENABLED=true",
        ))

    # Check 2: Data prerequisites (only if DB available and flags enabled)
    if db is not None and manual_event_enabled:
        try:
            if hasattr(db, "list_theme_nodes"):
                nodes = await db.list_theme_nodes(is_active=True, limit=200)
                if len(nodes) < 10:
                    violations.append(StartupContractViolation(
                        "DATA_THEME_NODES_INSUFFICIENT",
                        f"MANUAL_EVENT_ENABLED requires >= 10 active theme nodes, found {len(nodes)}",
                    ))
            if hasattr(db, "list_theme_edges"):
                edges = await db.list_theme_edges(is_active=True, limit=200)
                if len(edges) < 5:
                    violations.append(StartupContractViolation(
                        "DATA_THEME_EDGES_INSUFFICIENT",
                        f"MANUAL_EVENT_ENABLED requires >= 5 active theme edges, found {len(edges)}",
                    ))
        except Exception as exc:
            violations.append(StartupContractViolation(
                "DATA_CHECK_FAILED",
                f"Database check failed: {exc}",
                blocking=False,
            ))

    # Check 3: Module importability (§11.3)
    if dynamic_target_enabled:
        try:
            from ..domain.target_count_resolver import resolve_target_symbol_limit
            # Smoke test
            result = resolve_target_symbol_limit(task_source="manual_event")
            if result < 3 or result > 30:
                violations.append(StartupContractViolation(
                    "MODULE_TARGET_RESOLVER_INVALID",
                    f"resolve_target_symbol_limit returned {result}, expected 3-30",
                ))
        except Exception as exc:
            violations.append(StartupContractViolation(
                "MODULE_TARGET_RESOLVER_IMPORT",
                f"Cannot import target_count_resolver: {exc}",
            ))

    # Log results
    blocking = [v for v in violations if v.blocking]
    warnings = [v for v in violations if not v.blocking]

    if warnings:
        for v in warnings:
            logger.warning("Startup contract warning: %s", v)

    if blocking:
        for v in blocking:
            logger.error("Startup contract VIOLATION: %s", v)
        logger.error(
            "Strategy factory has %d blocking contract violations. "
            "Circuit breaker will be engaged.",
            len(blocking),
        )

    return violations


__all__ = [
    "StartupContractViolation",
    "check_startup_contracts",
]
