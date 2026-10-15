"""因子研究专用大模型 provider。"""
from __future__ import annotations

# --- factor_llm_provider_parts/context.py ---
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationInfo, field_validator

from akshare_mcp.env_loader import load_mcp_env
import logging

logger = logging.getLogger(__name__)


class FactorLLMRequestError(RuntimeError):
    """因子研究模型请求异常。"""

    def __init__(self, message: str, *, metrics: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.metrics = dict(metrics or {})


class FactorLLMProviderCompatibilityError(ValueError):
    """Provider 返回 200 但结构不兼容时的显式异常。"""

    def __init__(self, message: str, *, metrics: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.metrics = dict(metrics or {})


@dataclass
class FactorLLMConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: float = 45.0
    connect_timeout_sec: float = 8.0
    write_timeout_sec: float = 10.0
    pool_timeout_sec: float = 5.0
    temperature: float = 0.2
    max_tokens: int = 1800
    retry_count: int = 2
    retry_backoff_sec: float = 1.0
    compatibility_minimal_streak: int = 3
    compatibility_cooldown_sec: float = 120.0
    smoke_check_enabled: bool = True
    smoke_check_ttl_sec: float = 300.0
    max_concurrency: int = 3
    strict: bool = False

    @classmethod
    def from_env(cls) -> "FactorLLMConfig":
        load_mcp_env(override=False, only_prefixes=("FACTOR_LLM_", "STRATEGY_LLM_"))

        def _env_text(name: str, *, fallback: str = "", default: str = "") -> str:
            raw = str(os.getenv(name, "") or "").strip()
            if raw:
                return raw
            if fallback:
                fallback_raw = str(os.getenv(fallback, "") or "").strip()
                if fallback_raw:
                    return fallback_raw
            return str(default or "").strip()

        def _env_bool(name: str, *, fallback: str = "", default: bool = False) -> bool:
            raw = str(os.getenv(name, "") or "").strip()
            if not raw and fallback:
                raw = str(os.getenv(fallback, "") or "").strip()
            if not raw:
                return bool(default)
            return raw.lower() in {"1", "true", "yes", "on"}

        def _env_float(name: str, *, fallback: str = "", default: float) -> float:
            raw = str(os.getenv(name, "") or "").strip()
            if not raw and fallback:
                raw = str(os.getenv(fallback, "") or "").strip()
            if not raw:
                return float(default)
            try:
                return float(raw)
            except Exception:
                return float(default)

        def _env_int(name: str, *, fallback: str = "", default: int) -> int:
            raw = str(os.getenv(name, "") or "").strip()
            if not raw and fallback:
                raw = str(os.getenv(fallback, "") or "").strip()
            if not raw:
                return int(default)
            try:
                return int(raw)
            except Exception:
                return int(default)

        enabled = _env_bool("FACTOR_LLM_ENABLED", fallback="STRATEGY_LLM_ENABLED", default=False)
        timeout_sec = _env_float("FACTOR_LLM_TIMEOUT_SEC", fallback="STRATEGY_LLM_TIMEOUT_SEC", default=45.0)
        return cls(
            enabled=enabled,
            provider=_env_text("FACTOR_LLM_PROVIDER", fallback="STRATEGY_LLM_PROVIDER", default="openai_compatible"),
            base_url=_env_text("FACTOR_LLM_BASE_URL", fallback="STRATEGY_LLM_BASE_URL"),
            api_key=_env_text("FACTOR_LLM_API_KEY", fallback="STRATEGY_LLM_API_KEY"),
            model=_env_text("FACTOR_LLM_MODEL", fallback="STRATEGY_LLM_MODEL"),
            timeout_sec=timeout_sec,
            connect_timeout_sec=_env_float(
                "FACTOR_LLM_CONNECT_TIMEOUT_SEC",
                fallback="STRATEGY_LLM_CONNECT_TIMEOUT_SEC",
                default=min(timeout_sec, 8.0),
            ),
            write_timeout_sec=_env_float(
                "FACTOR_LLM_WRITE_TIMEOUT_SEC",
                fallback="STRATEGY_LLM_WRITE_TIMEOUT_SEC",
                default=min(timeout_sec, 10.0),
            ),
            pool_timeout_sec=_env_float(
                "FACTOR_LLM_POOL_TIMEOUT_SEC",
                fallback="STRATEGY_LLM_POOL_TIMEOUT_SEC",
                default=min(timeout_sec, 5.0),
            ),
            temperature=_env_float("FACTOR_LLM_TEMPERATURE", fallback="STRATEGY_LLM_TEMPERATURE", default=0.2),
            max_tokens=max(256, _env_int("FACTOR_LLM_MAX_TOKENS", fallback="STRATEGY_LLM_MAX_TOKENS", default=1800)),
            retry_count=max(0, _env_int("FACTOR_LLM_RETRY_COUNT", fallback="STRATEGY_LLM_RETRY_COUNT", default=2)),
            retry_backoff_sec=max(
                0.0,
                _env_float("FACTOR_LLM_RETRY_BACKOFF_SEC", fallback="STRATEGY_LLM_RETRY_BACKOFF_SEC", default=1.0),
            ),
            compatibility_cooldown_sec=max(
                0.0,
                _env_float(
                    "FACTOR_LLM_COMPATIBILITY_COOLDOWN_SEC",
                    fallback="STRATEGY_LLM_COMPATIBILITY_COOLDOWN_SEC",
                    default=120.0,
                ),
            ),
            compatibility_minimal_streak=max(
                1,
                min(
                    8,
                    _env_int(
                        "FACTOR_LLM_COMPATIBILITY_MINIMAL_STREAK",
                        fallback="STRATEGY_LLM_COMPATIBILITY_MINIMAL_STREAK",
                        default=3,
                    ),
                ),
            ),
            smoke_check_enabled=_env_bool(
                "FACTOR_LLM_SMOKE_CHECK_ENABLED",
                fallback="STRATEGY_LLM_SMOKE_CHECK_ENABLED",
                default=True,
            ),
            smoke_check_ttl_sec=max(
                0.0,
                _env_float(
                    "FACTOR_LLM_SMOKE_CHECK_TTL_SEC",
                    fallback="STRATEGY_LLM_SMOKE_CHECK_TTL_SEC",
                    default=300.0,
                ),
            ),
            max_concurrency=max(
                1,
                min(8, _env_int("FACTOR_LLM_MAX_CONCURRENCY", fallback="STRATEGY_LLM_MAX_CONCURRENCY", default=3)),
            ),
            strict=_env_bool("FACTOR_LLM_STRICT_MODE", fallback="STRATEGY_LLM_STRICT_MODE", default=False),
        )


class FactorCandidateModel(BaseModel):
    """结构化候选因子。"""

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., min_length=2, max_length=80)
    hypothesis: str = Field(..., min_length=4, max_length=400)
    family: str = Field(default="custom", min_length=2, max_length=40)
    inputs: list[str] = Field(default_factory=list, min_length=1, max_length=24)
    expression_dsl: str = Field(..., min_length=3, max_length=600)
    expected_holding_period: int = Field(default=10, ge=1, le=252)
    expected_regime: list[str] = Field(default_factory=list, max_length=12)
    complexity_hint: str = Field(default="medium", min_length=1, max_length=20)
    novelty_rationale: str = Field(default="", max_length=400)
    generation_trace: dict[str, Any] = Field(default_factory=dict)
    source_model: str = Field(default="", max_length=120)

    @field_validator("name", "family", "hypothesis", "expression_dsl", "novelty_rationale", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any, info: ValidationInfo) -> str:
        text = " ".join(str(value or "").strip().split())
        max_lengths = {
            "name": 80,
            "family": 40,
            "hypothesis": 400,
            "expression_dsl": 600,
            "novelty_rationale": 400,
        }
        max_length = max_lengths.get(str(info.field_name or ""))
        if max_length and len(text) > max_length:
            return text[:max_length].rstrip()
        return text

    @field_validator("inputs", "expected_regime", mode="before")
    @classmethod
    def _normalize_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            parts = [item.strip() for item in re.split(r"[,;\n|]+", value) if item.strip()]
            return parts
        if isinstance(value, (list, tuple, set)):
            return [" ".join(str(item or "").strip().split()) for item in value if str(item or "").strip()]
        return [" ".join(str(value).strip().split())] if str(value).strip() else []

    @field_validator("complexity_hint", mode="before")
    @classmethod
    def _normalize_complexity(cls, value: Any) -> str:
        text = str(value or "medium").strip().lower()
        if text not in {"low", "medium", "high"}:
            return "medium"
        return text

    @field_validator("expected_holding_period", mode="before")
    @classmethod
    def _normalize_expected_holding_period(cls, value: Any) -> int:
        if value is None:
            return 10
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value or "").strip().lower()
        if not text:
            return 10
        numbers = [int(item) for item in re.findall(r"\d+", text)]
        if not numbers:
            return 10
        if len(numbers) >= 2 and any(token in text for token in ("-", "to", "~", "–", "—")):
            return int(round((numbers[0] + numbers[1]) / 2))
        return int(numbers[0])


