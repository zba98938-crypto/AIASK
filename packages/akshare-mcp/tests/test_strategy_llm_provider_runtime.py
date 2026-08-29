from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx

from akshare_mcp.services.strategy_llm_provider import (
    StrategyLLMConfig,
    StrategyLLMProvider,
    StrategyLLMRequestError,
)


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"completion_tokens": 12},
        },
    )


def _sse_response(content: str) -> httpx.Response:
    body = (
        "event: response.output_text.delta\n"
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': content}, ensure_ascii=False)}\n\n"
        "data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=body,
    )


def _provider_with_responses(responses: list[httpx.Response], *, stage_retry_count: int = 1) -> StrategyLLMProvider:
    queue = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer test-key"
        assert queue
        return queue.pop(0)

    provider = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://llm.example.test/v1",
            api_key="test-key",
            model="test-model",
            stage_retry_count=stage_retry_count,
            stage_retry_backoff_sec=0.0,
            retry_count=stage_retry_count,
            retry_backoff_sec=0.0,
            compatibility_cooldown_sec=60.0,
            max_concurrency=1,
        )
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _provider_with_handler(handler, *, stage_retry_count: int = 1) -> StrategyLLMProvider:
    provider = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://llm.example.test/v1",
            api_key="test-key",
            model="test-model",
            stage_retry_count=stage_retry_count,
            stage_retry_backoff_sec=0.0,
            retry_count=stage_retry_count,
            retry_backoff_sec=0.0,
            compatibility_cooldown_sec=60.0,
            max_concurrency=1,
        )
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _pin_mock_client_to_current_loop(provider: StrategyLLMProvider) -> None:
    provider._runtime_loop_id = id(asyncio.get_running_loop())


def test_strategy_llm_endpoint_adds_v1_for_bare_openai_compatible_host() -> None:
    provider = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://icoe.pp.ua",
            api_key="test-key",
            model="test-model",
        )
    )

    async def _run() -> str:
        try:
            return provider._endpoint()
        finally:
            await provider.close()

    assert asyncio.run(_run()) == "https://icoe.pp.ua/v1/chat/completions"


def test_strategy_llm_endpoint_preserves_explicit_api_paths() -> None:
    explicit = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://llm.example.test/custom/chat/completions",
            api_key="test-key",
            model="test-model",
        )
    )
    v1_base = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://llm.example.test/v1",
            api_key="test-key",
            model="test-model",
        )
    )
    responses = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            provider="openai_responses",
            base_url="https://llm.example.test",
            api_key="test-key",
            model="test-model",
        )
    )

    async def _run() -> tuple[str, str, str]:
        try:
            return explicit._endpoint(), v1_base._endpoint(), responses._endpoint()
        finally:
            await explicit.close()
            await v1_base.close()
            await responses.close()

    assert asyncio.run(_run()) == (
        "https://llm.example.test/custom/chat/completions",
        "https://llm.example.test/v1/chat/completions",
        "https://llm.example.test/v1/responses",
    )


