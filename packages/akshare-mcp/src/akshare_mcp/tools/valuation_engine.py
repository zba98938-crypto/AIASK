"""估值引擎 — 纯财务数学计算

包含 WACC 计算、FCF 预测、敏感性分析、Monte Carlo 模拟、多情景 DCF 等
纯计算函数，不依赖 MCP / 数据库 / 网络 I/O。
"""

from typing import Optional, List
import statistics
import math
import random


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# WACC / CAPM
# ---------------------------------------------------------------------------

def _cost_of_equity_capm(risk_free_rate: float, beta: float, market_risk_premium: float) -> float:
    """CAPM 权益成本"""
    return risk_free_rate + beta * market_risk_premium


def _after_tax_cost_of_debt(cost_of_debt: float, tax_rate: float) -> float:
    """税后债务成本"""
    return cost_of_debt * (1 - tax_rate)


def _compute_wacc(
    *,
    equity_weight: float,
    debt_weight: float,
    cost_of_equity: float,
    cost_of_debt: float,
    tax_rate: float,
) -> dict:
    """计算 WACC，并返回拆解详情。"""
    total_weight = equity_weight + debt_weight
    if total_weight <= 0:
        raise ValueError("equity_weight + debt_weight 必须大于 0")

    ew = equity_weight / total_weight
    dw = debt_weight / total_weight
    after_tax_debt = _after_tax_cost_of_debt(cost_of_debt, tax_rate)
    wacc = ew * cost_of_equity + dw * after_tax_debt
    return {
        "cost_of_equity": float(cost_of_equity),
        "cost_of_debt_pre_tax": float(cost_of_debt),
        "cost_of_debt_after_tax": float(after_tax_debt),
        "equity_weight": float(ew),
        "debt_weight": float(dw),
        "tax_rate": float(tax_rate),
        "wacc": float(wacc),
    }


# ---------------------------------------------------------------------------
# FCF 预测 & 折现
# ---------------------------------------------------------------------------

def _build_driver_fcf_projection(
    *,
    base_revenue: float,
    growth_rate: float,
    years: int,
    profit_margin: float,
    tax_rate: float,
    capex_ratio: float,
    depreciation_ratio: float,
    nwc_ratio: float,
) -> List[dict]:
    """按驱动项构造未来 FCF 预测。"""
    projection = []
    for year in range(1, years + 1):
        revenue = base_revenue * ((1 + growth_rate) ** year)
        ebit = revenue * profit_margin
        nopat = ebit * (1 - tax_rate)
        depreciation = revenue * depreciation_ratio
        capex = revenue * capex_ratio
        delta_nwc = revenue * nwc_ratio
        fcf = nopat + depreciation - capex - delta_nwc
        projection.append(
            {
                "year": year,
                "revenue": float(revenue),
                "ebit": float(ebit),
                "nopat": float(nopat),
                "depreciation": float(depreciation),
                "capex": float(capex),
                "delta_nwc": float(delta_nwc),
                "fcf": float(fcf),
            }
        )
    return projection


def _present_value_from_projection(
    projection: List[dict],
    *,
    discount_rate: float,
    terminal_growth_rate: float,
) -> dict:
    """将 FCF 预测折现并计算终值。"""
    pv_sum = 0.0
    discounted_projection = []
    for row in projection:
        year = row["year"]
        pv = row["fcf"] / ((1 + discount_rate) ** year)
        pv_sum += pv
        item = dict(row)
        item["pv_fcf"] = float(pv)
        discounted_projection.append(item)

    last_fcf = projection[-1]["fcf"]
    terminal_fcf = last_fcf * (1 + terminal_growth_rate)
    denom = discount_rate - terminal_growth_rate
    if abs(denom) < 1e-9:
        # Guard: when discount_rate ≈ terminal_growth_rate, cap terminal value
        terminal_value = terminal_fcf * 100  # conservative cap: 100x terminal FCF
    else:
        terminal_value = terminal_fcf / denom
    pv_terminal = terminal_value / ((1 + discount_rate) ** projection[-1]["year"])

    return {
        "projection": discounted_projection,
        "pv_sum": float(pv_sum),
        "terminal_fcf": float(terminal_fcf),
        "terminal_value": float(terminal_value),
        "pv_terminal": float(pv_terminal),
        "intrinsic_value": float(pv_sum + pv_terminal),
    }


