"""Regression test: ensure StrategyLLMRequestError details are surfaced
in the LLMProxyStrategyGenerator failure log line.

Before 2026-05-28 the warning only showed
``StrategyLLMRequestError('... after N attempts: ValueError')`` with no
hint of the underlying cause; root-cause hunting required digging into
strategy_factory_runs.stages.

This pins the new fields (last_error / last_error_type / status_code /
first_attempt_error) into the warning record's getMessage().

The tests drive the actual ``_run_external_provider_request`` coroutine
on a stubbed ``external_provider`` so any future refactor to the log
format is caught.
"""

from __future__ import annotations

import logging

import pytest

from akshare_mcp.services.strategy_generators import LLMProxyStrategyGenerator
from akshare_mcp.services.strategy_llm_provider import StrategyLLMRequestError


class _RaisingExternalProvider:
    """Provider double that always raises the supplied error from generate_candidates."""

    def __init__(self, error: Exception):
        self._error = error

    async def generate_candidates(self, **_kwargs):
        raise self._error


def _make_generator(error: Exception) -> LLMProxyStrategyGenerator:
    """Build a minimally-initialised generator with a stub external provider.

    We avoid the real ``__init__`` because it pulls in LLMAlphaMiner, env
    config, and other heavy deps that are unrelated to the logging path.
    """
    gen = LLMProxyStrategyGenerator.__new__(LLMProxyStrategyGenerator)
    gen.external_provider = _RaisingExternalProvider(error)
    return gen


@pytest.mark.asyncio
async def test_external_provider_failure_log_surfaces_root_cause(caplog):
    err = StrategyLLMRequestError(
        "external llm request failed after 1 attempts: ValueError",
        metrics={
            "status": "failed",
            "last_error_type": "ValueError",
            "last_error": "candidate payload missing entry/exit dsl block",
            "last_error_status_code": None,
            "attempts": [
                {
                    "attempt": 1,
                    "status": "failed",
                    "error_type": "ValueError",
                    "error": "candidate payload missing entry/exit dsl block",
                    "elapsed_seconds": 1.234,
                }
            ],
        },
    )
    gen = _make_generator(err)

    logger_name = "akshare_mcp.services._strategy_generators_external"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        result = await gen._run_external_provider_request(
            snapshot={},
            frame=None,
            frame_cache={},
            research_context={},
            parent_strategies=[],
            history_summary=[],
            research_task=None,
            request_limit=3,
            request_index=2,
        )

    assert result["status"] == "failed"
    assert result["request_report"]["error_type"] == "ValueError"

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name
    ]
    assert messages, "expected at least one warning record from the external provider failure"
    combined = " ".join(messages)
    assert "candidate payload missing entry/exit dsl block" in combined, (
        "log line should contain the underlying ValueError text; got: " + combined
    )
    assert "last_error_type=ValueError" in combined
    assert "first_attempt_error_type=ValueError" in combined
    assert "request_index=2" in combined
    assert "limit=3" in combined


@pytest.mark.asyncio
async def test_external_provider_failure_log_handles_missing_metrics(caplog):
    """空 metrics 时仍输出可解析的日志（不抛异常）。"""
    err = StrategyLLMRequestError(
        "external llm request failed after 1 attempts: ValueError"
    )
    gen = _make_generator(err)

    logger_name = "akshare_mcp.services._strategy_generators_external"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        result = await gen._run_external_provider_request(
            snapshot={},
            frame=None,
            frame_cache={},
            research_context={},
            parent_strategies=[],
            history_summary=[],
            research_task=None,
            request_limit=3,
            request_index=1,
        )

    assert result["status"] == "failed"
    combined = " ".join(
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name
    )
    # 没有 metrics 时降级到 exception class name，至少确认有可读 fallback
    assert "last_error_type=StrategyLLMRequestError" in combined
    assert "first_attempt_error_type=StrategyLLMRequestError" in combined
    assert "last_error='StrategyLLMRequestError'" in combined


@pytest.mark.asyncio
async def test_external_provider_failure_log_truncates_long_error(caplog):
    """超长错误文本被截断到 300 字符以内，避免日志被一条记录撑爆。"""
    long_text = "x" * 5000
    err = StrategyLLMRequestError(
        "external llm request failed after 1 attempts: ValueError",
        metrics={
            "status": "failed",
            "last_error_type": "ValueError",
            "last_error": long_text,
            "attempts": [
                {
                    "attempt": 1,
                    "status": "failed",
                    "error_type": "ValueError",
                    "error": long_text,
                    "elapsed_seconds": 0.5,
                }
            ],
        },
    )
    gen = _make_generator(err)

    logger_name = "akshare_mcp.services._strategy_generators_external"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        await gen._run_external_provider_request(
            snapshot={},
            frame=None,
            frame_cache={},
            research_context={},
            parent_strategies=[],
            history_summary=[],
            research_task=None,
            request_limit=3,
            request_index=1,
        )

    combined = " ".join(
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name
    )
    # 一条 warning 不应该包含完整的 5000 字符串
    assert long_text not in combined, "log line should be truncated, full 5k string leaked"
    # 但截断后的前缀应该出现
    assert ("x" * 100) in combined, "first 100 chars of truncated error should still appear"
