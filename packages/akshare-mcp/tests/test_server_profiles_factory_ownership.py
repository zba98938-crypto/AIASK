from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_server_profiles_no_longer_embed_factory_owned_runtimes() -> None:
    text = (ROOT / "src" / "akshare_mcp" / "server.py").read_text(
        encoding="utf-8",
        errors="ignore",
    )

    assert '_try_start("FactorScheduler"' not in text
    assert '_try_start("MatchingEngine"' not in text
    assert '_try_start("NavEngine"' not in text
    assert '_try_start("SignalTracker"' not in text
    assert "embedded StrategyFactory startup has been removed" in text
    assert "standalone runtimes" in text