# ---------------------------------------------------------------------------
# 敏感性分析
# ---------------------------------------------------------------------------

# 供测试与审计复用的默认情景偏移
DEFAULT_SENSITIVITY_SHOCKS = [-0.01, 0.0, 0.01]


def _run_sensitivity(
    *,
    base_revenue: float,
    years: int,
    profit_margin: float,
    tax_rate: float,
    capex_ratio: float,
    depreciation_ratio: float,
    nwc_ratio: float,
    growth_rate: float,
    discount_rate: float,
    terminal_growth_rate: float,
    growth_shocks: List[float],
    discount_shocks: List[float],
    terminal_shocks: List[float],
) -> List[dict]:
    """增长率/折现率/永续增长率三维敏感性。"""
    scenarios = []
    for g_shock in growth_shocks:
        for r_shock in discount_shocks:
            for tg_shock in terminal_shocks:
                g = growth_rate + g_shock
                r = discount_rate + r_shock
                tg = terminal_growth_rate + tg_shock
                if r <= tg:
                    continue
                projection = _build_driver_fcf_projection(
                    base_revenue=base_revenue,
                    growth_rate=g,
                    years=years,
                    profit_margin=profit_margin,
                    tax_rate=tax_rate,
                    capex_ratio=capex_ratio,
                    depreciation_ratio=depreciation_ratio,
                    nwc_ratio=nwc_ratio,
                )
                val = _present_value_from_projection(
                    projection,
                    discount_rate=r,
                    terminal_growth_rate=tg,
                )
                scenarios.append(
                    {
                        "growth_rate": float(g),
                        "discount_rate": float(r),
                        "terminal_growth_rate": float(tg),
                        "intrinsic_value": float(val["intrinsic_value"]),
                    }
                )
    return scenarios


# ---------------------------------------------------------------------------
# Monte Carlo 分布
# ---------------------------------------------------------------------------

def _sanitize_distribution_samples(samples: int, *, low: int = 100, high: int = 10000) -> int:
    """Normalize distribution sample count to a safe integer range."""
    try:
        n = int(samples)
    except Exception:
        n = 1000
    return max(low, min(high, n))


