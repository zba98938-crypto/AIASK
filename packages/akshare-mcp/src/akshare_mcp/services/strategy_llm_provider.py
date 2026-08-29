"""外部 AI 策略生成 provider。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import pandas as pd
from strategy_factory.api.semantic_contract import apply_target_symbol_policy, normalize_research_task_contract

from ..env_loader import load_mcp_env


class StrategyLLMRequestError(RuntimeError):
    def __init__(self, message: str, *, metrics: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.metrics = dict(metrics or {})


@dataclass
class StrategyLLMConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: float = 30.0
    connect_timeout_sec: float = 8.0
    write_timeout_sec: float = 10.0
    pool_timeout_sec: float = 5.0
    temperature: float = 0.3
    max_tokens: int = 900
    retry_count: int = 2
    retry_backoff_sec: float = 1.0
    stage_retry_count: int = 1
    stage_retry_backoff_sec: float = 1.5
    initial_compact_level: int = 0
    # 冷却默认值温和化:单次抖动不锁死整轮(streak=3 连续3次才冷却, cooldown=120s 数轮恢复)。
    # 旧默认 streak=1/cooldown=600 会让一次 ReadTimeout 锁 10 分钟,级联 pipeline 全 stage skip。
    recent_timeout_minimal_streak: int = 3
    recent_timeout_cooldown_sec: float = 120.0
    recent_connectivity_minimal_streak: int = 3
    recent_connectivity_cooldown_sec: float = 120.0
    recent_overload_minimal_streak: int = 3
    recent_overload_cooldown_sec: float = 120.0
    compatibility_minimal_streak: int = 3
    compatibility_cooldown_sec: float = 120.0
    max_concurrency: int = 3
    strict: bool = False

    @classmethod
    def from_env(cls) -> "StrategyLLMConfig":
        load_mcp_env(override=False, only_prefixes=('STRATEGY_LLM_',))
        enabled = str(os.getenv("STRATEGY_LLM_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
        timeout_sec = float(os.getenv("STRATEGY_LLM_TIMEOUT_SEC", "30") or 30)
        initial_compact_level = max(0, min(2, int(os.getenv("STRATEGY_LLM_INITIAL_COMPACT_LEVEL", "0") or 0)))
        # 默认值偏温和:单次外部超时不应锁死 LLM 整轮。streak=3 要求连续3次超时才进冷却,
        # cooldown=120s 让冷却在数轮内自然恢复,避免一次抖动级联成长时间 cooldown_skip 全退本地 fallback。
        recent_timeout_minimal_streak = max(1, min(8, int(os.getenv("STRATEGY_LLM_RECENT_TIMEOUT_MINIMAL_STREAK", "3") or 3)))
        recent_timeout_cooldown_sec = max(0.0, float(os.getenv("STRATEGY_LLM_RECENT_TIMEOUT_COOLDOWN_SEC", "120") or 120))
        recent_connectivity_minimal_streak = max(
            1,
            min(
                8,
                int(
                    os.getenv(
                        "STRATEGY_LLM_RECENT_CONNECTIVITY_MINIMAL_STREAK",
                        str(recent_timeout_minimal_streak),
                    )
                    or recent_timeout_minimal_streak
                ),
            ),
        )
        recent_connectivity_cooldown_sec = max(
            0.0,
            float(
                os.getenv(
                    "STRATEGY_LLM_RECENT_CONNECTIVITY_COOLDOWN_SEC",
                    str(recent_timeout_cooldown_sec),
                )
                or recent_timeout_cooldown_sec
            ),
        )
        recent_overload_minimal_streak = max(1, min(8, int(os.getenv("STRATEGY_LLM_RECENT_OVERLOAD_MINIMAL_STREAK", "3") or 3)))
        recent_overload_cooldown_sec = max(0.0, float(os.getenv("STRATEGY_LLM_RECENT_OVERLOAD_COOLDOWN_SEC", "120") or 120))
        return cls(
            enabled=enabled,
            provider=str(os.getenv("STRATEGY_LLM_PROVIDER", "openai_compatible") or "openai_compatible"),
            base_url=str(os.getenv("STRATEGY_LLM_BASE_URL", "") or "").strip(),
            api_key=str(os.getenv("STRATEGY_LLM_API_KEY", "") or "").strip(),
            model=str(os.getenv("STRATEGY_LLM_MODEL", "") or "").strip(),
            timeout_sec=timeout_sec,
            connect_timeout_sec=float(os.getenv("STRATEGY_LLM_CONNECT_TIMEOUT_SEC", str(min(timeout_sec, 8.0))) or min(timeout_sec, 8.0)),
            write_timeout_sec=float(os.getenv("STRATEGY_LLM_WRITE_TIMEOUT_SEC", str(min(timeout_sec, 10.0))) or min(timeout_sec, 10.0)),
            pool_timeout_sec=float(os.getenv("STRATEGY_LLM_POOL_TIMEOUT_SEC", str(min(timeout_sec, 5.0))) or min(timeout_sec, 5.0)),
            temperature=float(os.getenv("STRATEGY_LLM_TEMPERATURE", "0.3") or 0.3),
            max_tokens=max(128, int(os.getenv("STRATEGY_LLM_MAX_TOKENS", "900") or 900)),
            retry_count=max(0, int(os.getenv("STRATEGY_LLM_RETRY_COUNT", "2") or 2)),
            retry_backoff_sec=max(0.0, float(os.getenv("STRATEGY_LLM_RETRY_BACKOFF_SEC", "1.0") or 1.0)),
            stage_retry_count=max(0, int(os.getenv("STRATEGY_LLM_STAGE_RETRY_COUNT", "1") or 1)),
            stage_retry_backoff_sec=max(
                0.0,
                float(
                    os.getenv(
                        "STRATEGY_LLM_STAGE_RETRY_BACKOFF_SEC",
                        os.getenv("STRATEGY_LLM_RETRY_BACKOFF_SEC", "1.5"),
                    )
                    or 1.5
                ),
            ),
            initial_compact_level=initial_compact_level,
            recent_timeout_minimal_streak=recent_timeout_minimal_streak,
            recent_timeout_cooldown_sec=recent_timeout_cooldown_sec,
            recent_connectivity_minimal_streak=recent_connectivity_minimal_streak,
            recent_connectivity_cooldown_sec=recent_connectivity_cooldown_sec,
            recent_overload_minimal_streak=recent_overload_minimal_streak,
            recent_overload_cooldown_sec=recent_overload_cooldown_sec,
            compatibility_cooldown_sec=max(0.0, float(os.getenv("STRATEGY_LLM_COMPATIBILITY_COOLDOWN_SEC", "120") or 120)),
            compatibility_minimal_streak=max(1, min(8, int(os.getenv("STRATEGY_LLM_COMPATIBILITY_MINIMAL_STREAK", "3") or 3))),
            max_concurrency=max(1, min(16, int(os.getenv("STRATEGY_LLM_MAX_CONCURRENCY", "3") or 3))),
            strict=str(os.getenv("STRATEGY_LLM_STRICT_MODE", "")).strip().lower() in {"1", "true", "yes", "on"},
        )

from ._strategy_llm_provider_normalize import _StrategyLLMProviderNormalizeMixin
from ._strategy_llm_provider_prompt import _StrategyLLMProviderPromptMixin
from ._strategy_llm_provider_runtime import _StrategyLLMProviderRuntimeMixin


class StrategyLLMProvider(_StrategyLLMProviderNormalizeMixin, _StrategyLLMProviderPromptMixin, _StrategyLLMProviderRuntimeMixin):
        def __init__(self, config: Optional[StrategyLLMConfig] = None):
            self.config = config or StrategyLLMConfig.from_env()
            self._recent_timeout_streak = 0
            self._recent_timeout_cooldown_until = 0.0
            self._recent_connectivity_streak = 0
            self._recent_connectivity_cooldown_until = 0.0
            self._recent_overload_streak = 0
            self._recent_overload_cooldown_until = 0.0
            self._last_failure_type: Optional[str] = None
            self._last_failure_status_code: Optional[int] = None
            self._compatibility_cooldown_until = 0.0
            self._last_compatibility_failure_metrics: dict[str, Any] = {}
            self._client: httpx.AsyncClient | Any | None = None
            self._request_semaphore = asyncio.Semaphore(max(1, int(self.config.max_concurrency or 1)))
            self._runtime_loop_id: Optional[int] = None

        async def close(self) -> None:
            """关闭共享 HTTP 连接池。"""
            client = self._client
            self._client = None
            try:
                if client is not None:
                    await client.aclose()
            except Exception:
                pass
            self._runtime_loop_id = None

        def is_enabled(self) -> bool:
            return bool(self.config.enabled and self.config.base_url and self.config.api_key and self.config.model)




_strategy_llm_provider: Optional[StrategyLLMProvider] = None


def get_strategy_llm_provider() -> StrategyLLMProvider:
    global _strategy_llm_provider
    if _strategy_llm_provider is None:
        _strategy_llm_provider = StrategyLLMProvider()
    return _strategy_llm_provider


async def close_strategy_llm_provider() -> None:
    global _strategy_llm_provider
    provider = _strategy_llm_provider
    _strategy_llm_provider = None
    if provider is None:
        return
    await provider.close()
