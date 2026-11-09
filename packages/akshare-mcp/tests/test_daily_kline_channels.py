"""The daily-bar channel chain, and the two traps it exists to avoid.

The chain was added because 5,582 symbols depended on one endpoint. What makes it
safe is not the retry — it is that the alternative channels were first measured
against the incumbent and found to differ in two ways that no retry would notice:

* East money and Tencent report volume in 手 while Sina reports 股, exactly 100x.
* East money will answer with a *different security* for a reassigned 北交所 code
  (``430047`` returns a 2020 series while the stored one runs to 2025).

Both are asserted here on synthetic payloads, so the tests do not need the network
and cannot pass by accident when a provider changes shape.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def daily_sync():
    """Load ``scripts/free_data_daily_sync.py`` without importing the whole tool."""

    spec = importlib.util.spec_from_file_location(
        "daily_sync_under_test", REPO_ROOT / "scripts" / "free_data_daily_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class TestVolumeUnits:
    def test_sina_does_not_invent_turnover(self, daily_sync):
        payload = [{"day": "2026-11-05", "open": "10", "high": "12", "low": "9", "close": "11", "volume": "1000"}]
        with patch("requests.Session.get", return_value=_response(payload)):
            row = daily_sync._fetch_sina_kline("000001", datalen=1)[0]
        assert row["amount"] is None
        assert row["amount_unavailable_reason"] == "source_does_not_publish_amount"

    @pytest.mark.parametrize("raw,expected", [("", None), ("0", 0.0)])
    def test_eastmoney_distinguishes_missing_amount_and_measured_zero(self, daily_sync, raw, expected):
        payload = {"data": {"klines": [f"2026-11-05,10,11,12,9,0,{raw}"]}}
        with patch("requests.Session.get", return_value=_response(payload)):
            row = daily_sync._fetch_eastmoney_kline("000001", datalen=1)[0]
        assert row["amount"] == expected

    def test_tencent_does_not_invent_turnover_or_missing_volume(self, daily_sync):
        payload = {"data": {"sz000001": {"day": [["2026-11-05", "10", "11", "12", "9"]]}}}
        with patch("requests.Session.get", return_value=_response(payload)):
            row = daily_sync._fetch_tencent_kline("000001", datalen=1)[0]
        assert row["amount"] is None
        assert row["volume"] is None

    def test_eastmoney_lots_are_converted_to_shares(self, daily_sync):
        """``f56`` is 手. Written through unconverted it would be 100x too small.

        The stored ``kline_1d.volume`` column is in 股 because Sina fills it, so a
        fallback that skipped the conversion would mix two units in one column —
        visible only by comparing a fallback row against its neighbours.
        """

        payload = {"data": {"klines": ["2026-10-15,1.0,1312.97,1320.0,1300.0,57677,7572845954"]}}
        with patch("requests.Session.get", return_value=_response(payload)):
            rows = daily_sync._fetch_eastmoney_kline("600519", datalen=1)

        assert len(rows) == 1
        assert rows[0]["volume"] == 57677 * 100
        assert rows[0]["close"] == 1312.97
        # f57 is the real turnover; close x volume is only an approximation of it.
        assert rows[0]["amount"] == 7572845954

    def test_tencent_lots_are_converted_to_shares(self, daily_sync):
        payload = {"data": {"sh600519": {"day": [["2026-10-15", "1300", "1312.97", "1320", "1295", "57677"]]}}}
        with patch("requests.Session.get", return_value=_response(payload)):
            rows = daily_sync._fetch_tencent_kline("600519", datalen=1)

        assert rows[0]["volume"] == 57677 * 100
        assert rows[0]["close"] == 1312.97

    def test_eastmoney_asks_for_unadjusted_bars(self, daily_sync):
        """Sina's endpoint has no adjustment parameter, so it returns 不复权.

        Measured over 120 days, 12 of 12 sampled stocks matched East money's
        ``fqt=0`` and none matched ``fqt=1``. Requesting ``fqt=1`` would blend
        前复权 prices into a 不复权 series for every stock that paid a dividend.
        """

        captured: dict = {}

        def _capture(url, params=None, **kwargs):
            captured.update(params or {})
            return _response({"data": {"klines": ["2026-10-15,1.0,1.0,1.0,1.0,1,1"]}})

        with patch("requests.Session.get", side_effect=_capture):
            daily_sync._fetch_eastmoney_kline("600519", datalen=1)

        assert captured.get("fqt") == 0


class TestChannelChain:
    def test_falls_back_when_the_primary_raises(self, daily_sync, monkeypatch):
        monkeypatch.setitem(
            daily_sync.KLINE_FETCHERS,
            "sina",
            lambda code, **kw: (_ for _ in ()).throw(ConnectionError("outage")),
        )
        monkeypatch.setitem(daily_sync.KLINE_FETCHERS, "eastmoney", lambda code, **kw: [{"date": "2026-10-15"}])

        result = daily_sync.fetch_daily_bars("600519", datalen=1, order=("sina", "eastmoney"))

        assert result["channel"] == "eastmoney"
        assert result["is_fallback"] is True
        assert "ConnectionError" in result["attempts"][0]["outcome"]

    def test_an_empty_response_moves_to_the_next_channel(self, daily_sync, monkeypatch):
        """Empty is not success, and it is not the end of the chain either."""

        monkeypatch.setitem(daily_sync.KLINE_FETCHERS, "sina", lambda code, **kw: [])
        monkeypatch.setitem(daily_sync.KLINE_FETCHERS, "eastmoney", lambda code, **kw: [{"date": "2026-10-15"}])

        result = daily_sync.fetch_daily_bars("600519", order=("sina", "eastmoney"))

        assert result["channel"] == "eastmoney"
        assert result["attempts"][0]["outcome"] == "empty"

    def test_every_channel_failing_is_a_failure_not_an_empty_series(self, daily_sync, monkeypatch):
        for channel in daily_sync.KLINE_CHANNELS:
            monkeypatch.setitem(
                daily_sync.KLINE_FETCHERS,
                channel,
                lambda code, **kw: (_ for _ in ()).throw(RuntimeError("down")),
            )

        result = daily_sync.fetch_daily_bars("600519")

        assert result["all_channels_failed"] is True
        assert result["rows"] == []
        assert len(result["attempts"]) == len(daily_sync.KLINE_CHANNELS)

    def test_the_order_argument_is_read_at_call_time(self, daily_sync, monkeypatch):
        """A tuple bound as a default argument freezes the module constant.

        That bug made an override silently not apply, while the fallback *detection*
        used the new value — so every symbol was reported as a fallback.
        """

        monkeypatch.setattr(daily_sync, "KLINE_CHANNELS", ("tencent",))
        monkeypatch.setitem(daily_sync.KLINE_FETCHERS, "tencent", lambda code, **kw: [{"date": "2026-10-15"}])

        result = daily_sync.fetch_daily_bars("600519")

        assert result["channel"] == "tencent"
        assert result["is_fallback"] is False, "the only channel in the order is the primary"


class TestSeriesIdentity:
    def test_a_series_ending_long_before_the_market_is_refused(self, daily_sync):
        """``430047`` is the live case: a reassigned 北交所 code.

        East money answers with a 2020 series for a code whose stored series runs to
        2025. Nothing overlaps and nothing was stored, so the only available signal
        is that the series does not reach the present.
        """

        assert daily_sync._days_between("2020-06-29", "2026-10-16") == 2300
        assert daily_sync._days_between("not-a-date", "2026-10-16") is None
        assert daily_sync.STALE_SERIES_GRACE_DAYS < 2300

    def test_a_recent_series_is_not_treated_as_stale(self, daily_sync):
        """A new listing starts late; its last bar is still near the present."""

        assert daily_sync._days_between("2026-10-01", "2026-10-16") < daily_sync.STALE_SERIES_GRACE_DAYS


class TestMarketPrefixes:
    @pytest.mark.parametrize(
        "code, expected",
        [
            ("600519", "1.600519"),
            ("688981", "1.688981"),
            ("000001", "0.000001"),
            ("300750", "0.300750"),
            ("920000", "0.920000"),
            ("430047", "0.430047"),
        ],
    )
    def test_eastmoney_secid(self, daily_sync, code, expected):
        """北交所 (``92…``/``43…``) is market 0 on East money, not a separate one."""

        assert daily_sync._eastmoney_secid(code) == expected

    @pytest.mark.parametrize(
        "code, expected",
        [("600519", "sh600519"), ("000001", "sz000001"), ("920000", "bj920000"), ("430047", "bj430047")],
    )
    def test_tencent_symbol(self, daily_sync, code, expected):
        assert daily_sync._tencent_symbol(code) == expected
