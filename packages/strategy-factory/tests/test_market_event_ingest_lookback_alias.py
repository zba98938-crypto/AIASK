"""``notice_days`` must not be silently discarded on the modern ingest path.

``run_market_event_ingest.py`` builds its kwargs with ``notice_days`` (never
``lookback_days``). The legacy support branch mapped that name itself, but the
orchestrator branch accepts only ``lookback_days`` — so every ``--notice-days``
value was dropped and the cycle silently ran the default window.
"""

from __future__ import annotations

import pytest

from strategy_factory.infrastructure.runtime_services import (
    clear_runtime_services,
    configure_runtime_services,
)
from strategy_factory.runtime.market_event_ingest import (
    DEFAULT_LOOKBACK_DAYS,
    get_market_event_ingest_runtime,
)


@pytest.fixture(autouse=True)
def _reset_runtime_services():
    clear_runtime_services()
    yield
    clear_runtime_services()


class _ModernSupport:
    """Support object WITHOUT ``_resolve_runtime_args`` -> orchestrator path."""


class _RecordingOrchestrator:
    def __init__(self):
        self.calls: list[dict] = []

    async def run_cycle(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "completed", **kwargs}


def _runtime_with_recorder():
    configure_runtime_services(
        db_provider=lambda: object(),
        market_event_ingest_support_factory=_ModernSupport,
    )
    runtime = get_market_event_ingest_runtime()
    recorder = _RecordingOrchestrator()
    runtime._orchestrator = recorder
    return runtime, recorder


@pytest.mark.asyncio
async def test_notice_days_reaches_the_orchestrator_as_lookback_days() -> None:
    runtime, recorder = _runtime_with_recorder()

    await runtime.run_once(notice_days=30)

    assert recorder.calls[0]["lookback_days"] == 30, (
        "notice_days must map to lookback_days; otherwise --notice-days is a no-op"
    )
    # The alias must not leak through as an unexpected kwarg.
    assert "notice_days" not in recorder.calls[0]


@pytest.mark.asyncio
async def test_explicit_lookback_days_wins_over_the_alias() -> None:
    runtime, recorder = _runtime_with_recorder()

    await runtime.run_once(lookback_days=5, notice_days=30)

    assert recorder.calls[0]["lookback_days"] == 5


@pytest.mark.asyncio
async def test_default_lookback_is_used_when_neither_is_supplied() -> None:
    runtime, recorder = _runtime_with_recorder()

    await runtime.run_once()

    assert recorder.calls[0]["lookback_days"] == DEFAULT_LOOKBACK_DAYS


@pytest.mark.asyncio
async def test_lookback_days_is_floored_at_one() -> None:
    runtime, recorder = _runtime_with_recorder()

    await runtime.run_once(notice_days=0)

    assert recorder.calls[0]["lookback_days"] == 1


@pytest.mark.asyncio
async def test_legacy_branch_still_receives_notice_days() -> None:
    """The legacy support surface keeps its own parameter name."""
    seen: list[dict] = []

    class _LegacySupport:
        def _resolve_runtime_args(self, payload):
            seen.append(dict(payload))
            return {
                "requested_doc_types": [],
                "requested_codes": [],
                "dry_run": True,
                "args_payload": {},
            }

        def _event_source_status(self):
            return {}

    configure_runtime_services(
        db_provider=lambda: object(),
        market_event_ingest_support_factory=_LegacySupport,
    )
    runtime = get_market_event_ingest_runtime()

    await runtime.run_once(notice_days=21, doc_types=["notice"])

    assert seen[0]["notice_days"] == 21
    assert seen[0]["doc_types"] == ["notice"]


@pytest.mark.asyncio
async def test_runner_kwargs_do_not_crash_the_modern_path() -> None:
    """The real runner passes ~18 kwargs; extras must be tolerated, not fatal."""
    runtime, recorder = _runtime_with_recorder()

    await runtime.run_once(
        stock_codes=None,
        doc_types=["notice"],
        news_limit=0,
        notice_limit=50,
        official_notice_limit=50,
        notice_days=14,
        code_notice_limit=10,
        research_code_limit=0,
        research_per_code=0,
        chunk_size=1000,
        overlap=120,
        version="v1",
        embed=False,
        build_snapshot=False,
        activate_snapshot=False,
        allow_network=True,
        dry_run=False,
    )

    assert recorder.calls[0]["lookback_days"] == 14
