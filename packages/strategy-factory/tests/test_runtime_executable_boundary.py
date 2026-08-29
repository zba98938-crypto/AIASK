"""Executable host-independence boundary for the four factory runtimes.

The existing boundary tests are *static*: they grep for ``akshare_mcp`` imports.
They pass today even though ``ensure_default_runtime_services()`` resolves all
required providers by dynamically importing ``akshare_mcp`` at runtime — the
static scan cannot see an import expressed as a string.

These tests close that gap by actually executing the runtime contract in a
subprocess where every ``akshare_mcp`` import is blocked at ``sys.meta_path``.
This is the real Phase 1 exit criterion: strategy-factory must be able to do
``preflight`` / ``status`` / ``run_once`` with no AKShare host present.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SF_SRC = ROOT / "packages" / "strategy-factory" / "src"
QC_SRC = ROOT / "packages" / "aiask-quant-core" / "src"

# Blocks akshare_mcp *and* records any attempt, so a test can distinguish
# "never tried" from "tried and was denied".
_PREAMBLE = """
import importlib.abc
import json
import sys

BLOCKED = []


class _BlockAkshare(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if str(fullname).split(".")[0] == "akshare_mcp":
            BLOCKED.append(fullname)
            raise ModuleNotFoundError("blocked akshare import", name=fullname)
        return None


sys.meta_path.insert(0, _BlockAkshare())
sys.path.insert(0, {sf_src!r})
sys.path.insert(0, {qc_src!r})
"""


def _run(body: str, *, timeout: int = 90) -> subprocess.CompletedProcess:
    code = textwrap.dedent(
        _PREAMBLE.format(sf_src=str(SF_SRC), qc_src=str(QC_SRC)) + textwrap.dedent(body)
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_importing_strategy_factory_never_touches_akshare() -> None:
    """A bare import must not reach for the host package at all."""
    completed = _run(
        """
        import strategy_factory
        from strategy_factory.runtime import (
            get_factor_mining_runtime,
            get_incubation_runtime,
            get_market_event_ingest_runtime,
            get_signal_tracker_runtime,
        )
        print(json.dumps({"blocked": BLOCKED}))
        """
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["blocked"] == [], (
        "importing strategy_factory must not attempt any akshare_mcp import"
    )


def test_four_runtimes_degrade_structurally_without_the_akshare_host() -> None:
    """preflight / status / run_once stay callable on injected providers alone.

    Scope note: the providers here are stubs returning ``None``, so this proves
    the contract *degrades structurally* (returns a ``support_missing`` payload)
    rather than raising or reaching for the host. It does NOT prove a real cycle
    can run standalone — that needs the genuine provider set to live inside
    strategy-factory, which is the actual Phase 1 exit condition still open.
    """
    completed = _run(
        """
        import asyncio
        import json as _json

        from strategy_factory.infrastructure.runtime_services import (
            clear_runtime_services,
            configure_runtime_services,
        )
        from strategy_factory.runtime import (
            get_factor_mining_runtime,
            get_incubation_runtime,
            get_market_event_ingest_runtime,
            get_signal_tracker_runtime,
        )

        clear_runtime_services()
        configure_runtime_services(
            db_provider=lambda: None,
            factor_scheduler=lambda: None,
            factor_mining_factory=lambda: None,
            factor_mining_support_factory=lambda *a, **k: None,
            factor_pool_gateway=lambda: None,
            quant_manager_callable=lambda *a, **k: None,
            runtime_warmup_runner=lambda *a, **k: None,
            signal_tracker_runtime_factory=lambda *a, **k: None,
            signal_tracker_runtime_support_factory=lambda *a, **k: None,
            incubation_runtime_factory=lambda *a, **k: None,
            incubation_runtime_support_factory=lambda *a, **k: None,
            market_event_ingest_support_factory=lambda *a, **k: None,
            strategy_promotion_pipeline_service=lambda: None,
            strategy_runtime_control_service=lambda: None,
            event_context_builder=lambda *a, **k: {},
            strategy_vector_platform_factory=lambda: None,
            execution_audit_snapshot_builder=lambda *a, **k: {},
            closure_review_builder=lambda *a, **k: {},
            strategy_llm_provider_loader=lambda *a, **k: None,
        )

        runtimes = {
            "factor_mining": get_factor_mining_runtime,
            "incubation": get_incubation_runtime,
            "signal_tracker": get_signal_tracker_runtime,
            "market_event_ingest": get_market_event_ingest_runtime,
        }

        report = {}
        for name, factory in runtimes.items():
            entry = {}
            try:
                runtime = factory()
                entry["preflight"] = bool(runtime.preflight() is not None)
                entry["status"] = bool(runtime.status() is not None)
                result = asyncio.run(runtime.run_once())
                # An empty-data run may legitimately report failure, but it must
                # return a structured payload rather than raise.
                entry["run_once"] = isinstance(result, dict)
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            report[name] = entry

        print(_json.dumps({"report": report, "blocked": BLOCKED}))
        """
    )

    assert completed.returncode == 0, completed.stderr

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    report = payload["report"]

    failures = {name: entry for name, entry in report.items() if "error" in entry}
    assert not failures, (
        "all four runtimes must satisfy preflight/status/run_once with injected "
        f"providers and no akshare_mcp host. Failures: {failures}"
    )
    for name, entry in report.items():
        assert entry["preflight"] is True, f"{name}: preflight returned nothing"
        assert entry["status"] is True, f"{name}: status returned nothing"
        assert entry["run_once"] is True, f"{name}: run_once did not return a dict"

    assert payload["blocked"] == [], (
        "the runtime contract must not attempt to import akshare_mcp; "
        f"attempted: {payload['blocked']}"
    )


def test_runtime_contract_raises_when_no_providers_are_configured() -> None:
    """With nothing injected, the contract must fail loudly, not pretend to work."""
    completed = _run(
        """
        import json as _json

        from strategy_factory.infrastructure.runtime_services import clear_runtime_services
        from strategy_factory.runtime import get_factor_mining_runtime

        clear_runtime_services()
        try:
            get_factor_mining_runtime()
            outcome = {"raised": None}
        except Exception as exc:
            outcome = {"raised": type(exc).__name__, "message": str(exc)[:200]}
        print(_json.dumps(outcome))
        """
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])

    assert outcome["raised"] == "RuntimeError", (
        "an unconfigured runtime must raise rather than silently degrade"
    )
    assert "missing_provider" in outcome["message"], (
        "the error must name the missing provider so operators can act on it"
    )


def test_phase1_exit_condition_real_cycle_without_the_host(tmp_path) -> None:
    """Phase 1 exit condition: a real cycle for all four runtimes, host absent.

    This was an ``xfail(strict=True)`` sentinel for as long as the only provider
    set lived in ``akshare_mcp``. The standalone provider set now satisfies the
    full required surface using Strategy Factory and quant-core alone, so this is
    a live assertion.

    Every runtime must complete ``preflight`` / ``status`` / ``run_once`` and
    return a payload carrying no ``error``, with ``akshare_mcp`` blocked at
    ``sys.meta_path`` throughout.
    """
    completed = _run(
        f"""
        import asyncio
        import json as _json
        import logging
        import os

        logging.disable(logging.ERROR)
        os.environ["AIASK_SQLITE_PATH"] = {str(tmp_path / "standalone.sqlite3")!r}

        from strategy_factory.infrastructure.runtime_services import clear_runtime_services
        from strategy_factory.runtime.standalone_providers import (
            configure_standalone_runtime_services,
        )
        from strategy_factory.runtime.default_bootstrap import (
            get_missing_required_runtime_providers,
        )
        from strategy_factory.runtime import (
            get_factor_mining_runtime,
            get_incubation_runtime,
            get_market_event_ingest_runtime,
            get_signal_tracker_runtime,
        )

        clear_runtime_services()
        configure_standalone_runtime_services()

        report = {{"missing_providers": sorted(get_missing_required_runtime_providers())}}
        runtimes = {{
            "factor_mining": get_factor_mining_runtime,
            "incubation": get_incubation_runtime,
            "signal_tracker": get_signal_tracker_runtime,
            "market_event_ingest": get_market_event_ingest_runtime,
        }}
        cycles = {{}}
        for name, factory in runtimes.items():
            runtime = factory()
            entry = {{
                "preflight": runtime.preflight() is not None,
                "status": runtime.status() is not None,
            }}
            result = asyncio.run(runtime.run_once())
            entry["is_mapping"] = isinstance(result, dict)
            entry["error"] = (result or {{}}).get("error")
            cycles[name] = entry
        report["cycles"] = cycles
        report["blocked"] = BLOCKED
        print(_json.dumps(report))
        """
    )
    assert completed.returncode == 0, completed.stderr

    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["missing_providers"] == [], (
        "the standalone provider set must satisfy every required provider; "
        f"missing: {payload['missing_providers']}"
    )
    for name, entry in payload["cycles"].items():
        assert entry["preflight"] is True, f"{name}: preflight returned nothing"
        assert entry["status"] is True, f"{name}: status returned nothing"
        assert entry["is_mapping"] is True, f"{name}: run_once did not return a payload"
        assert entry["error"] is None, f"{name}: cycle reported {entry['error']}"

    assert payload["blocked"] == [], (
        "a standalone cycle must not attempt to import akshare_mcp; "
        f"attempted: {payload['blocked']}"
    )


def test_standalone_results_are_marked_as_having_no_data_provider() -> None:
    """An empty standalone cycle must never be mistaken for a real one."""
    completed = _run(
        """
        import json as _json

        from strategy_factory.runtime.standalone_providers import STANDALONE_MARKER

        print(_json.dumps({"marker": STANDALONE_MARKER}))
        """
    )
    assert completed.returncode == 0, completed.stderr
    marker = json.loads(completed.stdout.strip().splitlines()[-1])["marker"]

    assert "standalone" in marker and "no_data" in marker, (
        "the marker must make the absence of a data provider self-evident"
    )


def test_default_bootstrap_selects_a_neutral_provider_set_when_no_host_exists() -> None:
    """The default entry point must be host-agnostic, not merely capable.

    This assertion replaced one that documented the opposite: the bootstrap used
    to hardcode the AKShare configurator, so blocking that package left providers
    unregistered. Selection is now driven by availability, so with the host
    blocked the in-package standalone set is chosen automatically — no explicit
    configuration, no missing providers.
    """
    completed = _run(
        """
        import json as _json
        import os
        import tempfile

        os.environ["AIASK_SQLITE_PATH"] = tempfile.mktemp(suffix=".sqlite3")

        from strategy_factory.infrastructure.runtime_services import clear_runtime_services
        from strategy_factory.runtime.default_bootstrap import (
            _resolve_configurator_path,
            ensure_default_runtime_services,
            get_missing_required_runtime_providers,
            resolve_configurator_candidates,
            runtime_services_ready,
        )

        clear_runtime_services()
        outcome = {
            "ready_before": runtime_services_ready(),
            "selected": _resolve_configurator_path(),
            "candidates": resolve_configurator_candidates(),
        }
        try:
            ensure_default_runtime_services()
            outcome["raised"] = None
        except Exception as exc:
            outcome["raised"] = f"{type(exc).__name__}: {exc}"
        outcome["missing_after"] = sorted(get_missing_required_runtime_providers())
        print(_json.dumps(outcome))
        """
    )
    assert completed.returncode == 0, completed.stderr

    outcome = json.loads(completed.stdout.strip().splitlines()[-1])

    assert outcome["ready_before"] is False
    assert outcome["raised"] is None, (
        f"the default bootstrap must succeed without a host; raised {outcome['raised']}"
    )
    assert outcome["selected"].startswith("strategy_factory.runtime.standalone_providers"), (
        f"expected the in-package provider set, got {outcome['selected']}"
    )
    assert outcome["missing_after"] == [], (
        "the default path must leave no required provider unregistered; "
        f"missing: {outcome['missing_after']}"
    )

    # The host candidate is still preferred when present; it is simply absent here.
    host_candidate = outcome["candidates"][0]
    assert host_candidate["available"] is False
    assert any(entry["available"] for entry in outcome["candidates"])


def test_an_explicitly_named_configurator_is_never_silently_swapped() -> None:
    """Naming a configurator that cannot load must error, not fall back.

    Silent substitution would mean an operator asking for one provider set and
    unknowingly getting another.
    """
    completed = _run(
        """
        import json as _json

        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services

        try:
            ensure_default_runtime_services(
                configurator_path="akshare_mcp.adapters.strategy_factory_runtime:configure_strategy_factory_runtime_services"
            )
            outcome = {"raised": None}
        except Exception as exc:
            outcome = {"raised": type(exc).__name__}
        print(_json.dumps(outcome))
        """
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])

    assert outcome["raised"] == "ModuleNotFoundError", (
        "an explicit but unloadable configurator must surface, not be replaced"
    )
