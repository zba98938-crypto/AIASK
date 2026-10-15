from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from .env_config import load_project_env
import logging

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .model_client import ModelClient
    from .memory import FinancialMemoryStore


PINNED_SYSTEM_MESSAGE_NAMES = {
    "handoff_state",
    "handoff_specialist_policy",
}


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += max(1, len(str(message.get("content") or "")) // 4)
        total += 8
    return total


@dataclass
class ContextResult:
    messages: list[dict[str, Any]]
    compacted: bool = False
    summary_id: str | None = None
    summary: str | None = None


class ContextManager:
    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        memory_store: FinancialMemoryStore | None = None,
        max_tokens: int | None = None,
        head_messages: int = 2,
        tail_messages: int = 12,
    ) -> None:
        self.model_client = model_client
        self.memory_store = memory_store
        self.max_tokens = int(max_tokens or os.getenv("AIASK_AGENT_CONTEXT_MAX_TOKENS", "12000"))
        self.head_messages = max(1, int(head_messages))
        self.tail_messages = max(4, int(tail_messages))

    async def prepare(self, messages: list[dict[str, Any]], user_id: str | None = None) -> ContextResult:
        current = list(messages or [])
        if estimate_tokens(current) <= self.max_tokens:
            return ContextResult(messages=current)
        if len(current) <= 2:
            return ContextResult(messages=current)
        head_count = min(self.head_messages, len(current) - 1)
        tail_count = min(self.tail_messages, max(1, len(current) - head_count - 1))
        head = current[:head_count]
        tail = current[-tail_count:]
        middle = current[head_count:-tail_count]
        pinned_middle = [message for message in middle if self._is_pinned_system_message(message)]
        compactable_middle = [message for message in middle if not self._is_pinned_system_message(message)]

        if not compactable_middle:
            return ContextResult(messages=[*head, *pinned_middle, *tail])
        
        summary = await self._summarize_middle(compactable_middle, user_id=user_id)
        summary_id = f"ctx_{uuid4().hex}"
        compacted = [
            *head,
            *pinned_middle,
            {
                "role": "system",
                "name": "context_summary",
                "content": (
                    "Compressed prior AIASK Agent context. Preserve these facts when answering or calling tools.\n"
                    f"summary_id={summary_id}\n{summary}"
                ),
                "metadata": {"context_summary_id": summary_id},
            },
            *tail,
        ]
        return ContextResult(messages=compacted, compacted=True, summary_id=summary_id, summary=summary)

    @staticmethod
    def _is_pinned_system_message(message: dict[str, Any]) -> bool:
        return (
            str(message.get("role") or "").strip().lower() == "system"
            and str(message.get("name") or "").strip() in PINNED_SYSTEM_MESSAGE_NAMES
        )

    async def _summarize_middle(self, messages: list[dict[str, Any]], user_id: str | None = None) -> str:
        if not self.model_client:
            return self._fallback_summarize(messages)

        lines: list[str] = []
        for message in messages:
            role = str(message.get("role") or "message")
            name = str(message.get("name") or "")
            content = str(message.get("content") or "")
            prefix = f"{role}:{name}" if name else role
            lines.append(f"[{prefix}]: {content}")
        
        raw_text = "\n\n".join(lines)
        prompt = (
            "You are a trajectory compressor for a financial agent.\n"
            "Analyze the following conversation history and tool outputs.\n"
            "Extract the core financial conclusions, data points, strategy configurations, and unfinished tasks.\n"
            "Generate a highly compact Markdown summary that preserves all crucial facts needed for future reasoning.\n\n"
            f"<history>\n{raw_text}\n</history>"
        )

        try:
            load_project_env()
            response = await self.model_client.complete(
                messages=[{"role": "user", "content": prompt}],
                model=os.getenv("AIASK_AGENT_MODEL", "gpt-4.1-mini")
            )
            summary = response.content or self._fallback_summarize(messages)
            
            if self.memory_store and response.content:
                try:
                    self.memory_store.add(
                        content=summary,
                        user_id=user_id,
                        research_topic="trajectory_compression_summary"
                    )
                except Exception as _swallowed:
                    logger.warning("packages/agent/src/aiask_agent/context.py:132: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                    pass
                    
            return summary
        except Exception:
            return self._fallback_summarize(messages)

    @staticmethod
    def _fallback_summarize(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages[-40:]:
            role = str(message.get("role") or "message")
            name = str(message.get("name") or "")
            content = str(message.get("content") or "").replace("\n", " ")
            if len(content) > 500:
                content = content[:500] + "..."
            if content:
                prefix = f"{role}:{name}" if name else role
                lines.append(f"- {prefix}: {content}")
        if not lines:
            return "- No compactable prior content."
        return "\n".join(lines)
