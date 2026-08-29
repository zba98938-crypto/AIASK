"""FactorGenerationEnvelope.analysis 字段宽容化回归测试。

根因:LLM 常把 analysis 作为整段说明文字(字符串)返回,旧定义强制 dict 导致整批
合法候选因 1 个校验错误被全部丢弃(实测 LLM 正常返回 6 个因子却报 FactorLLMRequestError,
引擎退化到本地 5 引擎,llm_primary 实际产出长期为 0)。
"""

from __future__ import annotations

import pytest

from akshare_mcp.services.factor_llm_provider import FactorGenerationEnvelope


def _valid_candidate() -> dict:
    return {
        "name": "test_factor",
        "hypothesis": "a valid hypothesis text",
        "expression_dsl": "close > open",
        "inputs": ["close", "open"],
    }


def test_analysis_accepts_string_wraps_as_summary():
    env = FactorGenerationEnvelope(candidates=[_valid_candidate()], analysis="Generated six factors")
    assert env.analysis == {"summary": "Generated six factors"}
    assert len(env.candidates) == 1


def test_analysis_accepts_dict_unchanged():
    env = FactorGenerationEnvelope(candidates=[_valid_candidate()], analysis={"k": "v"})
    assert env.analysis == {"k": "v"}


def test_analysis_none_becomes_empty_dict():
    env = FactorGenerationEnvelope(candidates=[_valid_candidate()], analysis=None)
    assert env.analysis == {}


def test_analysis_omitted_defaults_empty_dict():
    env = FactorGenerationEnvelope(candidates=[_valid_candidate()])
    assert env.analysis == {}


def test_analysis_list_wraps_as_items():
    env = FactorGenerationEnvelope(candidates=[_valid_candidate()], analysis=["x", "y"])
    assert env.analysis == {"items": ["x", "y"]}


def test_analysis_empty_string_becomes_empty_dict():
    env = FactorGenerationEnvelope(candidates=[_valid_candidate()], analysis="   ")
    assert env.analysis == {}
