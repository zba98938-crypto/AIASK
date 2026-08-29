"""Runtime toggle env-var parsing tests (DEV-V1 TG)."""

import pytest

from strategy_factory.application import _runtime_toggles as toggles


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts from a clean env."""
    for key in (
        "STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED",
        "STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED",
        "STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED",
        "STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES",
        "INCUBATION_FACTORY_PAPER_INTAKE_ENABLED",
        "INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT",
        "INCUBATION_FACTORY_RECOMPILE_REMEDIATION_ENABLED",
        "INCUBATION_FACTORY_RECOMPILE_REMEDIATION_BATCH_LIMIT",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_ENABLED",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_BATCH_LIMIT",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_TTL_DAYS",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_STATUS",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_MIN_WIN_RATE",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_MIN_TRADE_COUNT",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_HEALTH_GUARD_ENABLED",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_HEALTH_MAX_AGE_HOURS",
        "STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_DEDUPE_ENABLED",
        "STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED",
        "STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED",
        "STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED",
        "STRATEGY_FACTORY_MIN_VALIDATION_GRADE",
        "STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED",
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED",
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT",
        "INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE",
        "STRATEGY_FACTORY_DIRECTION_GATE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_observe_d_grade_enabled_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED", value)
    assert toggles.observe_d_grade_enabled() is expected


def test_observe_d_grade_default_false_when_unset():
    """安全默认 — 未设置时不解封 D 级,保持现有行为。"""
    assert toggles.observe_d_grade_enabled() is False


def test_observe_first_alias_enables_wide_intake(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_OBSERVE_FIRST_ENABLED", "1")
    assert toggles.observe_first_enabled() is True
    assert toggles.wide_intake_observe_enabled() is True


def test_wide_intake_legacy_toggle_still_supported(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_WIDE_INTAKE_OBSERVE_ENABLED", "1")
    assert toggles.observe_first_enabled() is False
    assert toggles.wide_intake_observe_enabled() is True


def test_trade_aware_extra_families_empty():
    assert toggles.trade_aware_extra_families() == frozenset()


def test_trade_aware_extra_families_single(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", "volatility_breakout")
    assert toggles.trade_aware_extra_families() == frozenset({"volatility_breakout"})


def test_trade_aware_extra_families_multiple_csv(monkeypatch):
    monkeypatch.setenv(
        "STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES",
        "volatility_breakout,value_factor,sector_rotation",
    )
    assert toggles.trade_aware_extra_families() == frozenset(
        {"volatility_breakout", "value_factor", "sector_rotation"}
    )


def test_trade_aware_extra_families_strips_whitespace(monkeypatch):
    monkeypatch.setenv(
        "STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES",
        " volatility_breakout , value_factor ,, ",
    )
    assert toggles.trade_aware_extra_families() == frozenset(
        {"volatility_breakout", "value_factor"}
    )


def test_trade_aware_extra_families_lowercases(monkeypatch):
    """env 值大小写不敏感,统一转 lower 比对。"""
    monkeypatch.setenv("STRATEGY_FACTORY_TRADE_AWARE_EXTRA_FAMILIES", "Volatility_Breakout,VALUE_FACTOR")
    assert toggles.trade_aware_extra_families() == frozenset(
        {"volatility_breakout", "value_factor"}
    )


def test_paper_intake_enabled_default_true():
    assert toggles.paper_intake_enabled() is True


def test_paper_intake_enabled_via_env(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "1")
    assert toggles.paper_intake_enabled() is True


def test_paper_intake_can_be_disabled_via_env(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_ENABLED", "0")
    assert toggles.paper_intake_enabled() is False


def test_paper_intake_batch_limit_default():
    assert toggles.paper_intake_batch_limit() == 500


@pytest.mark.parametrize(
    "value,expected",
    [
        ("10", 10),
        ("100", 100),
        ("0", 1),  # 下限 1
        ("-5", 1),
        ("999", 999),
        ("5000", 3000),  # 上限 3000
        ("garbage", 500),  # 解析失败回退默认值
        ("", 500),
    ],
)
def test_paper_intake_batch_limit_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("INCUBATION_FACTORY_PAPER_INTAKE_BATCH_LIMIT", value)
    assert toggles.paper_intake_batch_limit() == expected


def test_recompile_remediation_enabled_default_true():
    assert toggles.recompile_remediation_enabled() is True


def test_recompile_remediation_can_be_disabled_via_env(monkeypatch):
    monkeypatch.setenv("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_ENABLED", "0")
    assert toggles.recompile_remediation_enabled() is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("10", 10),
        ("200", 200),
        ("0", 1),
        ("-5", 1),
        ("9999", 1000),
        ("garbage", 200),
        ("", 200),
    ],
)
def test_recompile_remediation_batch_limit_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("INCUBATION_FACTORY_RECOMPILE_REMEDIATION_BATCH_LIMIT", value)
    assert toggles.recompile_remediation_batch_limit() == expected


def test_runtime_change_takes_effect(monkeypatch):
    """toggle 改变后立即生效,不依赖 module-level 缓存。"""
    assert toggles.observe_d_grade_enabled() is False
    monkeypatch.setenv("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED", "1")
    assert toggles.observe_d_grade_enabled() is True
    monkeypatch.delenv("STRATEGY_FACTORY_OBSERVE_D_GRADE_ENABLED")
    assert toggles.observe_d_grade_enabled() is False


def test_diagnostic_observation_enabled_default_false():
    assert toggles.diagnostic_observation_enabled() is False


def test_diagnostic_observation_enabled_via_env(monkeypatch):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_ENABLED", "1")
    assert toggles.diagnostic_observation_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5", 5),
        ("0", 1),
        ("-10", 1),
        ("999", 50),
        ("bad", 5),
        ("", 5),
    ],
)
def test_diagnostic_observation_batch_limit_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_BATCH_LIMIT", value)
    assert toggles.diagnostic_observation_batch_limit() == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("7", 7),
        ("0", 1),
        ("99", 30),
        ("bad", 7),
    ],
)
def test_diagnostic_observation_ttl_days_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_TTL_DAYS", value)
    assert toggles.diagnostic_observation_ttl_days() == expected


def test_diagnostic_observation_final_status_default_is_diagnostic():
    assert toggles.diagnostic_observation_final_status() == "diagnostic"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("submitted", "submitted"),
        ("diagnostic", "diagnostic"),
        ("bad", "diagnostic"),
        ("", "diagnostic"),
    ],
)
def test_diagnostic_observation_final_status_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_STATUS", value)
    assert toggles.diagnostic_observation_final_status() == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.36", 0.36),
        ("0.20", 0.20),
        ("0.90", 0.399),
        ("bad", 0.36),
    ],
)
def test_diagnostic_observation_min_win_rate_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_MIN_WIN_RATE", value)
    assert toggles.diagnostic_observation_min_win_rate() == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("4", 4),
        ("0", 1),
        ("200", 100),
        ("bad", 4),
    ],
)
def test_diagnostic_observation_min_trade_count_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_MIN_TRADE_COUNT", value)
    assert toggles.diagnostic_observation_min_trade_count() == expected


def test_diagnostic_observation_guard_defaults_enabled():
    assert toggles.diagnostic_observation_health_guard_enabled() is True
    assert toggles.diagnostic_observation_dedupe_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("24", 24),
        ("0", 1),
        ("999", 168),
        ("bad", 24),
    ],
)
def test_diagnostic_observation_health_max_age_hours_bounds(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_DIAGNOSTIC_OBSERVATION_HEALTH_MAX_AGE_HOURS", value)
    assert toggles.diagnostic_observation_health_max_age_hours() == expected


def test_trade_prediction_p4_toggles_default_disabled():
    assert toggles.strategy_trade_prediction_promotion_gate_enabled() is False
    assert toggles.strategy_trade_prediction_budget_feedback_enabled() is False
    assert toggles.strategy_trade_prediction_factor_decay_enabled() is False


def test_trade_prediction_p4_toggles_enable_via_env(monkeypatch):
    monkeypatch.setenv("STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED", "true")
    monkeypatch.setenv("STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED", "on")
    assert toggles.strategy_trade_prediction_promotion_gate_enabled() is True
    assert toggles.strategy_trade_prediction_budget_feedback_enabled() is True
    assert toggles.strategy_trade_prediction_factor_decay_enabled() is True


def test_min_validation_grade_defaults_to_c(monkeypatch):
    assert toggles.strategy_factory_min_validation_grade() == "C"
    assert toggles.validation_grade_at_least("B", "C") is True
    assert toggles.validation_grade_at_least("D", "C") is False
    monkeypatch.setenv("STRATEGY_FACTORY_MIN_VALIDATION_GRADE", "A")
    assert toggles.strategy_factory_min_validation_grade() == "A"


def test_gate3_record_only_defaults_disabled(monkeypatch):
    assert toggles.strategy_factory_gate3_record_only_enabled() is False
    monkeypatch.setenv("STRATEGY_FACTORY_GATE3_RECORD_ONLY_ENABLED", "1")
    assert toggles.strategy_factory_gate3_record_only_enabled() is True


def test_gate3_record_only_intake_defaults_disabled(monkeypatch):
    assert toggles.gate3_record_only_intake_enabled() is False
    assert toggles.gate3_record_only_intake_batch_limit() == 100
    assert toggles.gate3_record_only_intake_min_grade() == "C"

    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_INTAKE_ENABLED", "1")
    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_BATCH_LIMIT", "999")
    monkeypatch.setenv("INCUBATION_FACTORY_GATE3_RECORD_ONLY_MIN_GRADE", "SS")

    assert toggles.gate3_record_only_intake_enabled() is True
    assert toggles.gate3_record_only_intake_batch_limit() == 500
    assert toggles.gate3_record_only_intake_min_grade() == "SS"


def test_stock_direction_gate_defaults_enabled():
    assert toggles.stock_direction_gate_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_stock_direction_gate_toggle(monkeypatch, value, expected):
    monkeypatch.setenv("STRATEGY_FACTORY_DIRECTION_GATE_ENABLED", value)
    assert toggles.stock_direction_gate_enabled() is expected