def _linear_quantile(sorted_values: List[float], q: float) -> float:
    """Linear interpolation quantile on sorted values."""
    if not sorted_values:
        raise ValueError("sorted_values cannot be empty")
    q = _clamp(float(q), 0.0, 1.0)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    weight = pos - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def _run_dcf_distribution(
    *,
    base_revenue: float,
    years: int,
    tax_rate: float,
    capex_ratio: float,
    depreciation_ratio: float,
    nwc_ratio: float,
    growth_rate: float,
    profit_margin: float,
    discount_rate: float,
    terminal_growth_rate: float,
    sample_size: int,
    growth_std_ratio: float = 0.2,
    margin_std_ratio: float = 0.15,
    discount_std_ratio: float = 0.1,
    terminal_std_ratio: float = 0.1,
    seed: Optional[int] = None,
) -> dict:
    """
    Monte Carlo DCF distribution based on key assumptions.

    Each std arg is interpreted as a ratio vs. the base assumption.
    """
    rng = random.Random(seed)

    def _sample(base: float, ratio: float, low: float, high: float) -> float:
        sigma = abs(base) * max(float(ratio), 0.0)
        if sigma == 0:
            return _clamp(base, low, high)
        return _clamp(rng.gauss(base, sigma), low, high)

    values: List[float] = []
    attempts = 0
    max_attempts = max(sample_size * 4, sample_size + 10)

    while len(values) < sample_size and attempts < max_attempts:
        attempts += 1
        g = _sample(growth_rate, growth_std_ratio, -0.2, 0.5)
        pm = _sample(profit_margin, margin_std_ratio, 0.01, 0.6)
        r = _sample(discount_rate, discount_std_ratio, 0.02, 0.4)
        tg = _sample(terminal_growth_rate, terminal_std_ratio, -0.02, 0.08)
        if r <= tg:
            continue
        try:
            projection = _build_driver_fcf_projection(
                base_revenue=base_revenue,
                growth_rate=g,
                years=years,
                profit_margin=pm,
                tax_rate=tax_rate,
                capex_ratio=capex_ratio,
                depreciation_ratio=depreciation_ratio,
                nwc_ratio=nwc_ratio,
            )
            pv_result = _present_value_from_projection(
                projection,
                discount_rate=r,
                terminal_growth_rate=tg,
            )
            intrinsic = float(pv_result["intrinsic_value"])
            if math.isfinite(intrinsic):
                values.append(intrinsic)
        except Exception:
            continue

    payload = {
        "requested_samples": int(sample_size),
        "sample_size": int(len(values)),
        "attempts": int(attempts),
        "mean": None,
        "std": None,
        "p10": None,
        "p50": None,
        "p90": None,
        "min": None,
        "max": None,
        "assumption_std_ratio": {
            "growth_rate": float(growth_std_ratio),
            "profit_margin": float(margin_std_ratio),
            "discount_rate": float(discount_std_ratio),
            "terminal_growth_rate": float(terminal_std_ratio),
        },
    }
    if not values:
        payload["warning"] = "No valid Monte Carlo samples under current constraints"
        return payload

    sorted_values = sorted(values)
    p10 = _linear_quantile(sorted_values, 0.10)
    p50 = _linear_quantile(sorted_values, 0.50)
    p90 = _linear_quantile(sorted_values, 0.90)
    spread = p90 - p10
    spread_ratio = spread / abs(p50) if p50 != 0 else float('inf')
    # 宽度风险: narrow(<30%), moderate(30-60%), wide(60-100%), extreme(>100%)
    if spread_ratio < 0.3:
        spread_risk = "narrow"
    elif spread_ratio < 0.6:
        spread_risk = "moderate"
    elif spread_ratio < 1.0:
        spread_risk = "wide"
    else:
        spread_risk = "extreme"
    payload.update(
        {
            "mean": float(statistics.mean(sorted_values)),
            "std": float(statistics.pstdev(sorted_values)) if len(sorted_values) > 1 else 0.0,
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "min": float(sorted_values[0]),
            "max": float(sorted_values[-1]),
            "spread": round(float(spread), 4),
            "spread_ratio": round(float(spread_ratio), 4),
            "spread_risk": spread_risk,
        }
    )
    return payload


# ---------------------------------------------------------------------------
# P0-D: 行业参数模板 (Industry Parameter Templates)
# ---------------------------------------------------------------------------
# 每个行业模板包含 DCF 驱动项的合理默认值，用户可按需覆盖。
# 字段说明:
#   growth_rate       - 营收增长率
#   profit_margin     - 营业利润率
#   capex_ratio       - 资本开支 / 营收
#   depreciation_ratio- 折旧 / 营收
#   nwc_ratio         - 净营运资本变动 / 营收
#   beta              - CAPM Beta
#   equity_weight     - 权益占比
#   debt_weight       - 债务占比
#   cost_of_debt      - 债务成本
#   terminal_growth   - 永续增长率
# ---------------------------------------------------------------------------

