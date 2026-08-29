from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_root_runners_no_longer_import_legacy_akshare_bootstrap() -> None:
    runner_paths = [
        ROOT / "scripts" / "factories" / "run_strategy_factory.py",
        ROOT / "scripts" / "factories" / "run_factor_mining_factory.py",
        ROOT / "scripts" / "factories" / "run_incubation_factory.py",
        ROOT / "scripts" / "factories" / "run_signal_tracker.py",
        ROOT / "scripts" / "factories" / "run_market_event_ingest.py",
    ]

    for path in runner_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "akshare_mcp.runtime.strategy_factory_bootstrap" not in text
        assert "strategy_factory.runtime.default_bootstrap" in text


def test_strategy_runner_no_longer_imports_legacy_adapter_bridge() -> None:
    text = (ROOT / "scripts" / "factories" / "run_strategy_factory.py").read_text(
        encoding="utf-8",
        errors="ignore",
    )
    assert "akshare_mcp.adapters.strategy_factory_runtime" not in text


def test_canonical_bootstrap_skips_akshare_import_when_runtime_services_ready() -> None:
    code = textwrap.dedent(
        f"""
        import importlib.abc
        import sys

        class _BlockAkshare(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if str(fullname).startswith("akshare_mcp"):
                    raise ModuleNotFoundError("blocked akshare import", name=fullname)
                return None

        sys.meta_path.insert(0, _BlockAkshare())
        sys.path.insert(0, {str(ROOT / "packages" / "strategy-factory" / "src")!r})
        sys.path.insert(0, {str(ROOT / "packages" / "aiask-quant-core" / "src")!r})

        from strategy_factory.infrastructure.runtime_services import clear_runtime_services, configure_runtime_services
        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services, runtime_services_ready

        class _Db:
            pass

        clear_runtime_services()
        configure_runtime_services(
            db_provider=lambda: _Db(),
            factor_scheduler=lambda: None,
            factor_mining_factory=lambda: None,
            factor_mining_support_factory=lambda: None,
            factor_pool_gateway=lambda: None,
            quant_manager_callable=lambda *args, **kwargs: None,
            runtime_warmup_runner=lambda *args, **kwargs: None,
            signal_tracker_runtime_factory=lambda *args, **kwargs: None,
            signal_tracker_runtime_support_factory=lambda *args, **kwargs: None,
            incubation_runtime_factory=lambda *args, **kwargs: None,
            incubation_runtime_support_factory=lambda *args, **kwargs: None,
            market_event_ingest_support_factory=lambda: None,
            strategy_promotion_pipeline_service=lambda: None,
            strategy_runtime_control_service=lambda: None,
            event_context_builder=lambda *args, **kwargs: {{}},
            strategy_vector_platform_factory=lambda: None,
            execution_audit_snapshot_builder=lambda *args, **kwargs: {{}},
            closure_review_builder=lambda *args, **kwargs: {{}},
            strategy_llm_provider_loader=lambda *args, **kwargs: None,
        )
        assert runtime_services_ready() is True
        ensure_default_runtime_services()
        print("ok")
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout
