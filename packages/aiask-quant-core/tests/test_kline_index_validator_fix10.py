"""FIX-10 回归测试：入库校验器按代码+市场区分个股/指数价格区间。

历史 bug：bare 000001（平安银行，深市个股，股价≈11 元）被校验器当成上证指数，
按 [1000,30000] 拒绝入库（dead-letter 持续证据）。

修复：指数 K 线在本系统始终以带市场前缀的代码入库（sh000001 / sz399006），
故 _is_chinese_index_code 仅对显式前缀代码做区间校验；裸 6 位码视为个股。
"""

import pytest

from aiask_quant_core.core.validators import (
    _is_chinese_index_code,
    validate_kline,
)


def _row(code: str, close: float) -> dict:
    return {
        "date": "2026-05-29",
        "code": code,
        "open": close,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": 1000,
    }


@pytest.mark.parametrize(
    "code,is_index",
    [
        ("000001", False),   # 平安银行（深市个股）
        ("000300", False),   # 裸码一律视为个股
        ("600519", False),
        ("399006", False),   # 裸 399 码视为个股（指数走 sz 前缀）
        ("sh000001", True),  # 上证指数（显式前缀）
        ("sz399001", True),  # 深证成指（显式前缀）
        ("sz399006", True),  # 创业板指
        ("index_000001", True),
    ],
)
def test_fix10_index_code_detection(code, is_index):
    assert _is_chinese_index_code(code) is is_index


def test_fix10_bare_000001_stock_price_accepted():
    # 平安银行 close=11.61 不再被指数区间 [1000,30000] 拒绝
    validated = validate_kline(_row("000001", 11.61))
    assert float(validated.close) == pytest.approx(11.61)


def test_fix10_bare_stock_codes_accepted():
    for code, close in [("000300", 5.0), ("600519", 1600.0), ("399006", 12.3)]:
        validated = validate_kline(_row(code, close))
        assert validated is not None


def test_fix10_prefixed_index_contamination_still_rejected():
    # sh000001 写入 11 元（cross-symbol 污染）仍必须拒绝
    with pytest.raises(ValueError, match="index_close_out_of_range"):
        validate_kline(_row("sh000001", 11.0))
    with pytest.raises(ValueError, match="index_close_out_of_range"):
        validate_kline(_row("sz399001", 11.0))


def test_fix10_prefixed_index_normal_value_accepted():
    assert validate_kline(_row("sh000001", 4068.0)) is not None
    assert validate_kline(_row("sz399006", 2200.0)) is not None
