"""PR-F (Phase 4, 2026-05-24) — Agent ActionIntent execution chain for
event-driven manager actions.

Drives the full chain ``ActionIntent.create → IntentExecutor.confirm →
desktop_ops.execute_confirmed_action → strategy_factory.execute_confirmed_action
→ strategy_manager.ACTION_HANDLERS["factory_event_*"]``.

Coverage maps to plan §6 Phase 4 acceptance:
    - (1) ``tool_risk.CONFIRM_REQUIRED_STRATEGY_ACTIONS`` 包含 4 个写入 action.
    - (2) ``READ_ONLY_STRATEGY_ACTIONS`` 包含 ``factory_event_list`` /
        ``factory_event_preview_tasks``.
    - (3) ``ALLOWED_ACTIONS`` 派生出 ``strategy_manager.factory_event_create``
        / ``factory_event_update`` / ``factory_event_approve`` /
        ``factory_event_record_outcome``.
    - (4) Untrusted ``factory_event_*`` write actions cannot bypass intent
        creation (read-only ``factory_event_list/preview_tasks`` are not
        promoted to confirm-required by mistake).
    - (5) Confirmed intent dispatches to the strategy_factory adapter with
        the right action name and payload (executor side never bypasses
        manager handler).
    - (6) Without confirmation, the strategy_factory adapter never calls
        the manager handler — the action stays at
        ``awaiting_confirmation`` until ``IntentExecutor.confirm`` runs.

These tests intentionally monkeypatch the executor at the
``strategy_factory.execute_confirmed_action`` boundary (mirroring the
existing ``test_intents.py`` pattern). End-to-end coverage that drives
the real ``ACTION_HANDLERS["factory_event_*"]`` against a SQLite DAO is
already provided by ``packages/akshare-mcp/tests/test_theme_graph_schema.py``
(PR-A/PR-B1, 18 cases).
"""

from __future__ import annotations

import asyncio

import pytest

from aiask_agent.intents import ALLOWED_ACTIONS, ActionIntentStore, IntentExecutor
from aiask_agent.tool_risk import (
    CONFIRM_REQUIRED_STRATEGY_ACTIONS,
    READ_ONLY_STRATEGY_ACTIONS,
    classify_strategy_manager_action,
)


# ---------------------------------------------------------------------------
# (1) + (2) White-list registry coverage — these are pure data assertions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        "factory_event_create",
        "factory_event_update",
        "factory_event_approve",
        "factory_event_record_outcome",
        "factory_event_bootstrap",
        "factory_theme_exposure_refresh",
        "factory_event_outbox_drain",
        "factory_theme_regression_run",
    ],
)
def test_factory_event_write_actions_require_confirmation(action: str) -> None:
    """Phase 4 §6 acceptance #1: 4 个写入 action 必须强制走 ActionIntent."""
    assert action in CONFIRM_REQUIRED_STRATEGY_ACTIONS, action
    side_effect = classify_strategy_manager_action(action)
    assert side_effect["level"] == "stateful", side_effect
    assert side_effect["confirmation_required"] is True, side_effect
    assert side_effect.get("unknown_action") is not True, side_effect


@pytest.mark.parametrize(
    "action",
    [
        "factory_event_list",
        "factory_event_preview_tasks",
        "factory_event_lineage",
        "factory_theme_exposure_status",
        "factory_event_outbox_status",
    ],
)
def test_factory_event_read_actions_are_idempotent(action: str) -> None:
    """Phase 4 §6 acceptance #2: 读 action 走 read-only,无需 confirmation."""
    assert action in READ_ONLY_STRATEGY_ACTIONS, action
    side_effect = classify_strategy_manager_action(action)
    assert side_effect["level"] == "read_only", side_effect
    assert side_effect["confirmation_required"] is False, side_effect
    assert side_effect["idempotent"] is True, side_effect


