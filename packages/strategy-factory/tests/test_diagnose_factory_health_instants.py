"""Ordering two stored timestamps needs parsing, not string comparison.

`paper_trades.trade_time` is written as `2026-10-18T00:00:00+00:00` and the audit
snapshot's `updated_at` as `2026-10-18 08:17:14`. They diverge at index 10, where
`T` outranks a space, so comparing the strings puts a trade from midnight *after* a
verdict from eight in the morning. Every same-day pair then reads as stale — which
is what the audit-gate classification did before this helper existed.

This is the same class of mistake the project records elsewhere: a column written by
two producers in two formats, and a consumer that never noticed.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def diagnostic():
    spec = importlib.util.spec_from_file_location(
        "diagnose_factory_health_under_test",
        REPO_ROOT / "scripts" / "factories" / "diagnose_factory_health.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestParsing:
    def test_both_stored_formats_parse(self, diagnostic):
        for text in ("2026-10-18T00:00:00+00:00", "2026-10-18 08:17:14", "2026-07-15T13:25:10.885536+00:00"):
            assert isinstance(diagnostic._as_instant(text), datetime), text

    def test_a_trailing_z_is_accepted(self, diagnostic):
        assert diagnostic._as_instant("2026-10-18T00:00:00Z") == datetime(2026, 10, 18, 0, 0, 0)

    @pytest.mark.parametrize("bad", ["", None, "not-a-date", "T02:43:32.548408+00:00"])
    def test_an_unusable_value_is_none_rather_than_a_guess(self, diagnostic, bad):
        assert diagnostic._as_instant(bad) is None


class TestOrdering:
    def test_a_same_day_trade_before_a_verdict_orders_before_it(self, diagnostic):
        """The case the string comparison got backwards.

        The trade is at 00:00 and the verdict at 08:17, so the verdict is the newer
        of the two and the strategy is **not** stale on this pair. String comparison
        said the opposite, which inflated the stale count.
        """

        trade = diagnostic._as_instant("2026-10-18T00:00:00+00:00")
        verdict = diagnostic._as_instant("2026-10-18 08:17:14")

        assert trade < verdict
        assert not (trade > verdict), "the string form compares as later; parsing must not"
        assert "2026-10-18T00:00:00+00:00" > "2026-10-18 08:17:14", (
            "documents the trap: the raw strings really do order the wrong way"
        )

    def test_a_trade_after_the_verdict_orders_after_it(self, diagnostic):
        trade = diagnostic._as_instant("2026-10-18T09:00:00+00:00")
        verdict = diagnostic._as_instant("2026-10-18 08:17:14")
        assert trade > verdict

    def test_cross_month_ordering_is_unaffected(self, diagnostic):
        assert diagnostic._as_instant("2026-07-15T13:25:10+00:00") < diagnostic._as_instant("2026-10-18 08:17:14")
