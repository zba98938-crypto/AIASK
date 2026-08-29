from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from .todo import FinancialTodoStore


@dataclass
class PlannerResult:
    steps: list[dict[str, Any]] = field(default_factory=list)
    prompt_message: dict[str, Any] | None = None


class TaskPlanner:
    def __init__(self, *, enabled: bool | None = None, todo_store: FinancialTodoStore | None = None) -> None:
        raw = os.getenv("AIASK_AGENT_ENABLE_PLANNER", "1").strip().lower()
        self.enabled = enabled if enabled is not None else raw not in {"0", "false", "off", "no"}
        self.todo_store = todo_store or FinancialTodoStore()

    def plan(
        self,
        *,
        messages: list[dict[str, Any]],
        session_id: str,
        user_id: str | None = None,
    ) -> PlannerResult:
        if not self.enabled:
            return PlannerResult()
        latest = self._latest_user_text(messages)
        if not self._looks_complex(latest):
            return PlannerResult()
        steps = self._build_steps(latest)
        self.todo_store.set_items(session_id=session_id, user_id=user_id, items=steps, merge=False)
        prompt = {
            "role": "system",
            "name": "aiask_planner",
            "content": "AIASK execution plan:\n" + "\n".join(f"{item['id']}. {item['content']}" for item in steps),
        }
        return PlannerResult(steps=steps, prompt_message=prompt)

    @staticmethod
    def _latest_user_text(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages or []):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @staticmethod
    def _looks_complex(text: str) -> bool:
        normalized = str(text or "")
        if len(normalized) > 260:
            return True
        separators = len(re.findall(r"[,，;；、]| and | then |然后|并且|同时|再", normalized, flags=re.I))
        finance_terms = len(re.findall(r"股票|策略|回测|治理|校验|风险|估值|因子|组合|stock|strategy|risk", normalized, flags=re.I))
        return separators >= 2 or finance_terms >= 3

    @staticmethod
    def _build_steps(text: str) -> list[dict[str, Any]]:
        candidates = [item.strip() for item in re.split(r"[,，;；、]|\bthen\b|然后|并且|同时|再", text) if item.strip()]
        if len(candidates) < 2:
            candidates = [
                "Clarify the financial target and required evidence.",
                "Call the safest available AIASK financial tools.",
                "Synthesize findings with risks, gaps, and next actions.",
            ]
        return [
            {
                "id": str(index),
                "content": item[:240],
                "status": "pending" if index > 1 else "in_progress",
            }
            for index, item in enumerate(candidates[:8], 1)
        ]