class FactorGenerationEnvelope(BaseModel):
    """候选因子生成返回结构。"""

    model_config = ConfigDict(extra="allow")

    candidates: list[FactorCandidateModel] = Field(default_factory=list, min_length=1, max_length=32)
    analysis: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("analysis", mode="before")
    @classmethod
    def _normalize_analysis(cls, value: Any) -> dict[str, Any]:
        # LLM 常把 analysis 作为整段说明文字(字符串)返回,而非结构化 dict。
        # 旧定义强制 dict 导致整批合法候选因 1 个校验错误被全部丢弃(实测 LLM 26.7s
        # 正常返回 6 个因子却报 FactorLLMRequestError)。这里宽容化:字符串包成
        # {"summary": text},None→{},其它标量包成 {"summary": str(value)}。与 warnings 同策略。
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            text = value.strip()
            return {"summary": text} if text else {}
        if isinstance(value, (list, tuple, set)):
            return {"items": [str(item) for item in value]}
        return {"summary": str(value)}

    @field_validator("warnings", mode="before")
    @classmethod
    def _normalize_warnings(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []


_FACTOR_GENERATION_ADAPTER = TypeAdapter(FactorGenerationEnvelope)
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "factor_candidate.schema.json"


def get_factor_candidate_schema_path() -> str:
    """返回候选因子 schema 路径。"""

    return str(_SCHEMA_PATH)


def load_factor_candidate_schema() -> dict[str, Any]:
    """读取候选因子 schema 文件。"""

    with _SCHEMA_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def factor_candidate_runtime_schema() -> dict[str, Any]:
    """返回运行时 pydantic schema。"""

    return FactorGenerationEnvelope.model_json_schema()


def validate_factor_generation_payload(
    payload: Any,
    *,
    model: str = "",
    provider: str = "",
) -> dict[str, Any]:
    """校验模型返回的候选因子结构。"""

    normalized = payload
    if isinstance(payload, list):
        normalized = {"candidates": payload}
    elif isinstance(payload, dict) and "candidates" not in payload:
        if payload.get("name") and payload.get("expression_dsl"):
            normalized = {"candidates": [payload]}
        else:
            normalized = {"candidates": payload.get("items") or []}

    envelope = _FACTOR_GENERATION_ADAPTER.validate_python(normalized)
    data = envelope.model_dump(mode="json")
    for candidate in data.get("candidates", []):
        trace = dict(candidate.get("generation_trace") or {})
        if provider and not trace.get("provider"):
            trace["provider"] = provider
        if model and not candidate.get("source_model"):
            candidate["source_model"] = model
        if model and not trace.get("model"):
            trace["model"] = model
        candidate["generation_trace"] = trace
    return data

# --- factor_llm_provider_parts/specs.py ---
class FactorLLMProvider:
    """因子研究大模型 provider。"""

    def __init__(self, config: Optional[FactorLLMConfig] = None):
        self.config = config or FactorLLMConfig.from_env()
        self._client: httpx.AsyncClient | Any | None = None
        self._closed = False
        self._request_semaphore = asyncio.Semaphore(max(1, int(self.config.max_concurrency or 1)))
        self._created_at = datetime.now().astimezone()
        self._last_request_at: Optional[datetime] = None
        self._last_success_at: Optional[datetime] = None
        self._last_error_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_error_type: Optional[str] = None
        self._last_latency_ms: Optional[float] = None
        self._request_count = 0
        self._success_count = 0
        self._consecutive_failures = 0
        self._rebuild_count = 0
        self._last_rebuild_at: Optional[datetime] = None
        self._compatibility_cooldown_until = 0.0
        self._compatibility_streak = 0
        self._last_compatibility_failure_metrics: dict[str, Any] = {}
        self._last_smoke_check_at: Optional[datetime] = None
        self._last_smoke_check_ok_at: Optional[datetime] = None
        self._last_smoke_check_error: Optional[str] = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(follow_redirects=True, http2=False)

    @property
    def is_closed(self) -> bool:
        if self._closed or self._client is None:
            return True
        try:
            return bool(getattr(self._client, "is_closed"))
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/factor_llm_provider.py:375: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return False

    def _client_state(self) -> str:
        if self._closed:
            return "closed"
        client = self._client
        if client is None:
            return "idle"
        try:
            return "closed" if bool(getattr(client, "is_closed")) else "open"
        except Exception:
            return "unknown"

    def client_state(self) -> str:
        return self._client_state()

    async def _ensure_client(self) -> None:
        if not self.is_closed:
            return
        self._client = self._build_client()
        self._closed = False

    async def close(self) -> None:
        client = self._client
        self._client = None
        self._closed = True
        if client is None:
            return
        try:
            close = getattr(client, "aclose", None)
            if close is None:
                return
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/factor_llm_provider.py:411: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

    def is_enabled(self) -> bool:
        return bool(self.config.enabled and self.config.base_url and self.config.api_key and self.config.model)

    @staticmethod
    def _isoformat(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        try:
            observed = value if value.tzinfo is not None else value.astimezone()
        except Exception:
            observed = value
        return observed.isoformat()

    def _configured(self) -> bool:
        return bool(self.config.base_url and self.config.api_key and self.config.model)

    def _health_status(self) -> str:
        if not bool(self.config.enabled):
            return "disabled"
        if not self._configured():
            return "misconfigured"
        if self._client_state() == "closed":
            return "closed"
        if self._active_compatibility_failure() is not None:
            return "blocked"
        if self._consecutive_failures > 0:
            return "degraded"
        return "ready"

    def _rebuild_recommended(self) -> bool:
        last_error_type = str(self._last_error_type or "").lower()
        timeout_like_error = last_error_type in {
            "connecterror",
            "readtimeout",
            "writetimeout",
            "pooltimeout",
            "timeouterror",
            "factorllmprovidercompatibilityerror",
            "jsondecodeerror",
        }
        client_state = self._client_state()
        return bool(
            self._active_compatibility_failure() is not None
            or client_state == "closed"
            or self._consecutive_failures > 0
            or timeout_like_error
        )

    def status(self) -> dict[str, Any]:
        compatibility_metrics = self._active_compatibility_failure()
        return {
            "provider": str(self.config.provider or "openai_compatible"),
            "model": str(self.config.model or ""),
            "enabled": bool(self.config.enabled),
            "configured": self._configured(),
            "ready": bool(
                self.is_enabled()
                and self._client_state() != "closed"
                and self._consecutive_failures == 0
                and compatibility_metrics is None
            ),
            "client_closed": bool(self._client_state() == "closed"),
            "client_state": self._client_state(),
            "health_status": self._health_status(),
            "rebuild_recommended": self._rebuild_recommended(),
            "request_count": int(self._request_count),
            "success_count": int(self._success_count),
            "consecutive_failures": int(self._consecutive_failures),
            "rebuild_count": int(self._rebuild_count),
            "created_at": self._isoformat(self._created_at),
            "last_request_at": self._isoformat(self._last_request_at),
            "last_success_at": self._isoformat(self._last_success_at),
            "last_error_at": self._isoformat(self._last_error_at),
            "last_error_type": self._last_error_type,
            "last_error": self._last_error,
            "last_latency_ms": self._last_latency_ms,
            "last_rebuild_at": self._isoformat(self._last_rebuild_at),
            "compatibility_cooldown_active": compatibility_metrics is not None,
            "compatibility_cooldown_sec": (
                compatibility_metrics.get("compatibility_cooldown_sec") if compatibility_metrics else 0.0
            ),
            "last_compatibility_failure": compatibility_metrics,
            "smoke_check_enabled": bool(self.config.smoke_check_enabled),
            "last_smoke_check_at": self._isoformat(self._last_smoke_check_at),
            "last_smoke_check_ok_at": self._isoformat(self._last_smoke_check_ok_at),
            "last_smoke_check_error": self._last_smoke_check_error,
        }

    async def rebuild_client(self, *, reason: str = "manual") -> dict[str, Any]:
        previous = self.status()
        await self.close()
        await self._ensure_client()
        self._consecutive_failures = 0
        self._rebuild_count += 1
        self._last_rebuild_at = datetime.now().astimezone()
        self._compatibility_cooldown_until = 0.0
        self._compatibility_streak = 0
        self._last_compatibility_failure_metrics = {}
        current = self.status()
        current["rebuild_reason"] = reason
        return {
            "status": "rebuilt",
            "reason": reason,
            "before": previous,
            "after": current,
        }

    def _mark_success(self, *, latency_ms: float) -> None:
        now = datetime.now().astimezone()
        self._request_count += 1
        self._success_count += 1
        self._consecutive_failures = 0
        self._last_request_at = now
        self._last_success_at = now
        self._last_latency_ms = round(float(latency_ms), 2)
        self._last_error = None
        self._last_error_type = None
        self._compatibility_cooldown_until = 0.0
        self._compatibility_streak = 0
        self._last_compatibility_failure_metrics = {}

    def _mark_failure(self, exc: Exception, *, latency_ms: float) -> None:
        now = datetime.now().astimezone()
        self._request_count += 1
        self._consecutive_failures += 1
        self._last_request_at = now
        self._last_error_at = now
        self._last_latency_ms = round(float(latency_ms), 2)
        self._last_error_type = exc.__class__.__name__
        self._last_error = self._error_text(exc)

    @staticmethod
    def _openai_compatible_api_base(base_url: str) -> str:
        base = str(base_url or "").rstrip("/")
        if not base:
            return base
        lowered = base.lower()
        if lowered.endswith(("/chat/completions", "/responses", "/messages")):
            return base
        remainder = base.split("://", 1)[1] if "://" in base else base
        path = remainder.split("/", 1)[1] if "/" in remainder else ""
        if not path:
            return f"{base}/v1"
        return base

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        lowered = base.lower()
        if lowered.endswith("/chat/completions"):
            return base
        if lowered.endswith("/messages"):
            return base
        api_base = self._openai_compatible_api_base(base)
        provider = str(getattr(self.config, "provider", "") or "").strip().lower()
        if provider in {"anthropic_messages", "anthropic"}:
            return f"{api_base}/messages"
        return f"{api_base}/chat/completions"

    def _adapt_payload_for_endpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(getattr(self.config, "provider", "") or "").strip().lower()
        if provider not in {"anthropic_messages", "anthropic"}:
            return payload
        messages = payload.get("messages") or []
        system_parts: list[str] = []
        adapted_messages: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user").strip().lower()
            content = msg.get("content", "")
            if role == "system":
                if content not in (None, ""):
                    system_parts.append(str(content))
                continue
            if role not in {"user", "assistant"}:
                role = "user"
            adapted_messages.append({"role": role, "content": content if content is not None else ""})
        if not adapted_messages:
            adapted_messages.append({"role": "user", "content": ""})
        raw_max_tokens = payload.get("max_tokens") or payload.get("max_output_tokens") or self.config.max_tokens or 1024
        try:
            max_tokens = max(1, int(raw_max_tokens))
        except Exception:
            max_tokens = 1024
        adapted = {
            "model": payload.get("model", self.config.model),
            "messages": adapted_messages,
            "max_tokens": max_tokens,
        }
        if system_parts:
            adapted["system"] = "\n\n".join(system_parts)
        if payload.get("temperature") is not None:
            adapted["temperature"] = payload["temperature"]
        return adapted

    def _timeout(self) -> httpx.Timeout:
        timeout_sec = max(float(self.config.timeout_sec or 45.0), 5.0)
        connect_timeout = max(1.0, min(float(self.config.connect_timeout_sec or timeout_sec), timeout_sec))
        write_timeout = max(1.0, min(float(self.config.write_timeout_sec or timeout_sec), timeout_sec))
        pool_timeout = max(1.0, min(float(self.config.pool_timeout_sec or timeout_sec), timeout_sec))
        return httpx.Timeout(connect=connect_timeout, read=timeout_sec, write=write_timeout, pool=pool_timeout)

    @staticmethod
    def _error_text(exc: Exception) -> str:
        text = str(exc or "").strip()
        return text or exc.__class__.__name__

    def _active_compatibility_failure(self) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        cooldown_until = float(self._compatibility_cooldown_until or 0.0)
        metrics = dict(self._last_compatibility_failure_metrics or {})
        if not metrics:
            return None
        if cooldown_until <= 0:
            return None
        if cooldown_until > 0 and cooldown_until <= now:
            self._compatibility_cooldown_until = 0.0
            self._last_compatibility_failure_metrics = {}
            return None
        metrics["compatibility_cooldown_sec"] = round(max(cooldown_until - now, 0.0), 4)
        return metrics

    @staticmethod
    def _has_direct_generation_payload(payload: Any) -> bool:
        if isinstance(payload, list):
            return True
        if not isinstance(payload, dict):
            return False
        return bool(
            "candidates" in payload
            or "items" in payload
            or (payload.get("name") and payload.get("expression_dsl"))
        )

    def _response_structure_metrics(
        self,
        payload: Any,
        *,
        response: Optional[httpx.Response],
        raw_text_preview: str = "",
        content_preview: str = "",
    ) -> dict[str, Any]:
        body = dict(payload or {}) if isinstance(payload, dict) else {}
        choices = list(body.get("choices") or [])
        first_choice = dict(choices[0] or {}) if choices else {}
        message = dict((first_choice or {}).get("message") or {})
        usage = dict(body.get("usage") or {})
        try:
            raw_response_preview = (
                json.dumps(payload, ensure_ascii=False, default=str)[:400]
                if payload is not None
                else raw_text_preview[:400]
            )
        except Exception:
            raw_response_preview = str(payload if payload is not None else raw_text_preview)[:400]
        return {
            "response_status_code": getattr(response, "status_code", None),
            "response_content_type": str((getattr(response, "headers", {}) or {}).get("content-type") or ""),
            "response_keys": sorted(str(key) for key in list(body.keys())[:20]),
            "choice_keys": sorted(str(key) for key in list(first_choice.keys())[:20]),
            "message_keys": sorted(str(key) for key in list(message.keys())[:20]),
            "completion_tokens": usage.get("completion_tokens"),
            "raw_response_preview": raw_response_preview,
            "raw_text_preview": str(raw_text_preview or "")[:400],
            "content_preview": str(content_preview or "")[:400],
        }

    @staticmethod
    def _response_content_type(response: Optional[httpx.Response]) -> str:
        return str((getattr(response, "headers", {}) or {}).get("content-type") or "").strip().lower()

    def _raise_compatibility_error(
        self,
        message: str,
        *,
        response: Optional[httpx.Response],
        payload: Any = None,
        raw_text_preview: str = "",
        content_preview: str = "",
        empty_200_response: bool = False,
    ) -> None:
        metrics = self._response_structure_metrics(
            payload,
            response=response,
            raw_text_preview=raw_text_preview,
            content_preview=content_preview,
        )
        raise FactorLLMProviderCompatibilityError(
            message,
            metrics={
                "status": "compatibility_failed",
                "last_error_type": "FactorLLMProviderCompatibilityError",
                "last_error_status_code": getattr(response, "status_code", None),
                "empty_200_response": bool(empty_200_response),
                **metrics,
            },
        )

    @staticmethod
    def _extract_json_text(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return raw
        fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", raw, flags=re.S)
        if fence:
            return fence.group(1).strip()
        if raw.startswith("{") or raw.startswith("["):
            return raw
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return raw[start : end + 1]
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            return raw[start : end + 1]
        return raw

    @staticmethod
    def _extract_content(payload: dict[str, Any]) -> str:
        anthropic_content = payload.get("content")
        if isinstance(anthropic_content, list):
            parts = []
            for item in anthropic_content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
                elif item not in (None, ""):
                    parts.append(str(item))
            if parts:
                return "\n".join(parts)
        if isinstance(anthropic_content, str) and anthropic_content:
            return anthropic_content
        choices = list(payload.get("choices") or [])
        if choices:
            message = dict((choices[0] or {}).get("message") or {})
            content = message.get("content")
            if isinstance(content, list):
                return "\n".join(str(item.get("text") or item) for item in content)
            if content not in (None, ""):
                return str(content)
            # Fallback: DeepSeek-V4-pro / R1 style returns reasoning_content when content is empty
            # (2026-05-28: official DeepSeek API returns msg.reasoning_content for v4-pro)
            reasoning_content = message.get("reasoning_content")
            if isinstance(reasoning_content, list):
                return "\n".join(str(item.get("text") or item) for item in reasoning_content)
            if reasoning_content not in (None, ""):
                return str(reasoning_content)
            return ""
        output = list(payload.get("output") or [])
        parts = []
        for item in output:
            for content in list((item or {}).get("content") or []):
                text = content.get("text") if isinstance(content, dict) else None
                if text:
                    parts.append(str(text))
        return "\n".join(parts)

    @classmethod
    def _append_text_fragments(cls, fragments: list[str], value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            fragments.append(value)
            return
        if isinstance(value, list):
            for item in value:
                cls._append_text_fragments(fragments, item)
            return
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                fragments.append(str(value.get("text") or ""))
            elif "text" in value:
                cls._append_text_fragments(fragments, value.get("text"))
            if isinstance(value.get("delta"), str):
                fragments.append(str(value.get("delta") or ""))
            elif "delta" in value:
                cls._append_text_fragments(fragments, value.get("delta"))
            if "content" in value:
                cls._append_text_fragments(fragments, value.get("content"))
            if "output_text" in value:
                cls._append_text_fragments(fragments, value.get("output_text"))
            # DeepSeek-V4-pro / R1 style: also collect reasoning_content from deltas/messages
            # (2026-05-28: official DeepSeek API returns reasoning_content for v4-pro)
            if "reasoning_content" in value:
                cls._append_text_fragments(fragments, value.get("reasoning_content"))

    @classmethod
    def _extract_stream_event_text(cls, payload: Any, *, event_name: str = "") -> str:
        fragments: list[str] = []
        if not isinstance(payload, dict):
            return ""

        for choice in list(payload.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            cls._append_text_fragments(fragments, choice.get("delta"))
            cls._append_text_fragments(fragments, choice.get("message"))

        payload_type = str(payload.get("type") or event_name or "").strip().lower()
        if payload_type.endswith("output_text.delta"):
            cls._append_text_fragments(fragments, payload.get("delta"))
        if payload_type.endswith("output_text.done"):
            cls._append_text_fragments(fragments, payload.get("text"))

        cls._append_text_fragments(fragments, payload.get("item"))
        cls._append_text_fragments(fragments, payload.get("response"))
        return "".join(fragments)

    @staticmethod
    def _parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        current_event: Optional[str] = None
        current_data_lines: list[str] = []

        def _flush() -> None:
            nonlocal current_event, current_data_lines
            if current_event is None and not current_data_lines:
                return
            data_raw = "\n".join(current_data_lines).strip()
            record: dict[str, Any] = {
                "event": current_event,
                "data_raw": data_raw,
            }
            if data_raw == "[DONE]":
                record["done"] = True
            elif data_raw:
                try:
                    record["payload"] = json.loads(data_raw)
                except Exception:
                    record["payload"] = None
            events.append(record)
            current_event = None
            current_data_lines = []

        for raw_line in str(raw_text or "").splitlines():
            line = raw_line.rstrip("\r")
            if not line.strip():
                _flush()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line[len("event:") :].strip() or None
                continue
            if line.startswith("data:"):
                current_data_lines.append(line[len("data:") :].strip())
                continue
        _flush()
        return events

    def _should_retry_with_stream(self, exc: FactorLLMProviderCompatibilityError) -> bool:
        metrics = dict(getattr(exc, "metrics", {}) or {})
        content_type = str(metrics.get("response_content_type") or "").strip().lower()
        error_text = self._error_text(exc).lower()
        if "missing extractable content" in error_text:
            return True
        return "text/event-stream" in content_type

    async def _stream_parse_response_payload(
        self,
        *,
        headers: dict[str, Any],
        request_payload: dict[str, Any],
        request_kind: str,
    ) -> tuple[Any, Any, str]:
        stream_method = getattr(self._client, "stream", None)
        if not callable(stream_method):
            raise FactorLLMProviderCompatibilityError(
                f"{request_kind}: stream fallback unavailable for event-stream compatibility replay",
                metrics={
                    "status": "compatibility_failed",
                    "response_content_type": "text/event-stream",
                    "stream_fallback_unavailable": True,
                },
            )

        stream_payload = dict(request_payload or {})
        stream_payload["stream"] = True
        raw_chunks: list[str] = []
        async with self._client.stream(
            "POST",
            self._endpoint(),
            headers={**headers, "Accept": "text/event-stream, application/json"},
            json=stream_payload,
            timeout=self._timeout(),
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_text():
                if chunk:
                    raw_chunks.append(str(chunk))
        raw_text = "".join(raw_chunks)
        events = self._parse_sse_events(raw_text)
        content_parts: list[str] = []
        last_payload: dict[str, Any] = {}
        for event in events:
            payload = event.get("payload")
            if isinstance(payload, dict):
                last_payload = payload
                text = self._extract_stream_event_text(payload, event_name=str(event.get("event") or ""))
                if text:
                    content_parts.append(text)
        content = "".join(content_parts)
        if not str(content or "").strip():
            self._raise_compatibility_error(
                f"{request_kind}: stream replay still missing extractable content",
                response=response,
                payload=last_payload or None,
                raw_text_preview=raw_text,
                content_preview=content,
            )
        parsed_text = self._extract_json_text(content)
        if not str(parsed_text or "").strip():
            self._raise_compatibility_error(
                f"{request_kind}: stream replay content missing JSON payload",
                response=response,
                payload=last_payload or None,
                raw_text_preview=raw_text,
                content_preview=content,
            )
        try:
            parsed = json.loads(parsed_text)
        except Exception as exc:
            self._raise_compatibility_error(
                f"{request_kind}: stream replay content is not valid JSON ({self._error_text(exc)})",
                response=response,
                payload=last_payload or None,
                raw_text_preview=raw_text,
                content_preview=content,
            )
        synthetic_body = {
            "id": last_payload.get("id") if isinstance(last_payload, dict) else None,
            "object": last_payload.get("object") if isinstance(last_payload, dict) else None,
            "model": last_payload.get("model") if isinstance(last_payload, dict) else None,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": last_payload.get("usage") if isinstance(last_payload, dict) else None,
            "compatibility_mode": "chat_stream_replay",
        }
        return parsed, synthetic_body, content

    async def _request_and_parse_payload(
        self,
        *,
        headers: dict[str, Any],
        request_payload: dict[str, Any],
        request_kind: str,
    ) -> tuple[Any, Any, str, str]:
        actual_payload = self._adapt_payload_for_endpoint(request_payload)
        response = await self._client.post(
            self._endpoint(),
            headers=headers,
            json=actual_payload,
            timeout=self._timeout(),
        )
        response.raise_for_status()
        try:
            parsed, body, content = self._parse_response_payload(
                response,
                request_kind=request_kind,
            )
            return parsed, body, content, "direct"
        except FactorLLMProviderCompatibilityError as exc:
            if not self._should_retry_with_stream(exc):
                raise
            parsed, body, content = await self._stream_parse_response_payload(
                headers=headers,
                request_payload=request_payload,
                request_kind=request_kind,
            )
            return parsed, body, content, "chat_stream_replay"

    def _parse_response_payload(
        self,
        response: httpx.Response,
        *,
        request_kind: str,
    ) -> tuple[Any, Any, str]:
        try:
            raw_text = str(getattr(response, "text", "") or "")
        except Exception:
            raw_text = ""
        try:
            body = response.json()
        except Exception as exc:
            empty_200_response = int(getattr(response, "status_code", 0) or 0) == 200 and not raw_text.strip()
            self._raise_compatibility_error(
                f"{request_kind}: response body is not valid JSON ({self._error_text(exc)})",
                response=response,
                raw_text_preview=raw_text,
                empty_200_response=empty_200_response,
            )
        if self._has_direct_generation_payload(body):
            return body, body, ""
        content = self._extract_content(body if isinstance(body, dict) else {})
        if not str(content or "").strip():
            self._raise_compatibility_error(
                f"{request_kind}: response missing extractable content",
                response=response,
                payload=body,
                raw_text_preview=raw_text,
                empty_200_response=int(getattr(response, "status_code", 0) or 0) == 200 and not raw_text.strip(),
            )
        parsed_text = self._extract_json_text(content)
        if not str(parsed_text or "").strip():
            self._raise_compatibility_error(
                f"{request_kind}: response content missing JSON payload",
                response=response,
                payload=body,
                raw_text_preview=raw_text,
                content_preview=content,
            )
        try:
            parsed = json.loads(parsed_text)
        except Exception as exc:
            self._raise_compatibility_error(
                f"{request_kind}: response content is not valid JSON ({self._error_text(exc)})",
                response=response,
                payload=body,
                raw_text_preview=raw_text,
                content_preview=content,
            )
        return parsed, body, content

    def _record_compatibility_failure(self, exc: Exception, *, latency_ms: float) -> None:
        self._mark_failure(exc, latency_ms=latency_ms)
        metrics = dict(getattr(exc, "metrics", {}) or {})
        self._last_compatibility_failure_metrics = {
            "status": "compatibility_failed",
            "last_error_type": self._last_error_type,
            "last_error": self._last_error,
            **metrics,
        }
        self._compatibility_streak = int(getattr(self, "_compatibility_streak", 0) or 0) + 1
        minimal_streak = max(1, int(getattr(self.config, "compatibility_minimal_streak", 3) or 3))
        if bool(metrics.get("empty_200_response")):
            minimal_streak *= 2
        cooldown_sec = max(0.0, float(getattr(self.config, "compatibility_cooldown_sec", 300.0) or 300.0))
        if self._compatibility_streak >= minimal_streak and cooldown_sec > 0:
            self._compatibility_cooldown_until = time.monotonic() + cooldown_sec
        else:
            self._compatibility_cooldown_until = 0.0

    async def smoke_check(self, *, force: bool = False) -> dict[str, Any]:
        if not self.is_enabled():
            return {"status": "disabled"}
        if not bool(self.config.smoke_check_enabled) and not force:
            return {"status": "disabled"}
        compatibility_metrics = self._active_compatibility_failure()
        if compatibility_metrics is not None and not force:
            return {
                **compatibility_metrics,
                "status": "compatibility_skip",
            }
        now = datetime.now().astimezone()
        ttl_sec = max(0.0, float(self.config.smoke_check_ttl_sec or 0.0))
        if (
            not force
            and self._last_smoke_check_ok_at is not None
            and ttl_sec > 0
            and (now - self._last_smoke_check_ok_at).total_seconds() <= ttl_sec
        ):
            self._last_smoke_check_at = now
            return {
                "status": "cached_success",
                "last_smoke_check_ok_at": self._isoformat(self._last_smoke_check_ok_at),
                "smoke_check_ttl_sec": ttl_sec,
            }

        await self._ensure_client()
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        payload = {
            "model": str(self.config.model or "").strip(),
            "temperature": 0,
            "max_tokens": min(64, max(16, int(self.config.max_tokens or 64))),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return a compact JSON object with an ok field."},
                {"role": "user", "content": '{"ok": true}'},
            ],
        }
        started = time.perf_counter()
        try:
            parsed, body, _content, compatibility_mode = await self._request_and_parse_payload(
                headers=headers,
                request_payload=payload,
                request_kind="smoke_check",
            )
            if not isinstance(parsed, dict):
                self._raise_compatibility_error(
                    "smoke_check: parsed payload is not a JSON object",
                    response=None,
                    payload=body,
                    content_preview="",
                )
            self._mark_success(latency_ms=(time.perf_counter() - started) * 1000)
            self._last_smoke_check_at = now
            self._last_smoke_check_ok_at = now
            self._last_smoke_check_error = None
            return {
                "status": "passed",
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "compatibility_mode": compatibility_mode,
            }
        except FactorLLMProviderCompatibilityError as exc:
            self._record_compatibility_failure(exc, latency_ms=(time.perf_counter() - started) * 1000)
            self._last_smoke_check_at = now
            self._last_smoke_check_error = self._error_text(exc)
            raise FactorLLMRequestError(
                "factor llm smoke check failed",
                metrics={
                    "status": "smoke_check_failed",
                    "provider": str(self.config.provider or "openai_compatible"),
                    "model": str(self.config.model or ""),
                    **dict(getattr(exc, "metrics", {}) or {}),
                },
            )
        except Exception as exc:
            self._mark_failure(exc, latency_ms=(time.perf_counter() - started) * 1000)
            self._last_smoke_check_at = now
            self._last_smoke_check_error = self._error_text(exc)
            raise FactorLLMRequestError(
                "factor llm smoke check failed",
                metrics={
                    "status": "smoke_check_failed",
                    "provider": str(self.config.provider or "openai_compatible"),
                    "model": str(self.config.model or ""),
                    "last_error_type": exc.__class__.__name__,
                    "last_error": self._error_text(exc),
                },
            )

    async def generate_candidates(
        self,
        prompt: Any,
        *,
        candidate_count: int = 8,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """调用模型生成候选因子。"""

        if not self.is_enabled():
            raise FactorLLMRequestError("factor llm provider not configured")
        await self._ensure_client()

        system_prompt = str(getattr(prompt, "system_prompt", "") or "")
        user_prompt = str(getattr(prompt, "user_prompt", "") or "")
        if not system_prompt or not user_prompt:
            raise FactorLLMRequestError("factor mining prompt is empty")

        resolved_model = str(model or self.config.model or "").strip()
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        request_payload = {
            "model": resolved_model,
            "temperature": float(self.config.temperature or 0.2),
            "max_tokens": int(self.config.max_tokens or 1800),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        last_error: Optional[FactorLLMRequestError] = None
        compatibility_metrics = self._active_compatibility_failure()
        if compatibility_metrics is not None:
            raise FactorLLMRequestError(
                "factor llm request skipped during compatibility cooldown",
                metrics={
                    "provider": str(self.config.provider or "openai_compatible"),
                    "model": resolved_model,
                    **compatibility_metrics,
                    "status": "compatibility_skip",
                },
            )
        async with self._request_semaphore:
            for attempt in range(max(1, int(self.config.retry_count or 0) + 1)):
                started = time.perf_counter()
                try:
                    parsed, body, _content, compatibility_mode = await self._request_and_parse_payload(
                        headers=headers,
                        request_payload=request_payload,
                        request_kind="generate_candidates",
                    )
                    validated = validate_factor_generation_payload(
                        parsed,
                        model=resolved_model,
                        provider=str(self.config.provider or "openai_compatible"),
                    )
                    self._mark_success(latency_ms=(time.perf_counter() - started) * 1000)
                    return {
                        **validated,
                        "provider": str(self.config.provider or "openai_compatible"),
                        "model": resolved_model,
                        "candidate_count": len(validated.get("candidates", [])),
                        "requested_candidate_count": max(1, int(candidate_count)),
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                        "raw_usage": body.get("usage") if isinstance(body, dict) else None,
                        "compatibility_mode": compatibility_mode,
                    }
                except FactorLLMProviderCompatibilityError as exc:
                    self._record_compatibility_failure(exc, latency_ms=(time.perf_counter() - started) * 1000)
                    last_error = FactorLLMRequestError(
                        self._error_text(exc),
                        metrics={
                            "attempt": attempt + 1,
                            "provider": str(self.config.provider or "openai_compatible"),
                            "model": resolved_model,
                            "candidate_count": max(1, int(candidate_count)),
                            **dict(getattr(exc, "metrics", {}) or {}),
                        },
                    )
                    break
                except Exception as exc:
                    last_error = FactorLLMRequestError(
                        self._error_text(exc),
                        metrics={
                            "attempt": attempt + 1,
                            "provider": str(self.config.provider or "openai_compatible"),
                            "model": resolved_model,
                            "candidate_count": max(1, int(candidate_count)),
                        },
                    )
                    self._mark_failure(exc, latency_ms=(time.perf_counter() - started) * 1000)
                    if attempt >= int(self.config.retry_count or 0):
                        break
                    await asyncio.sleep(max(0.0, float(self.config.retry_backoff_sec or 0.0)) * (attempt + 1))

        if last_error is None:
            last_error = FactorLLMRequestError("unknown factor llm failure")
        raise last_error

# --- factor_llm_provider_parts/runtime.py ---
_factor_llm_provider: Optional[FactorLLMProvider] = None


def get_factor_llm_provider() -> FactorLLMProvider:
    """返回全局 provider 单例。"""

    global _factor_llm_provider
    if _factor_llm_provider is None or bool(getattr(_factor_llm_provider, "_closed", False)):
        _factor_llm_provider = FactorLLMProvider()
    return _factor_llm_provider


async def close_factor_llm_provider() -> None:
    global _factor_llm_provider
    provider = _factor_llm_provider
    _factor_llm_provider = None
    if provider is None:
        return
    await provider.close()

