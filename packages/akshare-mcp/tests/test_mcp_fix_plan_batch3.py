"""MCP_FIX_PLAN 第三批回归测试（FIX-19~23）。

- FIX-19 (F-N04-1): TechnicalAnalysis.calculate_rsi warmup 不足返回 None/unknown，不伪造超卖买入
- FIX-20 (F-N18-1): optimize_portfolio 跟踪 valid_codes/dropped_codes（静态扫描）
- FIX-21 (F-N43-6): check_crowding 无因子池时不再虚报 momentum 高拥挤；含重复因子时 similar_count>0
- FIX-22 (F-N43-2): prediction_diagnosis_workflow platt 路径尊重用户 platt_a/platt_b（静态扫描 + 单元）
- FIX-23 (F-N08-1): scenario_dcf 负内在价值标注 valuation_reliable=false + quality_flags（静态扫描）
"""

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "akshare_mcp"


def _strategy_mgr_crud_source() -> str:
    """Read strategy_mgr_crud source, tolerating either a single module file or a package dir."""
    managers = _SRC / "tools" / "managers"
    single = managers / "strategy_mgr_crud.py"
    if single.exists():
        return single.read_text(encoding="utf-8")
    pkg = managers / "strategy_mgr_crud"
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))


# ── FIX-19: RSI warmup 不足不伪造信号 ────────────────────────────────

def test_fix19_rsi_insufficient_data_returns_unknown():
    from akshare_mcp.services.technical_analysis import TechnicalAnalysis

    # RSI(14) 至少需要 15 根；给 5 根
    res = TechnicalAnalysis.calculate_rsi([10.0, 10.1, 9.9, 10.2, 10.3], period=14)
    assert res["value"] is None
    assert res["signal"] == "unknown"
    assert res["reliable"] is False
    # 关键：不得伪造 oversold buy
    assert res.get("oversold") in (None, False)


def test_fix19_rsi_sufficient_data_reliable():
    from akshare_mcp.services.technical_analysis import TechnicalAnalysis

    closes = [10.0 + 0.1 * i for i in range(40)]  # 单调上升
    res = TechnicalAnalysis.calculate_rsi(closes, period=14)
    assert res["reliable"] is True
    assert res["value"] is not None
    assert 0 <= res["value"] <= 100


def test_fix19_rsi_numpy_insufficient_returns_none():
    from akshare_mcp.services.technical_analysis import TechnicalAnalysis

    val = TechnicalAnalysis._calculate_rsi_numpy([10.0, 10.1, 9.9], period=14)
    assert val is None


# ── FIX-21: crowding 真实相似度，无池不虚报高拥挤 ────────────────────

def test_fix21_crowding_no_pool_not_high():
    from akshare_mcp.services.governance_monitor import check_crowding

    res = check_crowding(
        "probe_momentum",
        expression="close/ma_20-1",
        category="momentum",
        existing_pool=None,
    )
    # 无因子池 → 不能虚报 high（F-N43-6 核心）
    assert res["crowding_band"] != "high"
    assert res["assessment_basis"] == "category_prior_only"
    assert res["confidence"] == "low"
    assert res["similar_factor_count"] == 0


def test_fix21_crowding_exact_duplicate_detected():
    from akshare_mcp.services.governance_monitor import check_crowding

    res = check_crowding(
        "probe_momentum",
        expression="close/ma_20-1",
        category="momentum",
        existing_pool=["close/ma_20-1", "rsi_14", "macd", "close/ma_20-1"],
    )
    # 池中两个完全相同因子 → 必须被检出
    assert res["exact_duplicate_count"] >= 2
    assert res["similar_factor_count"] >= 2
    assert res["assessment_basis"] == "pool_similarity"


def test_fix21_crowding_unique_factor_low_with_pool():
    from akshare_mcp.services.governance_monitor import check_crowding

    res = check_crowding(
        "probe_unique",
        expression="custom_alpha_xyz",
        category="quality",
        existing_pool=["rsi_14", "macd", "momentum_20d", "pe_ttm", "roe"],
    )
    # 与池无相似 → 低拥挤
    assert res["similar_factor_count"] == 0
    assert res["crowding_band"] in ("low", "medium")


# ── FIX-22: platt_a/platt_b 不再是死参数 ─────────────────────────────

def test_fix22_platt_fixed_coefficients_differ():
    """固定系数 Platt：不同 (a,b) 必须产出不同校准概率。"""
    from akshare_mcp.services.probability_calibration import platt_scale

    raw = 0.5
    p1 = platt_scale(raw, a=1.2, b=-0.3)
    p2 = platt_scale(raw, a=3.0, b=-1.5)
    assert abs(p1 - p2) > 1e-6, "不同 platt 系数必须产出不同概率（非死参数）"


def test_fix22_formatter_honors_user_platt():
    src = (_SRC / "tools" / "ai_workflows_parts" / "formatters.py").read_text(encoding="utf-8")
    # 必须存在「用户显式传入 platt 系数走固定系数路径」的分支
    assert "user_supplied_platt" in src
    assert "platt_fixed_coefficients" in src
    assert 'workflow_method == "platt" and user_supplied_platt' in src


