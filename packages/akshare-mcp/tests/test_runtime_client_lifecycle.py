from __future__ import annotations

import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta, time

import httpx

from akshare_mcp.services.financial_semantic_service import FinancialSemanticConfig, FinancialSemanticService
from akshare_mcp.services.factor_llm_provider import (
    FactorLLMConfig,
    FactorLLMProvider,
    FactorLLMProviderCompatibilityError,
    FactorLLMRequestError,
)
from akshare_mcp.services.text_embedding import StrategyTextEmbeddingConfig, StrategyTextEmbeddingService
from akshare_mcp.services._vector_platform_backend import _StrategyVectorPlatformBackendMixin


def test_text_embedding_service_builds_http_client_lazily() -> None:
    service = StrategyTextEmbeddingService(
        StrategyTextEmbeddingConfig(
            enabled=True,
            provider="openai_compatible",
            base_url="https://embedding.example.test/v1",
            api_key="test-key",
            model="test-embedding",
        )
    )

    async def _run() -> None:
        try:
            assert service._client is None
            status = service.status()
            assert status["ready"] is True
            assert status["health_status"] == "ready"
            assert status["client_state"] == "idle"
            assert status["rebuild_recommended"] is False
            assert _StrategyVectorPlatformBackendMixin._embedding_service_is_closed(service) is False
            await service.ensure_client()
            assert isinstance(service._client, httpx.AsyncClient)
        finally:
            await service.close()

    asyncio.run(_run())


def test_financial_semantic_service_builds_http_client_lazily() -> None:
    service = FinancialSemanticService(
        FinancialSemanticConfig(
            enabled=True,
            provider="openai_compatible",
            base_url="https://semantic.example.test/v1",
            api_key="test-key",
            model="test-semantic",
        )
    )

    async def _run() -> None:
        try:
            assert service._client is None
            client = await service._ensure_client()
            assert isinstance(client, httpx.AsyncClient)
        finally:
            await service.close()

    asyncio.run(_run())


def test_factor_llm_provider_builds_http_client_lazily() -> None:
    provider = FactorLLMProvider(
        FactorLLMConfig(
            enabled=True,
            base_url="https://factor.example.test/v1",
            api_key="test-key",
            model="test-factor",
        )
    )

    async def _run() -> None:
        try:
            assert provider._client is None
            assert provider._closed is False
            status = provider.status()
            assert status["ready"] is True
            assert status["health_status"] == "ready"
            assert status["client_state"] == "idle"
            assert status["rebuild_recommended"] is False
            await provider._ensure_client()
            assert isinstance(provider._client, httpx.AsyncClient)
        finally:
            await provider.close()

    asyncio.run(_run())


def test_factor_llm_compatibility_cooldown_requires_minimal_streak(monkeypatch) -> None:
    provider = FactorLLMProvider(
        FactorLLMConfig(
            enabled=True,
            base_url="https://factor.example.test/v1",
            api_key="test-key",
            model="test-factor",
            retry_count=0,
            compatibility_minimal_streak=3,
            compatibility_cooldown_sec=60.0,
            smoke_check_enabled=False,
        )
    )
    prompt = SimpleNamespace(system_prompt="system", user_prompt="user")
    real_requests = 0

    async def fake_request_and_parse_payload(**kwargs):
        nonlocal real_requests
        real_requests += 1
        raise FactorLLMProviderCompatibilityError(
            "generate_candidates: response missing extractable content",
            metrics={
                "status": "compatibility_failed",
                "empty_200_response": False,
            },
        )

    monkeypatch.setattr(provider, "_request_and_parse_payload", fake_request_and_parse_payload)

    async def _expect_request_failure() -> None:
        try:
            await provider.generate_candidates(prompt, candidate_count=1)
        except FactorLLMRequestError as exc:
            assert exc.metrics["status"] == "compatibility_failed"
        else:
            raise AssertionError("compatibility failure should surface as FactorLLMRequestError")

    async def _run() -> None:
        try:
            await _expect_request_failure()
            assert real_requests == 1
            assert provider.status()["compatibility_cooldown_active"] is False

            await _expect_request_failure()
            assert real_requests == 2
            assert provider.status()["compatibility_cooldown_active"] is False

            await _expect_request_failure()
            assert real_requests == 3
            health = provider.status()
            assert health["compatibility_cooldown_active"] is True
            assert health["health_status"] == "blocked"

            try:
                await provider.generate_candidates(prompt, candidate_count=1)
            except FactorLLMRequestError as exc:
                assert exc.metrics["status"] == "compatibility_skip"
            else:
                raise AssertionError("active compatibility cooldown should skip new requests")
            assert real_requests == 3
        finally:
            await provider.close()

    asyncio.run(_run())


