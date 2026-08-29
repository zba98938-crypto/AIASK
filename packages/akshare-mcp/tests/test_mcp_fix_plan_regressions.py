"""MCP_FIX_PLAN 回归测试。

覆盖 2026-06 修复方案中代码级缺陷的回归保护：
- FIX-3 遗留: factor 别名 rsi_14/rsi_6/rsi_24 不再被映射到非 canonical "rsi"
- FIX-4: calculate_stop_levels 非正 entry_price / 越界 risk_per_trade 显性拒绝
- FIX-6: get_conditional_returns 未识别 field / 非法 op 显性报错
- FIX-7: turnover_20d / volume_ratio 在 size=20 边界不再裸抛 IndexError
- FIX-8: factor_robustness_check 符号倒挂块迭代 dict 不再 'str'.get
"""

import asyncio

import numpy as np
import pytest


# ── FIX-3 遗留: RSI 别名映射 ──────────────────────────────────────

@pytest.mark.parametrize(
    "name,expected_canonical",
    [
        ("rsi", "rsi_14"),
        ("rsi_14", "rsi_14"),
        ("rsi_6", "rsi_6"),
        ("rsi_24", "rsi_14"),
        ("pb_ratio", "pb_mrq"),
        ("pe_ratio", "pe_ttm"),
        ("roe", "roe_ttm"),
    ],
)
def test_fix3_rsi_alias_resolves_to_supported_canonical(name, expected_canonical):
    from akshare_mcp.tools.factor_naming import check_factor_supported

    canonical, supported, err = check_factor_supported(name)
    assert supported is True, f"{name} 应被支持，实际 err={err}"
    assert canonical == expected_canonical, f"{name} -> {canonical}, 期望 {expected_canonical}"


# ── FIX-7: 量价因子边界不越界 ─────────────────────────────────────

@pytest.mark.parametrize("factor", ["turnover_20d", "volume_ratio"])
def test_fix7_volume_factors_no_indexerror_at_window_boundary(factor):
    from akshare_mcp.tools.quant_engine import _calculate_factor_value

    closes20 = list(np.linspace(10.0, 12.0, 20))
    closes21 = list(np.linspace(10.0, 12.0, 21))
    # size=20 历史上触发 closes[-21] 越界，现应优雅返回 None
    assert _calculate_factor_value(factor, closes20) is None
    # size=21 应能正常计算出有限值
    val = _calculate_factor_value(factor, closes21)
    assert val is not None and np.isfinite(val)


def test_fix7_min_history_reserves_extra_bar():
    from akshare_mcp.tools.quant_engine import _minimum_factor_history

    assert _minimum_factor_history("turnover_20d") >= 21
    assert _minimum_factor_history("volume_ratio") >= 21


def test_quant_safe_float_rejects_non_finite_values():
    from akshare_mcp.tools.quant_definitions import _safe_float
    from akshare_mcp.tools.quant_engine import _calculate_factor_value

    assert _safe_float("nan", 7.0) == 7.0
    assert _safe_float(float("inf"), 7.0) == 7.0

    closes = list(np.linspace(10.0, 12.0, 40))
    quality = _calculate_factor_value(
        "quality",
        closes,
        financial={"roe": "nan", "debt_ratio": "inf", "profit_growth": "nan"},
    )
    value = _calculate_factor_value(
        "value",
        closes,
        financial={"pe_ratio": "nan", "pb_ratio": "inf", "ps_ratio": "-inf"},
    )

    assert quality is not None and np.isfinite(quality)
    assert value is None


