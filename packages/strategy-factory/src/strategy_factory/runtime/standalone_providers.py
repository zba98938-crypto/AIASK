"""Standalone provider set: run the factory contract with no host package.

Phase 1's exit condition is that Strategy Factory can complete a real
``preflight`` / ``status`` / ``run_once`` cycle with no AKShare host present.
``ensure_default_runtime_services`` cannot satisfy that: it resolves all 19
required providers by dynamically importing
``akshare_mcp.adapters.strategy_factory_runtime``.

This module supplies the other half — a provider set built only from Strategy
Factory and ``aiask-quant-core`` (a declared dependency). It is deliberately
*minimal, not fake*:

* ``db_provider`` is the real quant-core SQLite adapter, so schema and ledger
  writes actually happen.
* Engine-shaped providers return empty results rather than fabricated data. An
  empty-data cycle is the honest outcome when no data provider is installed.
* Every degradation is reported through the normal contract (``degraded_reasons``
  / ``support_missing``), never by silently claiming success.

What this is **not**: a substitute for the AKShare host. It cannot mine factors,
fetch market data, or execute paper trades. Its purpose is to make the contract
executable — and therefore testable — without a host, and to give a second host a
working baseline to override provider by provider.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


#: Marker recorded on results produced without a data provider, so a caller can
#: never mistake an empty standalone cycle for a real one.
STANDALONE_MARKER = "standalone_no_data_provider"


class _EmptyPool:
    """Minimal active-pool stand-in: always empty, never fabricates factors."""

    size = 0
    _factors: dict[str, Any] = {}

    def get_active_factors(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def admit_batch(self, _validated: Any) -> list[Any]:
        return []


class StandaloneFactorMiningSupport:
    """Factor-mining provider with no search engines installed."""

    def __init__(self, db_provider: Any):
        self._db_provider = db_provider
        self._active_pool = _EmptyPool()

    async def get_db(self) -> Any:
        return _resolve_db(self._db_provider)

    async def ensure_persistent_pool(self, _db: Any) -> None:
        return None

    async def build_mining_context(
        self, db: Any = None, *, codes: list[str] | None = None
    ) -> Any:
        # The orchestrator passes ``db`` as a keyword, so the parameter must be
        # named accordingly rather than underscore-prefixed.
        del db, codes
        return {"validation_codes": [], "standalone": True}

    def install_quick_evidence_evaluators(self, _db: Any, _context: Any) -> Any:
        return None

    async def search_candidates(self, _context: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def evolve_candidates(self, candidates: list[Any], _context: Any, **_kwargs: Any):
        return list(candidates)

    async def quick_filter_candidates(self, candidates: list[Any], _context: Any):
        return list(candidates)

    async def validate_batch(self, _db: Any, _candidates: Any, _context: Any) -> list[Any]:
        return []

    async def admit_batch(self, _validated: Any) -> list[Any]:
        return []

    async def persist_admitted_factors(self, _db: Any, _admitted: Any) -> None:
        return None

    async def persist_mining_run(self, _db: Any, _report: Any) -> None:
        return None

    async def reappraise_quarantine_factors(self, _db: Any, *, limit: int = 200) -> dict[str, Any]:
        del limit
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    async def record_feedback(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_active_pool_size(self) -> int:
        return 0

    def get_last_engines_used(self) -> list[str]:
        return []

    # --- maintenance -----------------------------------------------------------

    async def run_decay_check(self, _db: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    async def persist_decay_report(self, _db: Any, _report: Any) -> None:
        return None

    async def persist_decay_updates(self, _db: Any, _report: Any) -> None:
        return None

    async def promote_quarantine_factors(self, _db: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    async def run_qc_pipeline(self, _db: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    def status(self) -> dict[str, Any]:
        return {"mode": "standalone", "engines": [], "pool_size": 0}


class StandaloneIncubationSupport:
    """Incubation provider with no strategies and no paper-trading engines."""

    def __init__(self, db_provider: Any, **kwargs: Any):
        self._db_provider = db_provider
        self.run_time = kwargs.get("run_time")
        self.dry_run = bool(kwargs.get("dry_run", False))
        self.owns_paper_trading = False

    async def get_db(self) -> Any:
        return _resolve_db(self._db_provider)

    async def scan_and_accept_strategies(self, _db: Any) -> dict[str, Any]:
        return {"accepted": 0, "reason": STANDALONE_MARKER}

    async def run_recompile_remediation(self, _db: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    async def list_incubating_strategies(self, _db: Any, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        return []

    async def list_paper_observation_strategies(self, _db: Any, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        return []

    async def list_diagnostic_observation_strategies(self, _db: Any, limit: int = 50) -> list[dict[str, Any]]:
        del limit
        return []

    async def generate_signals(self, _db: Any, _strategy: dict[str, Any]) -> dict[str, Any]:
        return {"signals_generated": 0}

    async def settle_strategy_orders(self, _db: Any, _strategy: Any, _signal_result: Any) -> dict[str, Any]:
        return {}

    async def verify_forward_returns(self, _db: Any, _strategy: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def record_metrics(self, _db: Any, _strategy: Any, _verification: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def generate_hit_rate_report(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"hit_rate_dashboard": {"overall": {}}}

    async def write_feedback(self, _db: Any, _report: Any) -> dict[str, Any]:
        return {}

    async def verify_trade_predictions(self, _db: Any) -> dict[str, Any]:
        return {}

    async def run_accelerator(self, _db: Any) -> dict[str, Any]:
        return {}

    async def monitor_alerts(self, _db: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def status(self) -> dict[str, Any]:
        return {"mode": "standalone", "dry_run": self.dry_run, "owns_paper_trading": False}


class StandaloneSignalTrackerSupport:
    """Signal-tracker provider with an empty universe.

    Implements the full 18-method orchestrator surface. Every phase returns an
    empty result so the A-I cycle runs end to end and reports honestly, rather
    than aborting partway with an ``AttributeError``.
    """

    def __init__(self, db_provider: Any, **_kwargs: Any):
        self._db_provider = db_provider

    async def get_db(self) -> Any:
        return _resolve_db(self._db_provider)

    def get_default_universe(self) -> list[str]:
        return []

    def phase_timeout_seconds(self, _phase: str, default: float = 600.0) -> float:
        return float(default)

    # --- strategy discovery ----------------------------------------------------

    async def load_executable_strategies(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def load_runtime_submitted_strategies(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def load_runtime_observation_strategies(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    # --- signal generation -----------------------------------------------------

    async def generate_signals(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"signals_generated": 0}

    async def create_signal_event_snapshot(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def backfill_forward_returns(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"computed": 0, "reason": STANDALONE_MARKER}

    async def get_klines(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    # --- downstream pipelines --------------------------------------------------

    async def run_submitted_runtime_pipeline(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    async def run_incubation_pipeline(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    async def sync_incubation_orders(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"synced": 0}

    async def sync_incubation_metrics(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"synced": 0}

    async def sync_incubation_nav_snapshots(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"synced": 0}

    async def run_runtime_risk_scan(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"events": 0}

    async def run_lifecycle_scan(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    async def snapshot_domain_projections(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"snapshots": 0}

    async def reconcile_vector_registry(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "skipped", "reason": STANDALONE_MARKER}

    def status(self) -> dict[str, Any]:
        return {"mode": "standalone", "universe_size": 0}


class StandaloneMarketEventIngestSupport:
    """Market-event provider with no sources configured."""

    def __init__(self, db_provider: Any, **_kwargs: Any):
        self._db_provider = db_provider

    async def get_db(self) -> Any:
        return _resolve_db(self._db_provider)

    async def run_cycle(
        self, *, trigger: str = "scheduled", lookback_days: int = 7
    ) -> dict[str, Any]:
        """Report a skip, not a success: no sources are configured."""
        return {
            "success": True,
            "status": "skipped",
            "trigger": trigger,
            "lookback_days": lookback_days,
            "quality_flags": [STANDALONE_MARKER],
            "totals": {"fetched": 0, "saved": 0, "errors": 0},
            "strategy_factory_bridge": {},
        }

    async def fetch_official_market_events(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def save_normalized_event(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def save_factory_event_cluster(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def save_factory_event_signal(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def status(self) -> dict[str, Any]:
        return {"mode": "standalone", "sources": []}


def _resolve_db(db_provider: Any) -> Any:
    resolved = db_provider() if callable(db_provider) else db_provider
    return resolved


def standalone_db_provider() -> Any:
    """The real quant-core SQLite adapter, so persistence genuinely happens."""
    from aiask_quant_core.storage import get_db

    return get_db()


def build_standalone_provider_kwargs() -> dict[str, Any]:
    """The full required provider set, implemented without any host package."""
    db = standalone_db_provider

    return {
        "db_provider": db,
        "factor_scheduler": lambda *a, **k: None,
        "factor_mining_factory": lambda *a, **k: StandaloneFactorMiningSupport(db),
        "factor_mining_support_factory": lambda *a, **k: StandaloneFactorMiningSupport(db),
        "factor_pool_gateway": lambda *a, **k: None,
        "quant_manager_callable": lambda *a, **k: None,
        "runtime_warmup_runner": lambda *a, **k: {"status": "skipped", "reason": STANDALONE_MARKER},
        "signal_tracker_runtime_factory": lambda **k: StandaloneSignalTrackerSupport(db, **k),
        "signal_tracker_runtime_support_factory": lambda **k: StandaloneSignalTrackerSupport(db, **k),
        "incubation_runtime_factory": lambda **k: StandaloneIncubationSupport(db, **k),
        "incubation_runtime_support_factory": lambda **k: StandaloneIncubationSupport(db, **k),
        "market_event_ingest_support_factory": lambda *a, **k: StandaloneMarketEventIngestSupport(db),
        "strategy_promotion_pipeline_service": lambda *a, **k: None,
        "strategy_runtime_control_service": lambda *a, **k: None,
        "event_context_builder": lambda *a, **k: {},
        "strategy_vector_platform_factory": lambda *a, **k: None,
        "execution_audit_snapshot_builder": lambda *a, **k: {},
        "closure_review_builder": lambda *a, **k: {},
        "strategy_llm_provider_loader": lambda *a, **k: None,
    }


def configure_standalone_runtime_services() -> tuple[str, ...]:
    """Register the standalone provider set.

    Signature matches the AKShare configurator so it can be selected through
    ``AIASK_STRATEGY_FACTORY_RUNTIME_CONFIGURATOR`` with no other change.
    """
    from ..api.runtime import configure_runtime_services
    from ..infrastructure.factory_schema import register_factory_schema

    register_factory_schema()
    configure_runtime_services(**build_standalone_provider_kwargs())
    logger.info(
        "strategy-factory: standalone provider set registered "
        "(no data provider; cycles produce empty, honestly-marked results)"
    )
    from ..infrastructure.runtime_services import get_registered_runtime_provider_names

    return get_registered_runtime_provider_names()


__all__ = [
    "STANDALONE_MARKER",
    "StandaloneFactorMiningSupport",
    "StandaloneIncubationSupport",
    "StandaloneMarketEventIngestSupport",
    "StandaloneSignalTrackerSupport",
    "build_standalone_provider_kwargs",
    "configure_standalone_runtime_services",
    "standalone_db_provider",
]