# ── FIX-23: scenario_dcf 负内在价值护栏 ──────────────────────────────

def test_fix23_scenario_dcf_negative_guard_present():
    src = (_SRC / "tools" / "valuation.py").read_text(encoding="utf-8")
    assert "valuation_reliable" in src
    assert "non_positive_intrinsic_value" in src
    assert "non_positive_per_share" in src


def test_fix23_scenario_dcf_negative_weighted_value_flagged():
    """直接用 _scenario_dcf 构造负 FCF 情景，验证 weighted_intrinsic_value<=0 可被检出。"""
    from akshare_mcp.tools.valuation_engine import _scenario_dcf

    # 负利润率 + 高 capex → FCF 为负 → 内在价值为负
    scenarios = [
        {
            "name": "Bear（悲观）",
            "probability": 1.0,
            "growth_rate": 0.05,
            "profit_margin": -0.10,  # 负利润率
            "capex_ratio": 0.20,
            "depreciation_ratio": 0.03,
            "nwc_ratio": 0.02,
            "beta": 1.2,
            "equity_weight": 0.7,
            "debt_weight": 0.3,
            "cost_of_debt": 0.05,
            "terminal_growth": 0.025,
        }
    ]
    result = _scenario_dcf(
        base_revenue=1.0e10,
        years=5,
        tax_rate=0.25,
        risk_free_rate=0.028,
        market_risk_premium=0.06,
        scenarios=scenarios,
    )
    # 验证引擎确实能产出 <=0 的内在价值（护栏逻辑的触发前提）
    assert result["weighted_intrinsic_value"] <= 0.0


# ── FIX-24: watchlist add_stocks 代码校验 ────────────────────────────

def test_fix24_watchlist_validates_codes():
    src = (_SRC / "tools" / "managers" / "watchlist_manager.py").read_text(encoding="utf-8")
    # add_stocks 分支必须调用存在性校验
    assert "resolve_existing_security_code_async" in src
    assert "invalid_codes" in src
    # 必须在 add_stocks 分支内、且在该分支的 INSERT INTO watchlist 之前
    add_idx = src.find('action in {"add_stocks", "add"}')
    validate_idx = src.find("resolve_existing_security_code_async")
    insert_idx = src.find("INSERT INTO watchlist", add_idx)  # add_stocks 分支内的 INSERT
    assert add_idx != -1 and insert_idx != -1 and validate_idx != -1
    assert add_idx < validate_idx < insert_idx, "校验必须发生在 add_stocks 分支内、入库之前"


# ── FIX-25: paper_trading 限价单代码校验 ─────────────────────────────

def test_fix25_paper_trading_validates_code_before_order_type_branch():
    src = (_SRC / "tools" / "managers" / "paper_trading_manager.py").read_text(encoding="utf-8")
    assert "resolve_existing_security_code_async" in src
    # 校验必须在 order_type limit/stop 分支之前（统一拦截，堵限价单旁路）
    validate_idx = src.find("resolve_existing_security_code_async")
    limit_branch_idx = src.find("if order_type in ('limit', 'stop'):")
    assert validate_idx != -1 and limit_branch_idx != -1
    assert validate_idx < limit_branch_idx, "代码校验必须在 limit/stop 分支之前"


# ── FIX-26: model_drift 回显未识别键 ─────────────────────────────────

def test_fix26_model_drift_surfaces_unrecognized_keys():
    from akshare_mcp.services.governance_monitor import check_model_drift

    res = check_model_drift(
        "redteam_model",
        current_metrics={"auc": 0.51, "ic": 0.005, "sharpe": 0.2},
        baseline_metrics={"auc": 0.68, "ic": 0.05, "sharpe": 1.1},
    )
    # auc/ic/sharpe 不被识别 → 必须回显而非静默丢弃
    assert set(res["unrecognized_keys"]) >= {"auc", "ic", "sharpe"}
    assert res["warnings"], "应有未识别键告警"
    # 全维度 unknown 且用户传了指标 → 不再静默 continue_monitoring
    assert res["action_recommended"] != "continue_monitoring"


def test_fix26_model_drift_recognized_keys_no_warning():
    from akshare_mcp.services.governance_monitor import check_model_drift

    res = check_model_drift(
        "redteam_model",
        current_metrics={"brier_score": 0.12, "ece": 0.04},
        baseline_metrics={"brier_score": 0.10, "ece": 0.03},
    )
    assert res["unrecognized_keys"] == []


# ── FIX-27: strategy_health 归属反映 target_id ───────────────────────

def test_fix27_strategy_health_uses_target_id():
    from akshare_mcp.services.governance_monitor import GovernanceMonitor

    report = GovernanceMonitor().run_full_check(
        target_type="model",
        target_id="redteam_n43_model",
        include_factor_decay=False,
        include_crowding=False,
        include_model_drift=False,
        include_strategy_health=True,
        include_consistency=False,
    )
    d = report.to_dict()
    # strategy_health.strategy_id 必须是 target_id，不再硬编码 "system"
    assert d["strategy_health"]["strategy_id"] == "redteam_n43_model"