def test_strategy_llm_normalize_backfills_trade_plan_claim_ids_from_prediction_contract() -> None:
    normalized = StrategyLLMProvider._normalize_candidate_payload(
        {
            "name": "claim-linked llm candidate",
            "strategy_type": "momentum",
            "target_symbols": ["600000"],
            "dsl": {
                "version": "1.0",
                "timeframe": "daily",
                "entry": {
                    "trade_plan_node_id": "entry_llm",
                    "op": "gt",
                    "left": {"field": "close"},
                    "right": {"indicator": "sma", "field": "close", "window": 10},
                },
                "exit": {
                    "trade_plan_node_id": "exit_llm",
                    "op": "lt",
                    "left": {"field": "close"},
                    "right": {"indicator": "sma", "field": "close", "window": 10},
                },
            },
            "trade_plan": {
                "entry": {"node_id": "entry_llm", "summary": "enter when thesis confirms"},
                "exit": {"node_id": "exit_llm", "summary": "exit when thesis fails"},
            },
            "evidence_chain": {
                "evidences": [
                    {"evidence_id": "ev_trend", "source_type": "technical", "direction": "up"},
                ]
            },
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "llm_claim_entry",
                        "claim_type": "entry",
                        "expected_move": "up",
                        "evidence_ids": ["ev_trend"],
                    }
                ]
            },
        },
        research_task={"target_symbols": ["600000"], "candidate_family": "momentum"},
        allow_legacy_contract_defaults=True,
    )

    assert normalized is not None
    trade_plan = dict(normalized["trade_plan"])
    assert trade_plan["entry"]["claim_ids"] == ["llm_claim_entry"]
    assert trade_plan["entry"]["evidence_ids"] == ["ev_trend"]
    assert trade_plan["exit"]["claim_ids"] == ["llm_claim_entry"]
    assert normalized["params"]["trade_plan"]["entry"]["claim_ids"] == ["llm_claim_entry"]
    assert normalized["claim_to_trade_plan_map"]["trade_step_to_claim_ids"]["entry_llm"] == [
        "llm_claim_entry"
    ]


def test_factor_llm_endpoint_adds_v1_for_bare_openai_compatible_host() -> None:
    from akshare_mcp.services.factor_llm_provider import FactorLLMConfig, FactorLLMProvider

    provider = FactorLLMProvider(
        FactorLLMConfig(
            enabled=True,
            base_url="https://icoe.pp.ua",
            api_key="test-key",
            model="test-model",
        )
    )

    async def _run() -> str:
        try:
            return provider._endpoint()
        finally:
            await provider.close()

    assert asyncio.run(_run()) == "https://icoe.pp.ua/v1/chat/completions"


def test_strategy_llm_extracts_markdown_fenced_json() -> None:
    provider = _provider_with_responses(
        [
            _chat_response(
                '```json\n{"confidence":0.81,"events":[{"code":"600519","reason":"ok"}]}\n```'
            )
        ]
    )

    async def _run() -> dict:
        try:
            _pin_mock_client_to_current_loop(provider)
            return await provider.call_stage(
                stage_id="event_recognition",
                input_data={"topic": "test"},
                system_prompt="Return JSON.",
            )
        finally:
            await provider.close()

    result = asyncio.run(_run())
    assert result["confidence"] == 0.81
    assert result["events"][0]["code"] == "600519"


def test_strategy_llm_extracts_json_from_prose() -> None:
    provider = _provider_with_responses(
        [
            _chat_response(
                'Here is the result:\n{"confirmations":[{"code":"000001","score":0.7}],"confidence":0.7}\nDone.'
            )
        ]
    )

    async def _run() -> dict:
        try:
            _pin_mock_client_to_current_loop(provider)
            return await provider.call_stage(
                stage_id="market_confirmation",
                input_data={"topic": "test"},
                system_prompt="Return JSON.",
            )
        finally:
            await provider.close()

    result = asyncio.run(_run())
    assert result["confirmations"][0]["code"] == "000001"
    assert result["confidence"] == 0.7


def test_strategy_llm_bad_non_empty_json_does_not_trigger_compatibility_cooldown() -> None:
    provider = _provider_with_responses(
        [
            _chat_response('{"confirmations":[{"code":"000001","score":0.7}],'),
            _chat_response('{"confirmations":[{"code":"000001","score":0.8}],"confidence":0.8}'),
            _chat_response('{"candidates":[],"analysis":{"hypothesis":"still usable"}}'),
        ],
        stage_retry_count=1,
    )

    async def _run() -> tuple[dict, dict]:
        try:
            _pin_mock_client_to_current_loop(provider)
            stage_result = await provider.call_stage(
                stage_id="market_confirmation",
                input_data={"topic": "test"},
                system_prompt="Return JSON.",
            )
            generated = await provider.generate_candidates(limit=1, research_task={"topic": "test"})
            return stage_result, generated or {}
        finally:
            await provider.close()

    stage_result, generated = asyncio.run(_run())
    assert stage_result["confirmations"][0]["score"] == 0.8
    assert generated["analysis"]["hypothesis"] == "still usable"
    health = provider.get_health_snapshot()
    assert health["compatibility_cooldown_active"] is False
    assert health["scheduler_should_disable"] is False
    assert health["last_error_type"] is None


