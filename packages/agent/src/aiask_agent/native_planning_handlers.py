from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .session_store import AgentSessionStore
from .todo import FinancialTodoStore


def build_planning_handlers(
    _envelope: Callable[..., dict[str, Any]],
    *,
    todos: FinancialTodoStore,
    session_store: AgentSessionStore,
) -> dict[str, Any]:
    def _scoped_session(
        arguments: dict[str, Any],
        *,
        tool_name: str,
        level: str,
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        runtime_context = dict(arguments.get("_aiask_runtime_context") or {})
        runtime_session_id = str(runtime_context.get("session_id") or "").strip()
        session_id = str(arguments.get("session_id") or runtime_session_id or "default").strip() or "default"
        runtime_user_id = str(runtime_context.get("user_id") or "").strip()
        user_id = str(arguments.get("user_id") or runtime_user_id or "").strip() or None
        if runtime_session_id and session_id != runtime_session_id:
            payload = _envelope(
                False,
                error="planning tool cannot target a different session than the active runtime",
                tool_name=tool_name,
                level=level,
                target=session_id,
                idempotent=False,
            )
            payload["error_code"] = "PLANNING_SESSION_SCOPE_VIOLATION"
            payload["meta"]["planning_session_guard"] = {
                "blocked": True,
                "reason": "runtime_context_scope_mismatch",
            }
            return session_id, user_id, payload
        if runtime_user_id and user_id and user_id != runtime_user_id:
            payload = _envelope(
                False,
                error="planning tool cannot target a different user than the active runtime",
                tool_name=tool_name,
                level=level,
                target=session_id,
                idempotent=False,
            )
            payload["error_code"] = "PLANNING_SESSION_SCOPE_VIOLATION"
            payload["meta"]["planning_session_guard"] = {
                "blocked": True,
                "reason": "runtime_context_user_mismatch",
            }
            return session_id, user_id, payload
        return session_id, user_id, None

    async def clarify(arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments.get("question") or "").strip()
        if not question:
            return _envelope(False, error="question is required", tool_name="agent_clarify", level="read_only")
        options = [str(item) for item in list(arguments.get("options") or []) if str(item).strip()]
        return _envelope(
            True,
            data={"question": question, "options": options, "requires_user_input": True},
            tool_name="agent_clarify",
            level="read_only",
            target="user",
        )

    async def todo_set(arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, user_id, scope_error = _scoped_session(arguments, tool_name="agent_todo_set", level="stateful")
        if scope_error is not None:
            return scope_error
        items = list(arguments.get("items") or [])
        result = todos.set_items(
            session_id=session_id,
            user_id=user_id,
            items=[dict(item) for item in items if isinstance(item, dict)],
            merge=bool(arguments.get("merge", False)),
        )
        return _envelope(
            True,
            data={"session_id": session_id, "items": result},
            tool_name="agent_todo_set",
            level="stateful",
            target=session_id,
            idempotent=False,
        )

    async def todo_list(arguments: dict[str, Any]) -> dict[str, Any]:
        session_id, _user_id, scope_error = _scoped_session(arguments, tool_name="agent_todo_list", level="read_only")
        if scope_error is not None:
            return scope_error
        return _envelope(
            True,
            data={"session_id": session_id, "items": todos.list_items(session_id=session_id)},
            tool_name="agent_todo_list",
            level="read_only",
            target=session_id,
        )

    async def todo(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_todo"
        action = str(arguments.get("action") or "list").strip().lower()
        level = "read_only" if action in {"list", "status"} else "stateful"
        session_id, user_id, scope_error = _scoped_session(arguments, tool_name=tool, level=level)
        if scope_error is not None:
            return scope_error
        try:
            if action == "list":
                data = {"session_id": session_id, "items": todos.list_items(session_id=session_id)}
            elif action == "clear":
                data = {"session_id": session_id, "items": todos.set_items(session_id=session_id, user_id=user_id, items=[])}
            elif action in {"add", "update"}:
                item_id = str(arguments.get("item_id") or uuid4().hex[:8])
                existing = todos.list_items(session_id=session_id)
                if action == "update":
                    existing = [item for item in existing if item.get("item_id") != item_id]
                existing.append(
                    {
                        "id": item_id,
                        "content": str(arguments.get("content") or "").strip() or "(no description)",
                        "status": str(arguments.get("status") or "pending"),
                    }
                )
                data = {"session_id": session_id, "items": todos.set_items(session_id=session_id, user_id=user_id, items=existing)}
            elif action == "status":
                items = todos.list_items(session_id=session_id)
                counts: dict[str, int] = {}
                for item in items:
                    counts[str(item.get("status") or "pending")] = counts.get(str(item.get("status") or "pending"), 0) + 1
                data = {"session_id": session_id, "count": len(items), "by_status": counts}
            else:
                raise ValueError(f"unsupported todo action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=session_id, idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    async def subgoal(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_subgoal"
        action = str(arguments.get("action") or "list").strip().lower()
        level = "read_only" if action in {"list", "status"} else "stateful"
        session_id, user_id, scope_error = _scoped_session(arguments, tool_name=tool, level=level)
        if scope_error is not None:
            return scope_error
        try:
            if action == "list":
                data = {"session_id": session_id, "subgoals": session_store.list_subgoals(session_id=session_id)}
            elif action == "status":
                items = session_store.list_subgoals(session_id=session_id)
                counts: dict[str, int] = {}
                for item in items:
                    counts[str(item.get("status") or "pending")] = counts.get(str(item.get("status") or "pending"), 0) + 1
                data = {"session_id": session_id, "count": len(items), "by_status": counts, "subgoals": items}
            elif action == "clear":
                data = {"session_id": session_id, "subgoals": session_store.clear_subgoals(session_id=session_id)}
            elif action in {"add", "update"}:
                title = str(arguments.get("title") or "").strip()
                if not title and action == "update" and arguments.get("subgoal_id"):
                    current = session_store.get_subgoal(str(arguments.get("subgoal_id") or ""))
                    title = str((current or {}).get("title") or "")
                item = session_store.upsert_subgoal(
                    session_id=session_id,
                    subgoal_id=arguments.get("subgoal_id") if action == "update" else arguments.get("subgoal_id"),
                    user_id=user_id,
                    title=title,
                    criteria=[str(item) for item in list(arguments.get("criteria") or [])],
                    status=str(arguments.get("status") or "pending"),
                )
                data = {"session_id": session_id, "subgoal": item, "subgoals": session_store.list_subgoals(session_id=session_id)}
            else:
                raise ValueError(f"unsupported subgoal action: {action}")
            return _envelope(True, data=data, tool_name=tool, level=level, target=session_id, idempotent=level == "read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="stateful", idempotent=False)

    return {
        "agent_clarify": clarify,
        "agent_todo_set": todo_set,
        "agent_todo_list": todo_list,
        "agent_todo": todo,
        "agent_subgoal": subgoal,
    }
