"""The incubation orchestration path must resolve the DB through the protocol.

``IncubationOrchestrator._get_db`` was the last private reach-through on this
path — its own docstring flagged it as a Phase 3 item. ``get_db()`` is now
declared on the provider protocol and preferred, with the underscored name kept
only as a fallback for hosts that predate it.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from strategy_factory.application.incubation.orchestrator import IncubationOrchestrator
from strategy_factory.application.incubation.provider import IncubationRuntimeSupport
from strategy_factory.runtime.provider_adapters import LegacyIncubationProvider


def test_protocol_declares_public_get_db() -> None:
    assert hasattr(IncubationRuntimeSupport, "get_db"), (
        "IncubationRuntimeSupport must declare get_db(); without it the "
        "orchestrator has to reach for a private attribute"
    )


def test_orchestrator_prefers_the_public_method() -> None:
    sentinel = object()
    calls: list[str] = []

    class _Modern:
        async def get_db(self):
            calls.append("public")
            return sentinel

        async def _get_db(self):
            calls.append("legacy")
            raise AssertionError("the legacy path must not be used when get_db exists")

    resolved = asyncio.run(IncubationOrchestrator(_Modern())._get_db())

    assert resolved is sentinel
    assert calls == ["public"]


def test_orchestrator_falls_back_for_older_hosts() -> None:
    sentinel = object()

    class _Legacy:
        async def _get_db(self):
            return sentinel

    assert asyncio.run(IncubationOrchestrator(_Legacy())._get_db()) is sentinel


def test_orchestrator_names_the_missing_contract() -> None:
    class _Neither:
        pass

    with pytest.raises(RuntimeError, match="get_db"):
        asyncio.run(IncubationOrchestrator(_Neither())._get_db())


def test_legacy_adapter_exposes_both_names() -> None:
    """Existing callers of the underscored name must keep working."""
    sentinel = object()

    class _Support:
        async def _get_db(self):
            return sentinel

    provider = LegacyIncubationProvider(_Support())

    assert asyncio.run(provider.get_db()) is sentinel
    assert asyncio.run(provider._get_db()) is sentinel


def test_legacy_adapter_prefers_a_public_host_method() -> None:
    class _Support:
        async def get_db(self):
            return "public"

        async def _get_db(self):
            return "legacy"

    assert asyncio.run(LegacyIncubationProvider(_Support()).get_db()) == "public"


def test_orchestrator_has_no_other_private_reach_through() -> None:
    """Guard the property: only the documented get_db fallback may remain."""
    source = inspect.getsource(IncubationOrchestrator)
    occurrences = source.count("self._support._")

    assert occurrences <= 1, (
        f"found {occurrences} private reach-throughs in IncubationOrchestrator; "
        "only the legacy _get_db fallback is permitted"
    )
    if occurrences:
        assert "self._support._get_db" not in source or "legacy" in source.lower()