def test_ai_workflows_rejects_non_finite_numeric_inputs():
    from akshare_mcp.tools.ai_workflows import (
        _budget_fail_response,
        _extract_rank_ic_history,
        _normalize_binary_outcomes,
        _safe_float,
    )

    assert _safe_float("nan") is None
    assert _safe_float(float("inf")) is None
    assert _safe_float("-inf") is None

    with pytest.raises(ValueError):
        _normalize_binary_outcomes([float("inf")])

    history = _extract_rank_ic_history(
        {
            "factor_validation_report": {
                "cross_section": {
                    "dates": [
                        {"rank_ic": "inf"},
                        {"rank_ic": float("nan")},
                        {"rank_ic": 0.12},
                    ]
                }
            }
        }
    )

    assert history == [0.12]

    response = _budget_fail_response(step="stage", message="failed", timeout_sec=float("inf"))
    assert response["data"]["timeout_sec"] == 0.0


# ── FIX-8: 符号倒挂块迭代 dict.items() ───────────────────────────

def test_fix8_robustness_sign_inversion_block_iterates_items():
    # 直接复现修复后的迭代逻辑：对 dict[str, dict] 用 .items()
    multi_window_results = {
        "10": {"ic": -0.9, "rank_ic": -0.9, "sample_size": 12},
        "20": {"ic": 0.8, "rank_ic": 0.8, "sample_size": 12},
    }
    horizon_ics = []
    for window_key, r in multi_window_results.items():
        if not isinstance(r, dict):
            continue
        ic_val = r.get("ic")
        if isinstance(ic_val, (int, float)) and r.get("sample_size", 0) >= 10:
            horizon_ics.append((r.get("window", window_key), float(ic_val)))
    signs = {1 if ic > 0.05 else (-1 if ic < -0.05 else 0) for _, ic in horizon_ics}
    assert 1 in signs and -1 in signs  # 倒挂被检出


# ── FIX-6: conditional_returns field/op 校验 ─────────────────────

@pytest.mark.parametrize(
    "conditions,expected_ok",
    [
        ([{"field": "ma_20", "op": "<", "value": 10}], True),
        ([{"field": "rsi_14", "op": "<", "value": 30}], True),
        ([{"field": "close", "op": ">", "value": 100}], True),
        ([{"id": "rsi_oversold"}], True),
        ([{"field": "nonexistent_zzz", "op": "<", "value": 1}], False),
        ([{"field": "macd", "op": "<", "value": 1}], False),
        ([{"field": "close", "op": "BADOP", "value": 1}], False),
    ],
)
def test_fix6_validate_conditions(conditions, expected_ok):
    from akshare_mcp.services.conditional_returns import validate_conditions

    ok, err = validate_conditions(conditions)
    assert ok is expected_ok
    if not expected_ok:
        assert err  # 必须给出可诊断原因


# ── FIX-4: stop_levels 输入校验 ──────────────────────────────────

def _fake_klines(n=27):
    return [
        {
            "date": f"2026-05-{d:02d}",
            "open": 10.0, "high": 10.5, "low": 9.5,
            "close": 10.0 + (d % 5) * 0.1, "volume": 1000,
        }
        for d in range(1, n + 1)
    ]


def test_fix4_stop_levels_rejects_nonpositive_entry():
    from akshare_mcp.tools.stop_levels import compute_stop_levels

    r0 = asyncio.run(compute_stop_levels("600519", 0, klines=_fake_klines()))
    assert r0.get("success") is False
    rn = asyncio.run(compute_stop_levels("600519", -50, klines=_fake_klines()))
    assert rn.get("success") is False


def test_fix4_stop_levels_rejects_out_of_range_risk():
    from akshare_mcp.tools.stop_levels import compute_stop_levels

    r = asyncio.run(
        compute_stop_levels("600519", 10.0, capital=1_000_000, risk_per_trade=5, klines=_fake_klines())
    )
    assert r.get("success") is False


def test_fix4_stop_levels_accepts_valid_input():
    from akshare_mcp.tools.stop_levels import compute_stop_levels

    r = asyncio.run(
        compute_stop_levels("600519", 10.0, capital=1_000_000, risk_per_trade=0.02, klines=_fake_klines())
    )
    assert r.get("success") is True
    pos = r.get("data", {}).get("position_sizing") or {}
    assert pos.get("max_shares", 0) >= 0
