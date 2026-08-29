from __future__ import annotations

from importlib import import_module


def test_slippage_wrapper_uses_quant_core() -> None:
    core_slippage = import_module("aiask_quant_core.slippage")
    from akshare_mcp.services import slippage as mcp_slippage

    assert mcp_slippage.SlippageCalculator is core_slippage.SlippageCalculator
    assert mcp_slippage.SlippageModelType is core_slippage.SlippageModelType
    assert mcp_slippage.slippage_calculator is core_slippage.slippage_calculator


def test_risk_model_wrapper_uses_quant_core() -> None:
    core_risk = import_module("aiask_quant_core.risk_model")
    from akshare_mcp.services import risk_model as mcp_risk

    assert mcp_risk.RiskModel is core_risk.RiskModel


def test_validation_wrapper_uses_quant_core() -> None:
    core_validation = import_module("aiask_quant_core.validation")
    from akshare_mcp.services import validation as mcp_validation

    assert mcp_validation.WalkForwardValidator is core_validation.WalkForwardValidator
    assert mcp_validation.FactorValidationPipeline is core_validation.FactorValidationPipeline


def test_strategy_dsl_wrapper_uses_quant_core() -> None:
    core_dsl = import_module("aiask_quant_core.strategy_dsl")
    from akshare_mcp.services import strategy_dsl as mcp_dsl

    assert mcp_dsl.compile_strategy_blueprint is core_dsl.compile_strategy_blueprint


def test_data_pipeline_wrapper_uses_quant_core() -> None:
    core_pipeline = import_module("aiask_quant_core.data_pipeline")
    from akshare_mcp.services import data_pipeline as mcp_pipeline

    assert mcp_pipeline.normalize_klines is core_pipeline.normalize_klines
    assert mcp_pipeline.compute_signal_hit_rate is core_pipeline.compute_signal_hit_rate
    assert mcp_pipeline.build_cross_section_summary is core_pipeline.build_cross_section_summary


def test_backtest_package_wrapper_uses_quant_core() -> None:
    core_backtest = import_module("aiask_quant_core.backtest")
    mcp_backtest = import_module("akshare_mcp.services.backtest")

    assert mcp_backtest.BacktestEngine is core_backtest.BacktestEngine
    assert mcp_backtest.StrategyRegistry is core_backtest.StrategyRegistry
    assert mcp_backtest.DslRuleStrategy is core_backtest.DslRuleStrategy


def test_backtest_submodule_wrapper_uses_quant_core() -> None:
    core_engine = import_module("aiask_quant_core.backtest.engine")
    mcp_engine = import_module("akshare_mcp.services.backtest.engine")
    core_utils = import_module("aiask_quant_core.backtest.utils")
    mcp_utils = import_module("akshare_mcp.services.backtest.utils")

    assert mcp_engine.BacktestEngine is core_engine.BacktestEngine
    assert mcp_engine.backtest_engine is core_engine.backtest_engine
    assert mcp_utils._compute_slippage_rate is core_utils._compute_slippage_rate


def test_factor_calculator_package_wrapper_uses_quant_core() -> None:
    core_factor = import_module("aiask_quant_core.factor_calculator")
    mcp_factor = import_module("akshare_mcp.services.factor_calculator")

    assert mcp_factor.FactorCalculator is core_factor.FactorCalculator
    assert mcp_factor.factor_calculator is core_factor.factor_calculator


def test_factor_calculator_submodule_wrapper_uses_quant_core() -> None:
    core_technical = import_module("aiask_quant_core.factor_calculator.technical")
    mcp_technical = import_module("akshare_mcp.services.factor_calculator.technical")
    core_analysis = import_module("aiask_quant_core.factor_calculator.analysis")
    mcp_analysis = import_module("akshare_mcp.services.factor_calculator.analysis")

    assert mcp_technical.TechnicalFactorsMixin is core_technical.TechnicalFactorsMixin
    assert mcp_analysis._check_monotonicity is core_analysis._check_monotonicity