def test_strategy_llm_repairs_prose_stage_response_before_fallback() -> None:
    captured_payloads: list[dict] = []
    responses = [
        _chat_response("我会这样处理：输出应包含确认列表，但这里没有直接给 JSON。"),
        _chat_response('{"confirmations":[{"symbol":"000001","confirmed":true,"signal_strength":"moderate"}]}'),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content.decode("utf-8")))
        assert responses
        return responses.pop(0)

    provider = _provider_with_handler(handler, stage_retry_count=0)

    async def _run() -> dict:
        try:
            _pin_mock_client_to_current_loop(provider)
            return await provider.call_stage(
                stage_id="market_confirmation",
                input_data={"topic": "test"},
                system_prompt="Return JSON.",
            )
        finally:
            await provider.close()

    result = asyncio.run(_run())
    assert result["confirmations"][0]["symbol"] == "000001"
    assert result["confirmations"][0]["confirmed"] is True
    assert len(captured_payloads) == 2
    assert "strict JSON repair layer" in captured_payloads[1]["messages"][0]["content"]
    assert "Previous assistant response that was not valid JSON" in captured_payloads[1]["messages"][1]["content"]
    health = provider.get_health_snapshot()
    assert health["compatibility_cooldown_active"] is False
    assert health["scheduler_should_disable"] is False


def test_strategy_llm_bad_non_empty_json_final_failure_keeps_provider_available() -> None:
    provider = _provider_with_responses(
        [
            _chat_response('{"events":[{"code":"600519"}],'),
            _chat_response("still not json"),
            _chat_response('{"candidates":[],"analysis":{"hypothesis":"still usable"}}'),
        ],
        stage_retry_count=0,
    )

    async def _run() -> dict:
        try:
            _pin_mock_client_to_current_loop(provider)
            try:
                await provider.call_stage(
                    stage_id="event_recognition",
                    input_data={"topic": "test"},
                    system_prompt="Return JSON.",
                )
            except Exception as exc:
                payload = json.loads(str(exc).split("attempts: ", 1)[-1])
                assert "not valid JSON" in payload["error"]
            return await provider.generate_candidates(limit=1, research_task={"topic": "test"}) or {}
        finally:
            await provider.close()

    generated = asyncio.run(_run())
    assert generated["analysis"]["hypothesis"] == "still usable"
    assert provider.get_health_snapshot()["compatibility_cooldown_active"] is False


def test_strategy_llm_missing_candidates_records_output_format_metrics() -> None:
    provider = _provider_with_responses(
        [
            _chat_response('{"analysis":{"hypothesis":"schema miss"}}'),
        ],
        stage_retry_count=0,
    )

    async def _run() -> dict:
        try:
            _pin_mock_client_to_current_loop(provider)
            try:
                await provider.generate_candidates(limit=1, research_task={"topic": "test"})
            except StrategyLLMRequestError as exc:
                return dict(exc.metrics)
            raise AssertionError("missing candidates should fail the provider contract")
        finally:
            await provider.close()

    metrics = asyncio.run(_run())
    assert metrics["output_format_failed"] is True
    assert metrics["validation_failure_reason"] == "missing_candidates"
    assert metrics["output_keys"] == ["analysis"]
    assert metrics["attempts"][0]["output_format_failed"] is True
    assert provider.get_health_snapshot()["compatibility_cooldown_active"] is False


