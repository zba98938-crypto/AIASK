#!/usr/bin/env python3
"""Readiness check for the Strategy Factory runtime split."""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # scripts/test_factory_architecture.py -> aiask/
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "strategy-factory" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "akshare-mcp" / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "aiask-quant-core" / "src"))


def _assert_runtime_result(label: str, result: object) -> str:
    """Fail the check when a runtime reports a failed/degraded contract result."""
    if not isinstance(result, dict):
        raise RuntimeError(f"{label} returned {type(result).__name__}, expected a mapping")
    status = str(result.get("status") or "").strip().lower()
    errors = result.get("errors")
    if result.get("success") is False or status in {"failed", "blocked", "error"} or errors:
        raise RuntimeError(f"{label} returned an unhealthy result: {result}")
    return status or ("success" if result.get("success") is True else "ok")


async def test_signal_tracker():
    """Test Signal Tracker with new Provider architecture."""
    print("=" * 60)
    print("Testing Signal Tracker with new architecture")
    print("=" * 60)

    try:
        # Import new runtime
        from strategy_factory.runtime.signal_tracker import build_signal_tracker_runtime

        # Build provider (placeholder - will use mock for now)
        class MockProvider:
            async def get_db(self):
                return None

            def get_default_universe(self):
                """Get default trading universe."""
                return []

            async def load_execution_universe(self, db, **kwargs):
                return []

            async def load_executable_strategies(self, db, **kwargs):
                """Load executable strategies."""
                return []

            async def load_runtime_submitted_strategies(self, db, **kwargs):
                """Load runtime submitted strategies."""
                return []

            async def load_runtime_observation_strategies(self, db, **kwargs):
                """Load runtime observation strategies."""
                return []

            def phase_timeout_seconds(self, phase_name):
                """Get timeout for a phase."""
                return 300

            async def execute_phase_a(self, db, universe):
                return {"success": True, "strategies": []}

            async def execute_phase_b(self, db, universe):
                return {"signals_generated": 0}

            async def execute_phase_c(self, db, results):
                return {"signals_validated": 0}

            async def execute_phase_d(self, db, results):
                return {"signals_verified": 0}

            async def execute_phase_e(self, db, results):
                return {"positions_tracked": 0}

            async def execute_phase_f(self, db, results):
                return {"metrics_recorded": 0}

            async def execute_phase_g(self, db, results):
                return {"events_bridged": 0}

            async def execute_phase_h(self, db, results):
                return {"quality_score": 1.0}

            async def backfill_forward_returns(self, *args, **kwargs):
                """Backfill forward returns."""
                return {"backfilled": 0}

            async def run_runtime_risk_scan(self, db, strategies):
                """Run runtime risk scan."""
                return {"scanned": 0}

            async def run_lifecycle_scan(self, db, strategies):
                """Run lifecycle scan."""
                return {"scanned": 0}

            async def reconcile_vector_registry(self, db, strategies):
                """Reconcile vector registry."""
                return {"reconciled": 0}

            async def snapshot_domain_projections(self, db, strategies):
                """Snapshot domain projections."""
                return {"snapshotted": 0}

        provider = MockProvider()
        runtime = build_signal_tracker_runtime(support=provider)

        # Test preflight
        preflight = runtime.preflight()
        print(f"[OK] Preflight: {preflight}")

        # Test run_once
        result = await runtime.run_once()
        result_status = _assert_runtime_result("Signal Tracker", result)
        print(f"[OK] Run result: status={result_status}")
        print(f"  Universe size: {result.get('universe_size', 0)}")
        print(f"  Elapsed: {result.get('elapsed_seconds', 0):.2f}s")

        print("\n[PASS] Signal Tracker test PASSED")
        return True

    except Exception as exc:
        print(f"\n[FAIL] Signal Tracker test FAILED: {exc}")
        import traceback
        traceback.print_exc()
        return False


async def test_factor_mining():
    """Test Factor Mining with new Provider architecture."""
    print("\n" + "=" * 60)
    print("Testing Factor Mining with new architecture")
    print("=" * 60)

    try:
        from strategy_factory.runtime.factor_mining import build_factor_mining_runtime

        class MockProvider:
            async def get_db(self):
                return None

            async def validate_environment(self, db):
                return {"valid": True}

            async def ensure_persistent_pool(self, db):
                """Ensure persistent factor pool exists."""
                pass

            def get_active_pool_size(self):
                """Get active pool size."""
                return 0

            async def build_mining_context(self, db, **kwargs):
                """Build mining context."""
                return {"universe": [], "features": []}

            async def mine_factors(self, db, **kwargs):
                return {"factors_mined": 0}

            async def persist_factors(self, db, result):
                return {"factors_persisted": 0}

            async def persist_mining_run(self, db, report):
                """Persist mining run report."""
                pass

            def quality_summary(self, result):
                return {"quality_score": 1.0}

        provider = MockProvider()
        runtime = build_factor_mining_runtime(support=provider)

        preflight = runtime.preflight()
        print(f"[OK] Preflight: {preflight}")

        result = await runtime.run_once()
        result_status = _assert_runtime_result("Factor Mining", result)
        print(f"[OK] Run result: status={result_status}")
        print(f"  Factors mined: {result.get('factors_mined', 0)}")

        print("\n[PASS] Factor Mining test PASSED")
        return True

    except Exception as exc:
        print(f"\n[FAIL] Factor Mining test FAILED: {exc}")
        import traceback
        traceback.print_exc()
        return False


