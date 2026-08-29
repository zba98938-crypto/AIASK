"""FIX-12 回归测试：data_source 层指数代码识别与路由。

根因：data_source.get_kline 首行 normalize_code() 把 sh000001/000001.SH 碾平为
裸 000001，再经 tqcenter 取成深市个股（平安银行 11 元）。修复在 normalize 之前
拦截指数代码并路由到指数专用取数。

本测试覆盖纯函数 `_resolve_index_storage_code` 的判定矩阵（确定性、不依赖网络/DB）。
"""

import pytest

from akshare_mcp.data_source.quotes import _resolve_index_storage_code


@pytest.mark.parametrize(
    "code,expected",
    [
        # 指数：带市场标识 + 指数号段
        ("sh000001", "sh000001"),   # 上证指数（前缀）
        ("SH000001", "sh000001"),   # 大小写不敏感
        ("000001.SH", "sh000001"),  # 上证指数（后缀）
        ("000001.sh", "sh000001"),
        ("sh000300", "sh000300"),   # 沪深300
        ("sh000016", "sh000016"),   # 上证50
        ("sh000905", "sh000905"),   # 中证500
        ("sz399001", "sz399001"),   # 深证成指
        ("399006.SZ", "sz399006"),  # 创业板指（后缀）
        ("sz399006", "sz399006"),
        # 个股 / 非指数：返回 None（按个股语义处理）
        ("000001", None),           # 裸码 = 平安银行（向后兼容）
        ("000001.SZ", None),        # 深市个股 000001 = 平安银行
        ("sz000001", None),         # 深市 000 段不是指数
        ("sh600519", None),         # 沪市个股茅台
        ("600519", None),
        ("600519.SH", None),
        ("510050", None),           # 50ETF
        ("510050.SH", None),        # ETF 带后缀也非指数
        ("399006", None),           # 裸 399 无市场标识 → 个股语义
        ("000300", None),           # 裸 000300 无市场标识 → 个股语义
        ("", None),
        ("abc", None),
    ],
)
def test_resolve_index_storage_code(code, expected):
    assert _resolve_index_storage_code(code) == expected


def test_index_codes_never_collide_with_bare_stock():
    """裸 6 位代码（无市场标识）必须一律按个股，保证 000001=平安银行 向后兼容。"""
    for bare in ["000001", "000300", "000016", "000905", "399001", "399006", "600519"]:
        assert _resolve_index_storage_code(bare) is None
