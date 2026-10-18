"""Volume units are per market, and getting them wrong is invisible.

``kline_1d.volume`` is 股 because Sina fills it that way. East money reports 手 in
every market, so its value is multiplied by 100. Tencent reports 手 in every market
*except* 科创板, where it already states shares — so a blanket ×100 makes 688xxx
bars 100× too large.

None of that shows up in the response: the bars parse, the closes match to the last
decimal, and only the volume is off. It was found by the sync's fallback guard,
which compares a fallback against stored history before writing and refused 57
科创板 symbols with ``volume ratio 100.0 is not 1.0``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from akshare_mcp.data_source.volume_units import (
    LOTS_TO_SHARES,
    is_tencent_share_denominated,
    tencent_volume_to_shares,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def daily_sync():
    spec = importlib.util.spec_from_file_location(
        "daily_sync_units_under_test", REPO_ROOT / "scripts" / "free_data_daily_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class TestMarketRule:
    @pytest.mark.parametrize(
        "code", ["688079", "688301", "689009", "sh688079", "SH688079", "688001"]
    )
    def test_kechuang_is_already_in_shares(self, code):
        assert is_tencent_share_denominated(code) is True
        assert tencent_volume_to_shares(code, 10_653_233) == 10_653_233

    @pytest.mark.parametrize(
        "code",
        [
            "600519",  # 沪主板
            "000001",  # 深主板
            "002594",  # 深主板
            "300750",  # 创业板
            "920000",  # 北交所
            "430047",
            "sh600519",
            "sz000001",
            "bj920000",
        ],
    )
    def test_every_other_market_is_in_lots(self, code):
        assert is_tencent_share_denominated(code) is False
        assert tencent_volume_to_shares(code, 57_677) == 57_677 * LOTS_TO_SHARES

    def test_a_market_prefix_does_not_hide_the_board(self):
        """``sh688079`` must be recognised, not only the bare code."""

        assert tencent_volume_to_shares("sh688079", 100) == 100
        assert tencent_volume_to_shares("SZ300750", 100) == 10_000

    def test_a_non_numeric_value_raises_rather_than_becoming_zero(self):
        """0.0 would write ``volume = 0``, which reads as "this bar had no trading".

        Substituting a zero for something unmeasurable is the defect this project
        treats as worse than an error, because a reader cannot tell it apart from a
        real zero.
        """

        with pytest.raises(TypeError):
            tencent_volume_to_shares("600519", None)
        with pytest.raises(TypeError):
            tencent_volume_to_shares("600519", {})
        with pytest.raises(TypeError):
            tencent_volume_to_shares("600519", True)
        # A numeric string is a number the caller has already parsed textually.
        assert tencent_volume_to_shares("600519", "57677") == 5_767_700


class TestFetchersApplyIt:
    def test_tencent_daily_does_not_scale_kechuang(self, daily_sync):
        """The measured case: 688079's raw field is already the 股 figure.

        Sina and the stored history both say 10,653,233 for 2026-10-15, and Tencent
        states the same number. Multiplying by 100 produced 1,065,323,300.
        """

        payload = {"data": {"sh688079": {"day": [["2026-10-15", "12.6", "12.63", "12.8", "12.5", "10653233"]]}}}
        with patch("requests.Session.get", return_value=_response(payload)):
            rows = daily_sync._fetch_tencent_kline("688079", datalen=1)

        assert rows[0]["volume"] == 10_653_233
        assert rows[0]["amount"] == pytest.approx(12.63 * 10_653_233)

    def test_tencent_daily_still_scales_other_markets(self, daily_sync):
        payload = {"data": {"sh600519": {"day": [["2026-10-15", "1300", "1312.97", "1320", "1295", "57677"]]}}}
        with patch("requests.Session.get", return_value=_response(payload)):
            rows = daily_sync._fetch_tencent_kline("600519", datalen=1)

        assert rows[0]["volume"] == 5_767_700

    def test_tencent_minute_does_not_scale_kechuang(self):
        """The same rule on the minute path, which had the same blanket ×100."""

        from akshare_mcp.tools.market.kline.kline_minute import _get_minute_kline_from_tencent

        payload = {
            "code": 0,
            "data": {"sh688079": {"m5": [["202610151500", "12.6", "12.63", "12.8", "12.5", "177698"]]}},
        }
        with patch("requests.get", return_value=_response(payload)):
            rows = _get_minute_kline_from_tencent("688079", 5, 10)

        assert rows and rows[0]["volume"] == 177_698

    def test_tencent_minute_still_scales_other_markets(self):
        from akshare_mcp.tools.market.kline.kline_minute import _get_minute_kline_from_tencent

        payload = {
            "code": 0,
            "data": {"sh600519": {"m5": [["202610151500", "1300", "1312.97", "1320", "1295", "2655"]]}},
        }
        with patch("requests.get", return_value=_response(payload)):
            rows = _get_minute_kline_from_tencent("600519", 5, 10)

        assert rows and rows[0]["volume"] == 265_500