def test_strategy_llm_stage_prompt_is_self_describing() -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured["user_prompt"] = payload["messages"][1]["content"]
        return _chat_response('{"confirmations":[{"symbol":"000001","confirmed":true}]}')

    provider = _provider_with_handler(handler, stage_retry_count=0)

    async def _run() -> dict:
        try:
            _pin_mock_client_to_current_loop(provider)
            return await provider.call_stage(
                stage_id="market_confirmation",
                input_data={"headline": "test headline"},
                system_prompt="Return JSON.",
            )
        finally:
            await provider.close()

    result = asyncio.run(_run())
    assert result["confirmations"][0]["confirmed"] is True
    user_prompt = captured["user_prompt"]
    assert "Execute Strategy Factory pipeline stage: market_confirmation" in user_prompt
    assert "The required top-level key is: confirmations" in user_prompt
    assert "Return only one valid JSON object" in user_prompt
    assert '"headline":"test headline"' in user_prompt


def test_strategy_llm_candidate_prompt_pins_root_shape() -> None:
    system_prompt, user_prompt = StrategyLLMProvider._build_prompt(
        snapshot={"date": "2026-06-19"},
        market_summary={"rows": 10},
        research_context={"active_factors": ["momentum"]},
        parent_strategies=[],
        history_summary=[],
        limit=1,
    )

    assert "only two top-level keys: analysis and candidates" in system_prompt
    assert "Do not put analysis subfields at the root" in system_prompt
    payload = json.loads(user_prompt)
    contract = payload["output_contract"]
    assert contract["required"] == ["analysis", "candidates"]
    assert "invalid_root_examples" in contract
    assert "valid_root_example" in contract


def test_strategy_llm_candidate_token_budget_allows_full_contract() -> None:
    provider = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://llm.example.test/v1",
            api_key="test-key",
            model="test-model",
            max_tokens=8000,
        )
    )
    try:
        assert provider._max_tokens_for_attempt(1, 0) >= 3000
        assert provider._max_tokens_for_attempt(2, 0) > provider._max_tokens_for_attempt(1, 0)
        assert provider._max_tokens_for_attempt(10, 0) <= 8000
    finally:
        asyncio.run(provider.close())


def test_strategy_llm_replays_event_stream_response_after_non_json_body() -> None:
    provider = _provider_with_responses(
        [
            _sse_response('{"events":[{"theme_code":"ai","event_type":"policy"}]}'),
            _sse_response('{"events":[{"theme_code":"ai","event_type":"policy"}]}'),
        ],
        stage_retry_count=0,
    )

    async def _run() -> dict:
        try:
            _pin_mock_client_to_current_loop(provider)
            return await provider.call_stage(
                stage_id="event_recognition",
                input_data={"topic": "test"},
                system_prompt="Return JSON.",
            )
        finally:
            await provider.close()

    result = asyncio.run(_run())
    assert result["events"][0]["theme_code"] == "ai"
    health = provider.get_health_snapshot()
    assert health["compatibility_cooldown_active"] is False
    assert health["scheduler_should_disable"] is False


def test_strategy_llm_should_retry_with_event_stream_content_type() -> None:
    provider = _provider_with_responses([], stage_retry_count=0)
    exc = RuntimeError("response body is not valid JSON")
    compat_exc = type(
        "CompatExc",
        (Exception,),
        {},
    )("response body is not valid JSON")
    compat_exc.metrics = {"response_content_type": "text/event-stream; charset=utf-8"}
    assert provider._should_retry_with_stream(compat_exc) is True
    assert provider._should_retry_with_stream(exc) is False


def test_recent_timeout_cooldown_defaults_are_lenient(monkeypatch) -> None:
    """单次超时不应锁死 LLM 整轮:默认 streak>=3 且冷却窗口短(<=120s)。"""
    for key in (
        "STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK",
        "STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC",
        "STRATEGY_LLM_RECENT_CONNECTIVITY_MINIMAL_STREAK",
        "STRATEGY_LLM_RECENT_CONNECTIVITY_COOLDOWN_SEC",
    ):
        monkeypatch.delenv(key, raising=False)
    config = StrategyLLMConfig.from_env()
    assert config.recent_timeout_minimal_streak == 3
    assert config.recent_timeout_cooldown_sec == 120.0
    # connectivity 默认继承 timeout 默认值
    assert config.recent_connectivity_minimal_streak == 3
    assert config.recent_connectivity_cooldown_sec == 120.0


