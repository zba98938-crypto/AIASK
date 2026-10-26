"""Factory-submitted predictions take ``prediction_as_of`` from the market session.

DataCollector keys the snapshot on the collection day (``date``).  When that
was the only date available, a Saturday factory cycle labelled 657
predictions 2026-10-24.  The snapshot now carries ``prediction_as_of`` from
the newest synced ``kline_1d`` session, and contract derivation prefers it.
"""

from __future__ import annotations

from strategy_factory.application.trade_prediction_contract import attach_trade_prediction_context


def test_session_as_of_wins_over_collection_day():
    snapshot = {"date": "2026-10-24", "prediction_as_of": "2026-10-23"}
    payload = attach_trade_prediction_context({"id": "c1", "params": {}}, snapshot=snapshot)
    assert payload["prediction_as_of"] == "2026-10-23"
    assert payload["params"]["prediction_as_of"] == "2026-10-23"
    # The collection-day key is still carried for snapshot lookups.
    assert payload["snapshot_date"] == "2026-10-24"


def test_without_a_synced_session_the_collection_day_is_still_used():
    payload = attach_trade_prediction_context({"id": "c1", "params": {}}, snapshot={"date": "2026-10-23"})
    assert payload["prediction_as_of"] == "2026-10-23"
