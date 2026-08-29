"""The standalone provider set: run the contract with no host package.

This is what closed Phase 1's exit condition. ``ensure_default_runtime_services``
resolves all 19 required providers by dynamically importing ``akshare_mcp``, so it
can never satisfy "runs without a host". This set supplies the same surface using
only Strategy Factory and quant-core.

The properties that matter are honesty properties: it must be *minimal, not fake*.
Empty results are correct when no data provider is installed; fabricated results
would be a lie, and silently claiming success would be worse.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from strategy_factory.runtime.default_bootstrap import (
    DEFAULT_REQUIRED_RUNTIME_PROVIDERS,
    get_missing_required_runtime_providers,
)
from strategy_factory.runtime.standalone_providers import (
    STANDALONE_MARKER,
    StandaloneFactorMiningSupport,
    StandaloneIncubationSupport,
    StandaloneMarketEventIngestSupport,
    StandaloneSignalTrackerSupport,
    build_standalone_provider_kwargs,
    configure_standalone_runtime_services,
)
from strategy_factory.infrastructure.runtime_services import clear_runtime_services


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_runtime_services()
    yield
    clear_runtime_services()


# --- coverage of the required surface ------------------------------------------


def test_the_set_covers_every_required_provider() -> None:
    supplied = set(build_standalone_provider_kwargs())
    missing = set(DEFAULT_REQUIRED_RUNTIME_PROVIDERS) - supplied

    assert missing == set(), (
        f"the standalone set must satisfy every required provider; missing: {sorted(missing)}"
    )


def test_registration_leaves_nothing_missing() -> None:
    configure_standalone_runtime_services()
    assert get_missing_required_runtime_providers() == []


def test_registration_returns_the_registered_names() -> None:
    names = configure_standalone_runtime_services()
    assert len(names) >= len(DEFAULT_REQUIRED_RUNTIME_PROVIDERS)


def test_registration_also_claims_schema_ownership() -> None:
    """A standalone process must own its schema, not inherit a host's."""
    quant_core = pytest.importorskip(
        "aiask_quant_core.storage.sqlite.schema_providers"
    )
    quant_core.clear_schema_initializers()

    configure_standalone_runtime_services()

    assert quant_core.schema_source("strategy_runtime") == (
        "strategy_factory.infrastructure.factory_schema"
    )


# --- no host dependency --------------------------------------------------------