def test_recent_timeout_cooldown_env_override(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK", "5")
    monkeypatch.setenv("STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC", "45")
    config = StrategyLLMConfig.from_env()
    assert config.recent_timeout_minimal_streak == 5
    assert config.recent_timeout_cooldown_sec == 45.0


def test_stage_timeout_cooldown_skips_following_stage_request() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ReadTimeout("stage read timed out", request=request)

    provider = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://llm.example.test/v1",
            api_key="test-key",
            model="test-model",
            stage_retry_count=0,
            stage_retry_backoff_sec=0.0,
            recent_timeout_minimal_streak=1,
            recent_timeout_cooldown_sec=60.0,
            max_concurrency=1,
        )
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run() -> tuple[dict, dict]:
        try:
            _pin_mock_client_to_current_loop(provider)
            try:
                await provider.call_stage(
                    stage_id="theme_propagation",
                    input_data={"topic": "timeout"},
                    system_prompt="Return JSON.",
                    timeout_sec=0.01,
                )
            except StrategyLLMRequestError as exc:
                first = dict(exc.metrics)
            else:  # pragma: no cover - defensive
                raise AssertionError("first stage call should fail")

            try:
                await provider.call_stage(
                    stage_id="exposure_mapping",
                    input_data={"topic": "skip"},
                    system_prompt="Return JSON.",
                    timeout_sec=0.01,
                )
            except StrategyLLMRequestError as exc:
                second = dict(exc.metrics)
            else:  # pragma: no cover - defensive
                raise AssertionError("second stage call should be skipped")
            return first, second
        finally:
            await provider.close()

    first, second = asyncio.run(_run())

    assert len(calls) == 1
    assert first["status"] == "failed"
    assert first["last_error_type"] == "ReadTimeout"
    assert first["recent_timeout_streak"] == 1
    assert second["status"] == "cooldown_skip"
    assert second["cooldown_reason"] == "recent_timeout"
    assert second["last_error_type"] == "RecentTimeoutCooldown"


def test_runtime_cooldown_fallbacks_are_lenient_for_partial_config() -> None:
    provider = StrategyLLMProvider(
        StrategyLLMConfig(
            enabled=True,
            base_url="https://llm.example.test/v1",
            api_key="test-key",
            model="test-model",
        )
    )
    try:
        provider.config = SimpleNamespace(
            initial_compact_level=0,
            recent_timeout_minimal_streak=3,
            recent_timeout_cooldown_sec=120.0,
        )

        provider._record_request_failure(httpx.ConnectError("connect failed"))
        assert 0 < provider._recent_connectivity_cooldown_until - time.monotonic() <= 120.0
        assert provider._active_connectivity_failure() is None

        response = httpx.Response(429, headers={})
        request_error = httpx.HTTPStatusError(
            "429 Too Many Requests",
            request=httpx.Request("POST", "https://llm.example.test/v1/chat/completions"),
            response=response,
        )
        provider._record_request_failure(request_error)
        assert 0 < provider._recent_overload_cooldown_until - time.monotonic() <= 120.0
        _level, reason = provider._recent_failure_degrade_state()
        assert reason is None

        compat_exc = type("CompatExc", (Exception,), {})("bad provider shape")
        compat_exc.metrics = {"last_error_type": "ProviderCompatibilityError"}
        provider._record_compatibility_failure(compat_exc)
        provider._record_compatibility_failure(compat_exc)
        provider._record_compatibility_failure(compat_exc)
        assert 0 < provider._compatibility_cooldown_until - time.monotonic() <= 120.0
    finally:
        asyncio.run(provider.close())