INDUSTRY_TEMPLATES: dict = {
    "银行": {
        "label": "银行/金融",
        "growth_rate": 0.05,
        "profit_margin": 0.35,
        "capex_ratio": 0.02,
        "depreciation_ratio": 0.015,
        "nwc_ratio": 0.01,
        "beta": 0.8,
        "equity_weight": 0.12,
        "debt_weight": 0.88,
        "cost_of_debt": 0.025,
        "terminal_growth": 0.02,
    },
    "制造": {
        "label": "制造/工业",
        "growth_rate": 0.08,
        "profit_margin": 0.10,
        "capex_ratio": 0.08,
        "depreciation_ratio": 0.05,
        "nwc_ratio": 0.03,
        "beta": 1.1,
        "equity_weight": 0.60,
        "debt_weight": 0.40,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.025,
    },
    "科技": {
        "label": "科技/TMT",
        "growth_rate": 0.15,
        "profit_margin": 0.18,
        "capex_ratio": 0.06,
        "depreciation_ratio": 0.04,
        "nwc_ratio": 0.02,
        "beta": 1.3,
        "equity_weight": 0.80,
        "debt_weight": 0.20,
        "cost_of_debt": 0.04,
        "terminal_growth": 0.03,
    },
    "消费": {
        "label": "消费/零售",
        "growth_rate": 0.10,
        "profit_margin": 0.14,
        "capex_ratio": 0.05,
        "depreciation_ratio": 0.035,
        "nwc_ratio": 0.025,
        "beta": 0.9,
        "equity_weight": 0.70,
        "debt_weight": 0.30,
        "cost_of_debt": 0.04,
        "terminal_growth": 0.025,
    },
    "医药": {
        "label": "医药/生物",
        "growth_rate": 0.12,
        "profit_margin": 0.20,
        "capex_ratio": 0.07,
        "depreciation_ratio": 0.04,
        "nwc_ratio": 0.02,
        "beta": 1.0,
        "equity_weight": 0.75,
        "debt_weight": 0.25,
        "cost_of_debt": 0.04,
        "terminal_growth": 0.025,
    },
    # P2-4.5.4 fix: 扩展行业模板覆盖到 15+ 类(诊断报告 §4.5.4)
    # 历史问题:仅 5 类不够细,白酒/酿酒/能源/新能源/光伏/半导体/军工/地产/汽车 等被迫使用通用模板
    "白酒": {
        "label": "白酒/酿酒",
        "growth_rate": 0.08,
        "profit_margin": 0.34,        # 高毛利
        "capex_ratio": 0.04,
        "depreciation_ratio": 0.025,
        "nwc_ratio": 0.05,            # 存货占用大
        "beta": 0.85,
        "equity_weight": 0.85,
        "debt_weight": 0.15,
        "cost_of_debt": 0.04,
        "terminal_growth": 0.025,
    },
    "新能源": {
        "label": "新能源/锂电",
        "growth_rate": 0.20,
        "profit_margin": 0.12,
        "capex_ratio": 0.15,          # 重资产
        "depreciation_ratio": 0.08,
        "nwc_ratio": 0.04,
        "beta": 1.4,
        "equity_weight": 0.65,
        "debt_weight": 0.35,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.03,
    },
    "光伏": {
        "label": "光伏/太阳能",
        "growth_rate": 0.18,
        "profit_margin": 0.08,        # 高度同质化、毛利较薄
        "capex_ratio": 0.18,          # 重资产
        "depreciation_ratio": 0.10,
        "nwc_ratio": 0.04,
        "beta": 1.3,
        "equity_weight": 0.60,
        "debt_weight": 0.40,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.025,
    },
    "半导体": {
        "label": "半导体/集成电路",
        "growth_rate": 0.18,
        "profit_margin": 0.22,
        "capex_ratio": 0.20,          # 晶圆厂极重资产
        "depreciation_ratio": 0.12,
        "nwc_ratio": 0.03,
        "beta": 1.5,
        "equity_weight": 0.78,
        "debt_weight": 0.22,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.03,
    },
    "军工": {
        "label": "军工/国防",
        "growth_rate": 0.10,
        "profit_margin": 0.10,        # 政府采购利润率受限
        "capex_ratio": 0.08,
        "depreciation_ratio": 0.05,
        "nwc_ratio": 0.10,            # 应收账款长账期
        "beta": 0.95,
        "equity_weight": 0.70,
        "debt_weight": 0.30,
        "cost_of_debt": 0.04,
        "terminal_growth": 0.02,
    },
    "汽车": {
        "label": "汽车/整车",
        "growth_rate": 0.08,
        "profit_margin": 0.06,        # 整车薄利
        "capex_ratio": 0.10,
        "depreciation_ratio": 0.06,
        "nwc_ratio": 0.03,
        "beta": 1.2,
        "equity_weight": 0.55,
        "debt_weight": 0.45,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.025,
    },
    "地产": {
        "label": "房地产",
        "growth_rate": 0.03,          # 行业进入低速期
        "profit_margin": 0.08,
        "capex_ratio": 0.05,
        "depreciation_ratio": 0.02,
        "nwc_ratio": 0.20,            # 存货巨大
        "beta": 1.1,
        "equity_weight": 0.30,
        "debt_weight": 0.70,
        "cost_of_debt": 0.05,
        "terminal_growth": 0.015,
    },
    "能源": {
        "label": "能源/油气煤炭",
        "growth_rate": 0.04,
        "profit_margin": 0.18,
        "capex_ratio": 0.12,
        "depreciation_ratio": 0.08,
        "nwc_ratio": 0.03,
        "beta": 1.0,
        "equity_weight": 0.60,
        "debt_weight": 0.40,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.02,
    },
    "公用事业": {
        "label": "公用事业/电力水务",
        "growth_rate": 0.04,
        "profit_margin": 0.12,
        "capex_ratio": 0.10,
        "depreciation_ratio": 0.07,
        "nwc_ratio": 0.02,
        "beta": 0.7,
        "equity_weight": 0.40,
        "debt_weight": 0.60,
        "cost_of_debt": 0.035,
        "terminal_growth": 0.02,
    },
    "通信": {
        "label": "通信/5G",
        "growth_rate": 0.10,
        "profit_margin": 0.12,
        "capex_ratio": 0.10,
        "depreciation_ratio": 0.08,
        "nwc_ratio": 0.03,
        "beta": 1.0,
        "equity_weight": 0.60,
        "debt_weight": 0.40,
        "cost_of_debt": 0.04,
        "terminal_growth": 0.025,
    },
    "互联网": {
        "label": "互联网/平台",
        "growth_rate": 0.18,
        "profit_margin": 0.20,
        "capex_ratio": 0.05,
        "depreciation_ratio": 0.03,
        "nwc_ratio": 0.01,
        "beta": 1.2,
        "equity_weight": 0.85,
        "debt_weight": 0.15,
        "cost_of_debt": 0.04,
        "terminal_growth": 0.03,
    },
    "建筑": {
        "label": "建筑/工程",
        "growth_rate": 0.05,
        "profit_margin": 0.04,        # 极薄
        "capex_ratio": 0.03,
        "depreciation_ratio": 0.02,
        "nwc_ratio": 0.15,            # 应收账款大
        "beta": 1.0,
        "equity_weight": 0.45,
        "debt_weight": 0.55,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.02,
    },
    "化工": {
        "label": "化工/材料",
        "growth_rate": 0.07,
        "profit_margin": 0.10,
        "capex_ratio": 0.10,
        "depreciation_ratio": 0.06,
        "nwc_ratio": 0.04,
        "beta": 1.1,
        "equity_weight": 0.55,
        "debt_weight": 0.45,
        "cost_of_debt": 0.045,
        "terminal_growth": 0.025,
    },
    "钢铁": {
        "label": "钢铁/有色",
        "growth_rate": 0.03,
        "profit_margin": 0.05,        # 周期性强
        "capex_ratio": 0.10,
        "depreciation_ratio": 0.06,
        "nwc_ratio": 0.04,
        "beta": 1.2,
        "equity_weight": 0.45,
        "debt_weight": 0.55,
        "cost_of_debt": 0.05,
        "terminal_growth": 0.015,
    },
}