def test_market_spot_executor_shutdown_is_idempotent_and_rebuilds() -> None:
    from akshare_mcp.tools.market import helpers

    helpers.shutdown_spot_executor(wait=True)
    assert helpers._spot_executor is None

    try:
        assert helpers.run_with_timeout(lambda: "ok", 1.0) == "ok"
        first_executor = helpers._spot_executor
        assert first_executor is not None

        helpers.shutdown_spot_executor(wait=True)
        assert helpers._spot_executor is None
        helpers.shutdown_spot_executor(wait=True)

        assert helpers.run_with_timeout(lambda: "again", 1.0) == "again"
        assert helpers._spot_executor is not None
        assert helpers._spot_executor is not first_executor
    finally:
        helpers.shutdown_spot_executor(wait=True)


def test_efinance_executor_shutdown_is_idempotent_and_rebuilds() -> None:
    from akshare_mcp.data_source import quotes

    quotes.shutdown_efinance_executor(wait=True)
    assert quotes._efinance_executor is None

    try:
        first_executor = quotes._get_efinance_executor()
        assert first_executor.submit(lambda: 42).result(timeout=1.0) == 42

        quotes.shutdown_efinance_executor(wait=True)
        assert quotes._efinance_executor is None
        quotes.shutdown_efinance_executor(wait=True)

        second_executor = quotes._get_efinance_executor()
        assert second_executor is not first_executor
        assert second_executor.submit(lambda: 7).result(timeout=1.0) == 7
    finally:
        quotes.shutdown_efinance_executor(wait=True)


def test_execution_realtime_executor_shutdown_is_idempotent_and_rebuilds() -> None:
    from akshare_mcp.tools.managers import _execution_manager_support as support

    support.shutdown_realtime_quote_executor(wait=True)
    assert support._REALTIME_QUOTE_EXECUTOR is None

    try:
        first_executor = support._get_realtime_quote_executor()
        assert first_executor.submit(lambda: "quote").result(timeout=1.0) == "quote"
        support._REALTIME_QUOTE_SKIP_UNTIL["000001"] = 1.0

        support.shutdown_realtime_quote_executor(wait=True)
        assert support._REALTIME_QUOTE_EXECUTOR is None
        assert support._REALTIME_QUOTE_SKIP_UNTIL == {}
        support.shutdown_realtime_quote_executor(wait=True)

        second_executor = support._get_realtime_quote_executor()
        assert second_executor is not first_executor
        assert second_executor.submit(lambda: "rebuilt").result(timeout=1.0) == "rebuilt"
    finally:
        support.shutdown_realtime_quote_executor(wait=True)