def test_the_module_never_imports_a_host_package() -> None:
    """Prose may explain the host; code must not reach for it.

    Checked on the AST rather than the text: the module docstring legitimately
    explains what this set replaces and why, and banning the word would force
    that explanation out.
    """
    import ast

    import strategy_factory.runtime.standalone_providers as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in {"import_module", "__import__"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        imported.add(arg.value.split(".")[0])

    assert "akshare_mcp" not in imported, (
        "the standalone set exists precisely to avoid the host; importing it "
        "would defeat its purpose"
    )


def test_the_db_provider_is_the_real_quant_core_adapter() -> None:
    """Persistence must genuinely happen; a stub would hide schema errors."""
    pytest.importorskip("aiask_quant_core.storage")

    from strategy_factory.runtime.standalone_providers import standalone_db_provider

    source = inspect.getsource(standalone_db_provider)
    assert "aiask_quant_core.storage" in source
    assert "get_db" in source


# --- honesty of empty results --------------------------------------------------


def test_the_marker_names_the_missing_capability() -> None:
    assert STANDALONE_MARKER == "standalone_no_data_provider"


def test_factor_mining_returns_empty_not_fabricated() -> None:
    support = StandaloneFactorMiningSupport(lambda: object())

    assert asyncio.run(support.search_candidates(None)) == []
    assert asyncio.run(support.validate_batch(None, ["x"], None)) == []
    assert asyncio.run(support.admit_batch(["x"])) == []
    assert support.get_active_pool_size() == 0
    assert support.get_last_engines_used() == []


def test_factor_mining_maintenance_reports_why_it_skipped() -> None:
    support = StandaloneFactorMiningSupport(lambda: object())

    for coroutine in (
        support.run_decay_check(None),
        support.promote_quarantine_factors(None),
        support.run_qc_pipeline(None),
        support.reappraise_quarantine_factors(None),
    ):
        outcome = asyncio.run(coroutine)
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == STANDALONE_MARKER, (
            "a skip must say why, so an empty cycle is not read as a clean one"
        )


def test_build_mining_context_accepts_the_keyword_the_orchestrator_uses() -> None:
    """The orchestrator calls ``build_mining_context(db=..., codes=...)``."""
    support = StandaloneFactorMiningSupport(lambda: object())
    context = asyncio.run(support.build_mining_context(db=object(), codes=["000001"]))

    assert context["validation_codes"] == []
    assert context["standalone"] is True


def test_incubation_accepts_no_strategies() -> None:
    support = StandaloneIncubationSupport(lambda: object())

    assert asyncio.run(support.scan_and_accept_strategies(None))["accepted"] == 0
    assert asyncio.run(support.list_incubating_strategies(None)) == []
    assert support.owns_paper_trading is False, (
        "a standalone process must not claim to own paper-trading engines"
    )


def test_market_event_ingest_reports_a_skip_not_a_success() -> None:
    support = StandaloneMarketEventIngestSupport(lambda: object())
    result = asyncio.run(support.run_cycle())

    assert result["status"] == "skipped"
    assert STANDALONE_MARKER in result["quality_flags"]
    assert result["totals"]["fetched"] == 0


# --- full provider surfaces ----------------------------------------------------


def test_signal_tracker_implements_the_whole_orchestrator_surface() -> None:
    """A partial surface aborts the A-I cycle with an AttributeError mid-run."""
    from strategy_factory.application.signal_tracker import orchestrator as module

    source = inspect.getsource(module)
    required = sorted(
        {
            line.split("self._provider.", 1)[1].split("(")[0].strip()
            for line in source.splitlines()
            if "self._provider." in line and "(" in line.split("self._provider.", 1)[1]
        }
    )

    support = StandaloneSignalTrackerSupport(lambda: object())
    missing = [name for name in required if not hasattr(support, name)]

    assert missing == [], f"StandaloneSignalTrackerSupport is missing {missing}"


def test_market_event_support_implements_its_orchestrator_surface() -> None:
    from strategy_factory.application.market_event_ingest import orchestrator as module

    source = inspect.getsource(module)
    required = sorted(
        {
            line.split("self._support.", 1)[1].split("(")[0].strip()
            for line in source.splitlines()
            if "self._support." in line and "(" in line.split("self._support.", 1)[1]
        }
    )

    support = StandaloneMarketEventIngestSupport(lambda: object())
    # ``_get_db`` is the legacy alias; the public ``get_db`` satisfies it.
    missing = [
        name
        for name in required
        if not hasattr(support, name) and not hasattr(support, name.lstrip("_"))
    ]

    assert missing == [], f"StandaloneMarketEventIngestSupport is missing {missing}"


def test_every_support_exposes_public_get_db() -> None:
    for factory in (
        StandaloneFactorMiningSupport,
        StandaloneIncubationSupport,
        StandaloneSignalTrackerSupport,
        StandaloneMarketEventIngestSupport,
    ):
        support = factory(lambda: object())
        assert callable(getattr(support, "get_db", None)), (
            f"{factory.__name__} must expose the public get_db()"
        )


def test_db_provider_is_resolved_lazily() -> None:
    """The adapter must not be constructed until a cycle needs it."""
    calls: list[int] = []

    def _provider():
        calls.append(1)
        return object()

    support = StandaloneFactorMiningSupport(_provider)
    assert calls == [], "constructing the support must not open a database"

    asyncio.run(support.get_db())
    assert calls == [1]


def test_every_support_reports_standalone_mode_in_status() -> None:
    for factory in (
        StandaloneFactorMiningSupport,
        StandaloneIncubationSupport,
        StandaloneSignalTrackerSupport,
        StandaloneMarketEventIngestSupport,
    ):
        status = factory(lambda: object()).status()
        assert status["mode"] == "standalone", (
            f"{factory.__name__}.status() must disclose standalone mode"
        )
