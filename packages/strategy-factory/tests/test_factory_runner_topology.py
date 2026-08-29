from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_supervisor_keeps_four_runtime_topology_and_excludes_sidecar() -> None:
    text = (ROOT / "scripts" / "factories" / "run_three_factories.py").read_text(
        encoding="utf-8",
        errors="ignore",
    )

    assert '"run_strategy_factory.py"' in text
    assert '"run_factor_mining_factory.py"' in text
    assert '"run_incubation_factory.py"' in text
    assert '"run_market_event_ingest.py"' in text
    assert '"run_signal_tracker.py"' not in text


def test_supervisor_docstring_disambiguates_the_three_overlapping_products() -> None:
    """The filename says three, the topology is four, and two other commands
    sound like the same thing. The header has to say so, because conflating them
    is how "the cycle ran" becomes "24h ops are proven".
    """
    text = (ROOT / "scripts" / "factories" / "run_three_factories.py").read_text(
        encoding="utf-8",
        errors="ignore",
    )
    header = text.split('"""')[1]

    assert "four-runtime supervisor" in header.lower()
    assert "FOUR runtimes, not three" in header
    # Must point at the two commands it is most often confused with.
    assert "quality_session" in header
    assert "factory cycle" in header
    # And must not let SignalTracker be counted as the fourth runtime.
    assert "sidecar" in header.lower()


def test_supervisor_log_dir_uses_product_name_and_keeps_legacy_history() -> None:
    """Renaming the log dir must not orphan an in-flight soak's logs."""
    text = (ROOT / "scripts" / "factories" / "run_three_factories.py").read_text(
        encoding="utf-8",
        errors="ignore",
    )

    assert '"four_runtime_supervisor"' in text
    # The legacy path is still honoured when it already exists.
    assert '"three_factories"' in text
    assert "_LEGACY_LOG_DIR.is_dir()" in text


def test_run_all_factories_delegates_to_maintained_supervisor_and_keeps_signal_tracker_spec() -> None:
    text = (ROOT / "scripts" / "factories" / "run_all_factories.py").read_text(
        encoding="utf-8",
        errors="ignore",
    )

    assert "run_three_factories.py" in text
    assert "run_signal_tracker.py" in text