def test_close_shared_runtime_clients_drains_market_resources_once(monkeypatch) -> None:
    import akshare_mcp.data_source.quotes as quotes
    import akshare_mcp.data_source.tdx_local as tdx_local
    import akshare_mcp.data_source.tdx_tqcenter as tdx_tqcenter
    import akshare_mcp.services as services
    import akshare_mcp.storage as storage
    import akshare_mcp.tools.market.helpers as helpers
    import akshare_mcp.tools.managers._execution_manager_support as execution_support

    calls: list[str] = []

    async def fake_factor_close() -> None:
        calls.append("factor")

    async def fake_strategy_close() -> None:
        calls.append("strategy")

    async def fake_embedding_close() -> None:
        calls.append("embedding")

    async def fake_close_db() -> None:
        calls.append("db")

    async def fake_drain_cleanup_callbacks() -> None:
        calls.append("drain")
        await services.close_shared_runtime_clients()

    class FakeTdxSource:
        def reset_hq(self) -> None:
            calls.append("tdx_hq")

    monkeypatch.setattr(services, "close_factor_llm_provider", fake_factor_close)
    monkeypatch.setattr(services, "close_strategy_llm_provider", fake_strategy_close)
    monkeypatch.setattr(services, "close_strategy_text_embedding_service", fake_embedding_close)
    monkeypatch.setattr(quotes, "shutdown_efinance_executor", lambda **_: calls.append("efinance"))
    monkeypatch.setattr(helpers, "shutdown_spot_executor", lambda **_: calls.append("spot"))
    monkeypatch.setattr(execution_support, "shutdown_realtime_quote_executor", lambda **_: calls.append("execution"))
    monkeypatch.setattr(tdx_local, "get_tdx_local_source", lambda: FakeTdxSource())
    monkeypatch.setattr(tdx_tqcenter, "reset_tq", lambda: calls.append("tqcenter"))
    monkeypatch.setattr(storage, "close_db", fake_close_db)
    monkeypatch.setattr(storage, "drain_cleanup_callbacks", fake_drain_cleanup_callbacks)

    asyncio.run(services.close_shared_runtime_clients())

    assert calls == [
        "factor",
        "strategy",
        "embedding",
        "efinance",
        "spot",
        "execution",
        "tdx_hq",
        "tqcenter",
        "db",
        "drain",
    ]


def test_factor_mining_factory_scheduler_shutdown_awaits_background_task() -> None:
    from akshare_mcp.services.factor_mining_factory.scheduler import FactorMiningFactoryScheduler

    now = datetime.now()
    run_time = (now + timedelta(hours=1)).time().replace(microsecond=0)
    scheduler = FactorMiningFactoryScheduler(mining_time=run_time, maintenance_time=time(23, 59))

    async def _run() -> None:
        scheduler.start()
        task = scheduler._task
        assert task is not None
        assert scheduler._running is True

        await scheduler.shutdown(grace_sec=0.01)
        assert scheduler._running is False
        assert scheduler._task is None
        assert task.done()

        await scheduler.shutdown(grace_sec=0.01)

    asyncio.run(_run())


def test_background_task_tracker_drains_pending_tasks() -> None:
    from akshare_mcp.services.background_tasks import (
        drain_background_tasks,
        pending_background_task_count,
        track_background_task,
    )

    state: list[str] = []

    async def _worker() -> None:
        await asyncio.sleep(0)
        state.append("done")

    async def _run() -> None:
        track_background_task(_worker(), name="test-background-worker")
        assert pending_background_task_count() == 1
        await drain_background_tasks(timeout_seconds=1.0)
        assert state == ["done"]
        assert pending_background_task_count() == 0

    asyncio.run(_run())


def test_sync_artifact_and_evidence_writes_are_drained(monkeypatch) -> None:
    import akshare_mcp.services.artifact_registry as artifact_registry
    import akshare_mcp.services.evidence_chain as evidence_chain
    from akshare_mcp.services.background_tasks import drain_background_tasks

    saved_artifacts: list[dict] = []
    saved_chains: list[dict] = []

    class FakeDb:
        async def save_artifact(self, payload: dict) -> None:
            await asyncio.sleep(0)
            saved_artifacts.append(dict(payload))

        async def save_evidence_chain(self, payload: dict) -> None:
            await asyncio.sleep(0)
            saved_chains.append(dict(payload))

    fake_db = FakeDb()
    monkeypatch.setattr(artifact_registry, "_get_db", lambda: fake_db)
    monkeypatch.setattr(evidence_chain, "_get_db", lambda: fake_db)

    async def _run() -> None:
        artifact_registry.register_artifact({"artifact_id": "artifact:lifecycle", "strategy": "test"})
        chain = evidence_chain.create_chain("trace:lifecycle", "000001", "buy")
        evidence_chain.save_chain(chain)

        assert saved_artifacts == []
        assert saved_chains == []

        await drain_background_tasks(timeout_seconds=1.0)

        assert [item["artifact_id"] for item in saved_artifacts] == ["artifact:lifecycle"]
        assert [item["trace_id"] for item in saved_chains] == ["trace:lifecycle"]

    asyncio.run(_run())
