from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_agent_strategy_factory_adapters_use_canonical_bootstrap() -> None:
    adapter_paths = [
        ROOT / "packages" / "agent" / "src" / "aiask_agent" / "adapters" / "strategy_factory.py",
        ROOT / "packages" / "agent" / "src" / "aiask_agent" / "adapters" / "desktop_ops.py",
    ]

    for path in adapter_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "strategy_factory.runtime.default_bootstrap" in text
        assert "ensure_default_runtime_services()" in text
        assert "akshare_mcp.runtime.strategy_factory_bootstrap" not in text
        assert "build_strategy_factory_scheduler_kwargs" not in text