# ---------------------------------------------------------------------------
# 多情景概率加权 DCF
# ---------------------------------------------------------------------------

def _scenario_dcf(
    *,
    base_revenue: float,
    years: int,
    tax_rate: float,
    risk_free_rate: float,
    market_risk_premium: float,
    scenarios: List[dict],
    enable_distribution: bool = False,
    distribution_samples: int = 1000,
    distribution_growth_std: float = 0.2,
    distribution_margin_std: float = 0.15,
    distribution_discount_std: float = 0.1,
    distribution_terminal_std: float = 0.1,
    distribution_seed: Optional[int] = None,
) -> dict:
    """
    多情景概率加权 DCF。

    每个 scenario 字典需包含:
        name, probability, growth_rate, profit_margin, capex_ratio,
        depreciation_ratio, nwc_ratio, beta, equity_weight, debt_weight,
        cost_of_debt, terminal_growth

    返回各情景估值明细 + 概率加权内在价值。
    当 enable_distribution=True 时，同时返回每个情景估值区间与加权区间。
    """
    results = []
    weighted_value = 0.0
    total_prob = 0.0

    scenario_interval_rows = []
    sample_size = _sanitize_distribution_samples(distribution_samples)

    for idx, sc in enumerate(scenarios):
        prob = sc["probability"]
        total_prob += prob

        cost_of_equity = _cost_of_equity_capm(
            risk_free_rate, sc["beta"], market_risk_premium
        )
        wacc_info = _compute_wacc(
            equity_weight=sc["equity_weight"],
            debt_weight=sc["debt_weight"],
            cost_of_equity=cost_of_equity,
            cost_of_debt=sc["cost_of_debt"],
            tax_rate=tax_rate,
        )
        discount_rate = wacc_info["wacc"]
        terminal_growth = sc["terminal_growth"]

        if discount_rate <= terminal_growth:
            # 跳过不合理情景
            continue

        projection = _build_driver_fcf_projection(
            base_revenue=base_revenue,
            growth_rate=sc["growth_rate"],
            years=years,
            profit_margin=sc["profit_margin"],
            tax_rate=tax_rate,
            capex_ratio=sc["capex_ratio"],
            depreciation_ratio=sc["depreciation_ratio"],
            nwc_ratio=sc["nwc_ratio"],
        )
        pv_result = _present_value_from_projection(
            projection,
            discount_rate=discount_rate,
            terminal_growth_rate=terminal_growth,
        )

        intrinsic = pv_result["intrinsic_value"]
        weighted_value += prob * intrinsic

        scenario_row = {
            "scenario": sc["name"],
            "probability": prob,
            "growth_rate": sc["growth_rate"],
            "profit_margin": sc["profit_margin"],
            "wacc": discount_rate,
            "terminal_growth": terminal_growth,
            "intrinsic_value": float(intrinsic),
            "weighted_contribution": float(prob * intrinsic),
        }

        if enable_distribution:
            dist_seed = None if distribution_seed is None else int(distribution_seed) + idx
            interval = _run_dcf_distribution(
                base_revenue=base_revenue,
                years=years,
                tax_rate=tax_rate,
                capex_ratio=sc["capex_ratio"],
                depreciation_ratio=sc["depreciation_ratio"],
                nwc_ratio=sc["nwc_ratio"],
                growth_rate=sc["growth_rate"],
                profit_margin=sc["profit_margin"],
                discount_rate=discount_rate,
                terminal_growth_rate=terminal_growth,
                sample_size=sample_size,
                growth_std_ratio=distribution_growth_std,
                margin_std_ratio=distribution_margin_std,
                discount_std_ratio=distribution_discount_std,
                terminal_std_ratio=distribution_terminal_std,
                seed=dist_seed,
            )
            scenario_row["valuation_interval"] = interval
            scenario_interval_rows.append((prob, interval))

        results.append(scenario_row)

    # 归一化（防止概率之和不为 1）
    if total_prob > 0 and abs(total_prob - 1.0) > 1e-6:
        weighted_value /= total_prob
        for r in results:
            r["weighted_contribution"] = float(
                r["probability"] / total_prob * r["intrinsic_value"]
            )

    payload = {
        "scenarios": results,
        "weighted_intrinsic_value": float(weighted_value),
    }

    if enable_distribution and scenario_interval_rows:
        norm = total_prob if total_prob > 0 else 1.0

        def _weighted_percentile(key: str):
            vals = []
            for prob, interval in scenario_interval_rows:
                v = interval.get(key)
                if v is not None and math.isfinite(float(v)):
                    vals.append((prob, float(v)))
            if not vals:
                return None
            return float(sum((p / norm) * v for p, v in vals))

        wp10 = _weighted_percentile("p10")
        wp50 = _weighted_percentile("p50")
        wp90 = _weighted_percentile("p90")
        w_spread = (wp90 - wp10) if wp90 is not None and wp10 is not None else None
        w_spread_ratio = (w_spread / abs(wp50)) if w_spread is not None and wp50 and wp50 != 0 else None
        if w_spread_ratio is None:
            w_spread_risk = "unknown"
        elif w_spread_ratio < 0.3:
            w_spread_risk = "narrow"
        elif w_spread_ratio < 0.6:
            w_spread_risk = "moderate"
        elif w_spread_ratio < 1.0:
            w_spread_risk = "wide"
        else:
            w_spread_risk = "extreme"

        payload["weighted_valuation_interval"] = {
            "p10": wp10,
            "p50": wp50,
            "p90": wp90,
            "spread": round(float(w_spread), 4) if w_spread is not None else None,
            "spread_ratio": round(float(w_spread_ratio), 4) if w_spread_ratio is not None else None,
            "spread_risk": w_spread_risk,
            "method": "probability_weighted_by_scenario",
            "distribution_enabled": True,
            "requested_samples": int(sample_size),
        }

    return payload
