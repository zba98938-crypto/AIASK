"""Regression tests for strategy stage validators.

These tests pin the string-array tolerance behaviour added on 2026-05-28 to
keep the staged pipeline from collapsing to monolithic fallback when the LLM
emits string-only payloads (the most common DeepSeek failure mode observed in
the strategy_factory logs).

Failure modes covered:
- ``{"events": ["半导体", "白酒"]}`` — string list instead of object list
- ``{"themes": ["新能源", "电动车"]}`` — string list instead of object list
- ``{"exposures": ["600519", "000858"]}`` — bare A-share codes
- single-object responses with no wrapping list (``{"theme_code": ..., ...}``)
"""

from __future__ import annotations

from akshare_mcp.services.strategy_stages import validate_stage_output


# ---------------------------------------------------------------------------
# event_recognition
# ---------------------------------------------------------------------------


def test_event_recognition_accepts_string_array():
    output = {"events": ["新能源装备", "人形机器人", "宽基ETF"]}
    assert validate_stage_output("event_recognition", output) is True
    assert isinstance(output["events"], list) and output["events"]
    for item in output["events"]:
        assert isinstance(item, dict)
        assert item.get("theme_code")
        assert item.get("event_type")
    # 主题库可识别的字符串应该映射到具体的 theme_code，不是 unknown
    theme_codes = {item.get("theme_code") for item in output["events"]}
    assert "new_energy_vehicle" in theme_codes
    assert "robotics_automation" in theme_codes


def test_event_recognition_accepts_empty_list_as_valid_no_signal():
    output = {"events": []}
    assert validate_stage_output("event_recognition", output) is True


def test_event_recognition_accepts_proper_object_list():
    output = {
        "events": [
            {
                "theme_code": "chip_domestic",
                "event_type": "sector_rotation",
                "event_id": "ev_001",
                "title": "半导体板块走强",
                "severity": 3,
                "affected_sectors": ["半导体"],
                "evidence": ["半导体板块涨3.2%"],
            }
        ]
    }
    assert validate_stage_output("event_recognition", output) is True
    # 不应该被改写
    assert output["events"][0]["theme_code"] == "chip_domestic"


# ---------------------------------------------------------------------------
# theme_propagation
# ---------------------------------------------------------------------------


def test_theme_propagation_accepts_string_array():
    output = {"themes": ["新能源", "电动车", "锂电"]}
    assert validate_stage_output("theme_propagation", output) is True
    assert isinstance(output["themes"], list) and output["themes"]
    for item in output["themes"]:
        assert isinstance(item, dict)
        assert item.get("theme_code")


def test_theme_propagation_rejects_placeholder_string():
    """LLM sometimes echoes the schema literal ``["theme_code"]``."""
    output = {"themes": ["theme_code"]}
    assert validate_stage_output("theme_propagation", output) is False


def test_theme_propagation_accepts_empty_as_valid_no_signal():
    output = {"themes": []}
    assert validate_stage_output("theme_propagation", output) is True


# ---------------------------------------------------------------------------
# exposure_mapping
# ---------------------------------------------------------------------------


def test_exposure_mapping_accepts_code_array():
    """LLM most common failure: bare list of A-share codes."""
    output = {"exposures": ["600519", "000858", "601398"]}
    assert validate_stage_output("exposure_mapping", output) is True
    # 应当合并成单个 exposure 含全部代码
    assert len(output["exposures"]) == 1
    exp = output["exposures"][0]
    assert exp["theme_code"]
    assert set(exp["target_symbols"]) == {"600519", "000858", "601398"}


def test_exposure_mapping_accepts_singleton_object():
    """LLM 偶尔忘记包 ``exposures`` 字段，直接返回单条 exposure 对象。"""
    output = {
        "theme_code": "new_energy_vehicle",
        "target_symbols": ["300750", "002594", "002709"],
        "sector": "新能源汽车",
        "exposure_type": "direct_beneficiary",
        "weight": 0.8,
    }
    assert validate_stage_output("exposure_mapping", output) is True
    assert isinstance(output.get("exposures"), list)
    assert len(output["exposures"]) == 1
    assert output["exposures"][0]["theme_code"] == "new_energy_vehicle"


def test_exposure_mapping_accepts_empty_as_valid_no_signal():
    output = {"exposures": []}
    assert validate_stage_output("exposure_mapping", output) is True


def test_exposure_mapping_rejects_pure_industry_names():
    """纯行业名（无股票代码）下游不可用，应当 fail 走 fallback。"""
    output = {"exposures": ["新能源装备", "人形机器人"]}
    assert validate_stage_output("exposure_mapping", output) is False


# ---------------------------------------------------------------------------
# negative cases
# ---------------------------------------------------------------------------


def test_validator_unknown_stage_passes_through():
    """未注册的 stage_id 不强制 schema 检查。"""
    assert validate_stage_output("nonexistent_stage", {"foo": 1}) is True


def test_validator_handles_garbage_input():
    assert validate_stage_output("event_recognition", {"foo": 1}) is False
    assert validate_stage_output("theme_propagation", {"bar": "baz"}) is False
    assert validate_stage_output("exposure_mapping", {"xyz": 0}) is False