# ---------------------------------------------------------------------------
# (3) ALLOWED_ACTIONS derivation — ``intents.py`` must accept the new
# ``strategy_manager.factory_event_*`` keys without raising.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        "factory_event_create",
        "factory_event_update",
        "factory_event_approve",
        "factory_event_record_outcome",
        "factory_event_bootstrap",
        "factory_theme_exposure_refresh",
        "factory_event_outbox_drain",
        "factory_theme_regression_run",
    ],
)
def test_allowed_actions_derived_for_factory_event_writes(action: str) -> None:
    key = f"strategy_manager.{action}"
    assert key in ALLOWED_ACTIONS, ALLOWED_ACTIONS
    descriptor = ALLOWED_ACTIONS[key]
    assert descriptor == {"tool": "strategy_manager", "action": action}


# ---------------------------------------------------------------------------
# (4) Read-only event actions must not be promoted to confirm-required.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        "factory_event_list",
        "factory_event_preview_tasks",
        "factory_event_lineage",
        "factory_theme_exposure_status",
        "factory_event_outbox_status",
    ],
)
def test_read_only_event_actions_not_in_confirm_required(action: str) -> None:
    assert action not in CONFIRM_REQUIRED_STRATEGY_ACTIONS, action
    # Read-only actions are not registered in ALLOWED_ACTIONS because they
    # do not need ActionIntent gating — the agent calls them directly.
    assert f"strategy_manager.{action}" not in ALLOWED_ACTIONS


# ---------------------------------------------------------------------------
# (5) Full chain: ActionIntent.create → confirm → strategy_factory adapter.
# Mirrors the existing ``test_confirm_executes_allowed_action_once_and_rejects_repeat``
# pattern but for ``factory_event_create``.
# ---------------------------------------------------------------------------


def test_intent_chain_dispatches_factory_event_create(tmp_path, monkeypatch) -> None:
    """Phase 4 §6 acceptance #3 + #4 integration:
    create intent → confirm → adapter is called with the expected
    ``factory_event_create`` action and payload.
    """

    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    payload = {
        "event_name": "test_event_phase4",
        "event_type": "manual_inject",
        "direction": "bullish",
        "intensity": 0.7,
        "primary_themes": ["AI_chip"],
        "operator_id": "operator_alice",
    }
    intent = store.create(
        action="strategy_manager.factory_event_create",
        params=payload,
        user_id="operator_alice",
    )
    assert intent["status"] == "awaiting_confirmation"
    assert intent["target_tool"] == "strategy_manager"
    assert intent["target_action"] == "factory_event_create"

    captured: list[dict] = []

    async def fake_executor(action: str, params: dict | None = None) -> dict:
        captured.append({"action": action, "params": dict(params or {})})
        return {
            "success": True,
            "data": {"event_id": "evt_ph4_001", "status": "active"},
            "error": None,
        }

    from aiask_agent.adapters import strategy_factory

    monkeypatch.setattr(strategy_factory, "execute_confirmed_action", fake_executor)

    confirmed = asyncio.run(executor.confirm(intent["intent_id"]))
    assert confirmed["success"] is True, confirmed
    # Adapter must receive the unprefixed action name + the original payload.
    assert captured == [{"action": "factory_event_create", "params": payload}]
    assert store.get(intent["intent_id"])["status"] == "succeeded"


def test_intent_chain_dispatches_factory_event_approve(tmp_path, monkeypatch) -> None:
    """Approve flow is the most safety-sensitive — its adapter dispatch
    must carry both ``event_id`` and ``approver_id`` so the manager
    handler can enforce the self-approval guard."""

    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    payload = {"event_id": "evt_ph4_001", "approver_id": "approver_bob"}
    intent = store.create(
        action="strategy_manager.factory_event_approve",
        params=payload,
        user_id="approver_bob",
    )

    captured: list[dict] = []

    async def fake_executor(action: str, params: dict | None = None) -> dict:
        captured.append({"action": action, "params": dict(params or {})})
        return {
            "success": True,
            "data": {"event_id": "evt_ph4_001", "approved_at": "2026-05-24T12:00:00Z"},
            "error": None,
        }

    from aiask_agent.adapters import strategy_factory

    monkeypatch.setattr(strategy_factory, "execute_confirmed_action", fake_executor)

    asyncio.run(executor.confirm(intent["intent_id"]))
    assert captured == [{"action": "factory_event_approve", "params": payload}]


