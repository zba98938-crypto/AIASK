"""Smoke tests for Deduplicator import and basic structure."""

from __future__ import annotations


def test_deduplicator_import():
    from strategy_factory.application.deduplicator import Deduplicator

    dedup = Deduplicator()
    assert dedup is not None
    assert hasattr(dedup, "deduplicate")