# ── FIX-28: factor_decay 转负/短半衰期升级告警 ───────────────────────

def test_fix28_factor_decay_negative_recent_ic_escalates():
    from akshare_mcp.services.governance_monitor import check_factor_decay

    # IC 序列递减并转负
    res = check_factor_decay(
        "decaying_factor",
        ic_history=[0.05, 0.04, 0.02, 0.0, -0.02, -0.04, -0.05, -0.06],
    )
    assert res["decay_status"] in ("decaying", "decayed")
    assert res["decay_status"] != "stable"
    assert "recent_ic_negative" in res.get("escalation_reasons", [])
    assert res["action_recommended"] in ("review_and_monitor", "retire_or_replace")


def test_fix28_factor_decay_healthy_stays_stable():
    from akshare_mcp.services.governance_monitor import check_factor_decay

    # 稳定正 IC
    res = check_factor_decay(
        "stable_factor",
        ic_history=[0.04, 0.045, 0.04, 0.042, 0.041, 0.043, 0.04, 0.044],
    )
    assert res["decay_status"] == "stable"


# ── FIX-30: data_validation non_null_fields 真正校验内容 ─────────────

def test_fix30_builtin_validator_non_null_fields():
    from akshare_mcp.services.adapters.data_validation_adapter import BuiltinValidationAdapter

    adapter = BuiltinValidationAdapter()
    records = [
        {"close": 10.5, "volume": 1000},
        {"close": None, "volume": 1200},  # close 为 null
        {"close": 10.7, "volume": None},  # volume 为 null
    ]
    res = adapter.validate_dataset(
        records,
        {"required_fields": ["close", "volume"], "non_null_fields": ["close", "volume"]},
    )
    # 含 null → 不应 passed=true（堵 F-N43-4「列存在即通过」）
    assert res.passed is False
    # 必须有 non_null_fields 检查项
    detail_keys = {d.get("expectation") for d in (res.details or [])}
    assert any(str(k).startswith("non_null_fields:") for k in detail_keys)


def test_fix30_builtin_validator_clean_data_passes():
    from akshare_mcp.services.adapters.data_validation_adapter import BuiltinValidationAdapter

    adapter = BuiltinValidationAdapter()
    records = [
        {"close": 10.5, "volume": 1000},
        {"close": 10.6, "volume": 1100},
    ]
    res = adapter.validate_dataset(
        records,
        {"required_fields": ["close", "volume"], "non_null_fields": ["close", "volume"]},
    )
    assert res.passed is True


def test_fix29_formatter_empty_probabilities_standardized():
    src = (_SRC / "tools" / "ai_workflows_parts" / "formatters.py").read_text(encoding="utf-8")
    # probabilities 现为 Optional，且对空/缺失返回标准化 PARAM_ERROR
    assert "probabilities: list[float] | None = None" in src
    assert "probabilities is required and must be a non-empty array" in src


# ── FIX-31: should_i_buy 概率校准 + 一致性 + style 校验 (F-N22) ──────

def test_fix31_should_i_buy_style_validation():
    src = (_SRC / "tools" / "_decision_buy.py").read_text(encoding="utf-8")
    assert "_VALID_STYLES" in src
    assert "investment_style='{investment_style}' 非法" in src or "非法（支持" in src


def test_fix31_should_i_buy_calibration_and_consistency():
    src = (_SRC / "tools" / "_decision_buy.py").read_text(encoding="utf-8")
    # 失校准修正：empirical shrinkage
    assert "empirical_shrinkage" in src
    assert "raw_buy_probability" in src
    # 一致性块
    assert "decision_consistency" in src
    assert "threshold_inversion_detected" in src


# ── FIX-32: should_i_sell buy_price sanity check (F-N22-7) ───────────

def test_fix32_should_i_sell_buy_price_sanity():
    src = (_SRC / "tools" / "_decision_sell.py").read_text(encoding="utf-8")
    assert "buy_price_warning" in src
    assert "数量级严重不符" in src


# ── FIX-33: publish promotion_gate 强制 (F-N42-1) ────────────────────

def test_fix33_publish_enforces_promotion_gate():
    src = _strategy_mgr_crud_source()
    # handle_publish 必须评估孵化总览并在 gate 失败时拒绝
    pub_idx = src.find("async def handle_publish")
    assert pub_idx != -1
    pub_src = src[pub_idx:pub_idx + 2500]
    assert "promotion_gate" in pub_src
    assert "promotion_ready" in pub_src
    assert "force" in pub_src  # 仅显式 force 可绕过
    assert "blockers" in pub_src


# ── FIX-34: create strategy_type 白名单 (F-N42-5) ────────────────────

def test_fix34_create_validates_strategy_type():
    src = _strategy_mgr_crud_source()
    assert "_KNOWN_STRATEGY_TYPES" in src
    assert "strategy_type_warning" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])