def test_intent_chain_dispatches_factory_event_outbox_drain(tmp_path, monkeypatch) -> None:
    """Maintenance writes must confirm through the same Strategy Manager path."""

    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    payload = {"limit": 20, "event_limit": 5}
    intent = store.create(
        action="strategy_manager.factory_event_outbox_drain",
        params=payload,
        user_id="operator_ops",
    )
    assert intent["status"] == "awaiting_confirmation"
    assert intent["target_tool"] == "strategy_manager"
    assert intent["target_action"] == "factory_event_outbox_drain"

    captured: list[dict] = []

    async def fake_executor(action: str, params: dict | None = None) -> dict:
        captured.append({"action": action, "params": dict(params or {})})
        return {
            "success": True,
            "data": {"processed": 1, "single_worker": True},
            "error": None,
        }

    from aiask_agent.adapters import strategy_factory

    monkeypatch.setattr(strategy_factory, "execute_confirmed_action", fake_executor)

    confirmed = asyncio.run(executor.confirm(intent["intent_id"]))
    assert confirmed["success"] is True, confirmed
    assert captured == [{"action": "factory_event_outbox_drain", "params": payload}]


def test_intent_chain_dispatches_factory_event_bootstrap(tmp_path, monkeypatch) -> None:
    """Bootstrap is a stateful maintenance action and must dispatch via confirm."""

    store = ActionIntentStore(tmp_path / "intents.sqlite3")
    executor = IntentExecutor(store)

    payload = {"batch_size": 1000, "refresh_exposure": True}
    intent = store.create(
        action="strategy_manager.factory_event_bootstrap",
        params=payload,
        user_id="operator_ops",
    )
    assert intent["status"] == "awaiting_confirmation"
    assert intent["target_tool"] == "strategy_manager"
    assert intent["target_action"] == "factory_event_bootstrap"

    captured: list[dict] = []

    async def fake_executor(action: str, params: dict | None = None) -> dict:
        captured.append({"action": action, "params": dict(params or {})})
        return {
            "success": True,
            "data": {"seed": {"status": "seeded"}, "availability": {"available": True}},
            "error": None,
        }

    from aiask_agent.adapters import strategy_factory

    monkeypatch.setattr(strategy_factory, "execute_confirmed_action", fake_executor)

    confirmed = asyncio.run(executor.confirm(intent["intent_id"]))
    assert confirmed["success"] is True, confirmed
    assert captured == [{"action": "factory_event_bootstrap", "params": payload}]


# ---------------------------------------------------------------------------
# (6) Without confirmation, the executor must never run.
# ---------------------------------------------------------------------------


def test_factory_event_create_does_not_dispatch_without_confirm(tmp_path, monkeypatch) -> None:
    """Phase 4 §6 acceptance #5: 未 confirm 时事件不落库.

    The intent stays at ``awaiting_confirmation`` and the adapter is never
    called. We assert this by raising in the fake executor — if the test
    ever invokes it, the assertion fires.
    """

    store = ActionIntentStore(tmp_path / "intents.sqlite3")

    async def trap_executor(action: str, params: dict | None = None) -> dict:
        raise AssertionError(
            "execute_confirmed_action must not run before IntentExecutor.confirm"
        )

    from aiask_agent.adapters import strategy_factory

    monkeypatch.setattr(strategy_factory, "execute_confirmed_action", trap_executor)

    intent = store.create(
        action="strategy_manager.factory_event_create",
        params={"event_name": "should_not_dispatch"},
    )

    # Mere creation must not flip status nor call the adapter.
    assert intent["status"] == "awaiting_confirmation"
    fetched = store.get(intent["intent_id"])
    assert fetched["status"] == "awaiting_confirmation"
    assert fetched["result"] is None