async def test_incubation():
    """Test Incubation with new Provider architecture."""
    print("\n" + "=" * 60)
    print("Testing Incubation with new architecture")
    print("=" * 60)

    try:
        from strategy_factory.runtime.incubation import build_incubation_runtime

        class MockProvider:
            async def get_db(self):
                return None

            async def scan_and_accept_strategies(self, db, **kwargs):
                return {"accepted": 0, "rejected": 0}

            async def list_incubating_strategies(self, db, **kwargs):
                return []

            async def list_paper_observation_strategies(self, db, **kwargs):
                return []

            async def list_diagnostic_observation_strategies(self, db, **kwargs):
                return []

            async def generate_signals(self, db, strategy, **kwargs):
                return {"signals_generated": 0}

            async def verify_forward_returns(self, db, strategy):
                return {"verified": True}

            async def record_metrics(self, db, strategy, result):
                return {"recorded": True}

            async def settle_orders(self, db, strategy, result):
                return {"settled": 0}

            async def run_pipeline(self, db, strategies):
                return {"transitions": 0, "promotions": 0, "terminations": 0}

            async def generate_hit_rate_report(self, db, strategies, results):
                return {"hit_rate": 0.0}

            def paper_runtime_status(self):
                return {"available": False}

            async def start_paper_runtime(self):
                pass

            async def stop_paper_runtime(self):
                pass

            async def run_recompile_remediation(self, db):
                return {"recompiled_count": 0, "promoted_count": 0}

            async def verify_trade_predictions(self, db):
                return {"verified_count": 0, "accuracy": None}

            async def run_accelerator(self, db):
                return {"accelerated_count": 0, "promoted_count": 0}

            async def monitor_alerts(self, db):
                return {"alerts_processed": 0, "critical_count": 0}

        provider = MockProvider()
        runtime = build_incubation_runtime(support=provider)

        preflight = runtime.preflight()
        print(f"[OK] Preflight: {preflight}")

        result = await runtime.run_once()
        result_status = _assert_runtime_result("Incubation", result)
        print(f"[OK] Run result: status={result_status}")
        print(f"  Intake accepted: {result.get('intake_accepted', 0)}")
        print(f"  Signals generated: {result.get('signals_generated', 0)}")

        print("\n[PASS] Incubation test PASSED")
        return True

    except Exception as exc:
        print(f"\n[FAIL] Incubation test FAILED: {exc}")
        import traceback
        traceback.print_exc()
        return False


async def test_market_event_ingest():
    """Test Market Event Ingest with new Provider architecture."""
    print("\n" + "=" * 60)
    print("Testing Market Event Ingest with new architecture")
    print("=" * 60)

    try:
        from strategy_factory.runtime.market_event_ingest import build_market_event_ingest_runtime

        class MockProvider:
            async def get_db(self):
                return None

            async def fetch_official_market_events(self, db, **kwargs):
                return []

            async def list_normalized_events(self, db, **kwargs):
                return []

            async def save_normalized_event(self, db, event):
                return {"saved": False, "event_id": event.get("event_id")}

            async def save_factory_event_cluster(self, db, cluster):
                return {"saved": False, "cluster_id": cluster.get("cluster_id")}

            async def save_factory_event_signal(self, db, signal):
                return {"saved": False, "signal_id": signal.get("event_id")}


        provider = MockProvider()
        runtime = build_market_event_ingest_runtime(
            support=provider,
            db_provider=lambda: None,
        )

        preflight = runtime.preflight()
        print(f"[OK] Preflight: {preflight}")

        result = await runtime.run_once()
        result_status = _assert_runtime_result("Market Event Ingest", result)
        print(f"[OK] Run result: status={result_status}")
        print(f"  Events fetched: {result.get('events_fetched', 0)}")

        print("\n[PASS] Market Event Ingest test PASSED")
        return True

    except Exception as exc:
        print(f"\n[FAIL] Market Event Ingest test FAILED: {exc}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("\nTesting Four Factory Architecture Refactor\n")

    results = []
    results.append(await test_signal_tracker())
    results.append(await test_factor_mining())
    results.append(await test_incubation())
    results.append(await test_market_event_ingest())

    print("\n" + "=" * 60)
    print(f"Test Results: {sum(results)}/{len(results)} passed")
    print("=" * 60)

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
