"""Text embedding service with multiple provider backends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

from ..env_loader import load_mcp_env


class StrategyTextEmbeddingError(RuntimeError):
    pass


@dataclass
class StrategyTextEmbeddingConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    local_model_path: str = ""
    timeout_sec: float = 20.0
    connect_timeout_sec: float = 8.0
    write_timeout_sec: float = 10.0
    pool_timeout_sec: float = 5.0
    retry_count: int = 2
    retry_backoff_sec: float = 0.75
    smoke_check_enabled: bool = True
    smoke_check_ttl_sec: float = 300.0
    max_text_chars: int = 6000
    normalize_embeddings: bool = True
    hash_dimensions: int = 256
    # Default to False: a real embedding provider must be configured.
    # Hash fallback is intended for offline development only and produces
    # 256-dim vectors that silently degrade semantic search quality.
    # Operators can opt-in via STRATEGY_EMBEDDING_ALLOW_HASH_FALLBACK=1.
    allow_hash_fallback: bool = False

    @classmethod
    def from_env(cls) -> "StrategyTextEmbeddingConfig":
        load_mcp_env(override=False, only_prefixes=("STRATEGY_EMBEDDING_", "STRATEGY_LLM_"))

        def _env_text(name: str, fallback: str = "", default: str = "") -> str:
            raw = str(os.getenv(name, "") or "").strip()
            if raw:
                return raw
            if fallback:
                raw = str(os.getenv(fallback, "") or "").strip()
                if raw:
                    return raw
            return str(default or "").strip()

        def _env_bool(name: str, fallback: str = "", default: bool = False) -> bool:
            raw = str(os.getenv(name, "") or "").strip()
            if not raw and fallback:
                raw = str(os.getenv(fallback, "") or "").strip()
            if not raw:
                return bool(default)
            return raw.lower() in {"1", "true", "yes", "on"}

        def _env_float(name: str, fallback: str = "", default: float = 20.0) -> float:
            raw = str(os.getenv(name, "") or "").strip()
            if not raw and fallback:
                raw = str(os.getenv(fallback, "") or "").strip()
            if not raw:
                return float(default)
            try:
                return float(raw)
            except Exception:
                return float(default)

        def _env_int(name: str, fallback: str = "", default: int = 256) -> int:
            raw = str(os.getenv(name, "") or "").strip()
            if not raw and fallback:
                raw = str(os.getenv(fallback, "") or "").strip()
            if not raw:
                return int(default)
            try:
                return int(raw)
            except Exception:
                return int(default)

        enabled_value = _env_bool("STRATEGY_EMBEDDING_ENABLED", "STRATEGY_LLM_ENABLED", False)
        timeout_sec = _env_float("STRATEGY_EMBEDDING_TIMEOUT_SEC", "STRATEGY_LLM_TIMEOUT_SEC", 20.0)
        provider = _env_text("STRATEGY_EMBEDDING_PROVIDER", "STRATEGY_LLM_PROVIDER", "openai_compatible").lower()
        return cls(
            enabled=enabled_value,
            provider=provider or "openai_compatible",
            base_url=_env_text("STRATEGY_EMBEDDING_BASE_URL", "STRATEGY_LLM_BASE_URL"),
            api_key=_env_text("STRATEGY_EMBEDDING_API_KEY", "STRATEGY_LLM_API_KEY"),
            model=_env_text("STRATEGY_EMBEDDING_MODEL", "STRATEGY_LLM_EMBEDDING_MODEL", "text-embedding-3-small"),
            local_model_path=_env_text("STRATEGY_EMBEDDING_LOCAL_MODEL_PATH", default=""),
            timeout_sec=timeout_sec,
            connect_timeout_sec=_env_float("STRATEGY_EMBEDDING_CONNECT_TIMEOUT_SEC", "STRATEGY_LLM_CONNECT_TIMEOUT_SEC", min(timeout_sec, 8.0)),
            write_timeout_sec=_env_float("STRATEGY_EMBEDDING_WRITE_TIMEOUT_SEC", "STRATEGY_LLM_WRITE_TIMEOUT_SEC", min(timeout_sec, 10.0)),
            pool_timeout_sec=_env_float("STRATEGY_EMBEDDING_POOL_TIMEOUT_SEC", "STRATEGY_LLM_POOL_TIMEOUT_SEC", min(timeout_sec, 5.0)),
            retry_count=max(0, _env_int("STRATEGY_EMBEDDING_RETRY_COUNT", "STRATEGY_LLM_RETRY_COUNT", 2)),
            retry_backoff_sec=max(0.0, _env_float("STRATEGY_EMBEDDING_RETRY_BACKOFF_SEC", "STRATEGY_LLM_RETRY_BACKOFF_SEC", 0.75)),
            smoke_check_enabled=_env_bool("STRATEGY_EMBEDDING_SMOKE_CHECK_ENABLED", default=True),
            smoke_check_ttl_sec=max(0.0, _env_float("STRATEGY_EMBEDDING_SMOKE_CHECK_TTL_SEC", default=300.0)),
            max_text_chars=max(512, _env_int("STRATEGY_EMBEDDING_MAX_TEXT_CHARS", default=6000)),
            normalize_embeddings=_env_bool("STRATEGY_EMBEDDING_NORMALIZE", default=True),
            hash_dimensions=max(32, min(_env_int("STRATEGY_EMBEDDING_HASH_DIMENSIONS", default=256), 2048)),
            allow_hash_fallback=_env_bool("STRATEGY_EMBEDDING_ALLOW_HASH_FALLBACK", default=False),
        )


class StrategyTextEmbeddingService:
    def __init__(self, config: Optional[StrategyTextEmbeddingConfig] = None):
        self.config = config or StrategyTextEmbeddingConfig.from_env()
        self._cache: dict[str, list[float]] = {}
        self._cache_info: dict[str, dict[str, Any]] = {}
        self._client: httpx.AsyncClient | None = None
        self._sentence_transformer = None
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
        self._last_smoke_check_at: Optional[datetime] = None
        self._last_smoke_check_ok_at: Optional[datetime] = None
        self._last_smoke_check_error: Optional[str] = None

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(follow_redirects=True, http2=False)

    def is_closed(self) -> bool:
        client = self._client
        if client is None:
            return True
        try:
            return bool(getattr(client, "is_closed"))
        except Exception:
            return False

    def _client_state(self) -> str:
        client = self._client
        if client is None:
            return "idle"
        try:
            return "closed" if bool(getattr(client, "is_closed")) else "open"
        except Exception:
            return "unknown"

    def client_state(self) -> str:
        return self._client_state()

    async def ensure_client(self) -> None:
        if not self.is_closed():
            return
        self._client = self._build_client()
        self._rebuild_count += 1
        self._last_rebuild_at = datetime.now().astimezone()

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            self._sentence_transformer = None
            return
        try:
            await client.aclose()
        except Exception:
            pass
        self._sentence_transformer = None

    def is_enabled(self) -> bool:
        if self._has_ready_provider():
            return True
        return bool(self.config.allow_hash_fallback)

    def prefers_text_embedding_default(self) -> bool:
        return self._has_ready_provider()

    def _has_ready_provider(self) -> bool:
        if not self.config.enabled:
            return False
        provider = str(self.config.provider or "openai_compatible").strip().lower()
        if provider == "openai_compatible":
            return bool(self.config.base_url and self.config.api_key and self.config.model)
        if provider == "ollama":
            return bool(self.config.base_url and self.config.model)
        if provider == "sentence_transformers":
            return bool(self.config.local_model_path or self.config.model)
        if provider == "hash_fallback":
            return True
        return False

    def _resolve_provider(self) -> str:
        provider = str(self.config.provider or "openai_compatible").strip().lower()
        if self._has_ready_provider():
            return provider
        if self.config.allow_hash_fallback:
            # Surface that we are degrading to hash_fallback so operators
            # can spot the misconfiguration in logs even though we proceed.
            import logging
            logging.getLogger(__name__).warning(
                "embedding provider not ready (provider=%s); using hash_fallback (%dd). "
                "Set STRATEGY_EMBEDDING_ENABLED=1 plus provider credentials to enable "
                "real embeddings.",
                provider,
                self.config.hash_dimensions,
            )
            return "hash_fallback"
        raise StrategyTextEmbeddingError(
            "text embedding provider not configured: set STRATEGY_EMBEDDING_ENABLED=1 "
            "with STRATEGY_EMBEDDING_PROVIDER + STRATEGY_EMBEDDING_BASE_URL + "
            "STRATEGY_EMBEDDING_API_KEY (or run an offline ollama / "
            "sentence_transformers backend), or explicitly opt-in to hash fallback "
            "for offline development with STRATEGY_EMBEDDING_ALLOW_HASH_FALLBACK=1."
        )

    @staticmethod
    def _isoformat(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        try:
            observed = value if value.tzinfo is not None else value.astimezone()
        except Exception:
            observed = value
        return observed.isoformat()

    @staticmethod
    def _error_text(exc: Exception) -> str:
        text = str(exc or "").strip()
        return text or exc.__class__.__name__

    def _configured(self) -> bool:
        provider = str(self.config.provider or "openai_compatible").strip().lower()
        if provider == "openai_compatible":
            return bool(self.config.base_url and self.config.api_key and self.config.model)
        if provider == "ollama":
            return bool(self.config.base_url and self.config.model)
        if provider == "sentence_transformers":
            return bool(self.config.local_model_path or self.config.model)
        return provider == "hash_fallback"

    def _health_status(self) -> str:
        if not bool(self.config.enabled):
            return "disabled"
        if not self._configured():
            return "misconfigured"
        if self._client_state() == "closed":
            return "closed"
        if self._consecutive_failures > 0:
            return "degraded"
        return "ready"

    def _rebuild_recommended(self) -> bool:
        last_error_type = str(self._last_error_type or "").lower()
        client_state = self._client_state()
        return bool(
            client_state == "closed"
            or self._consecutive_failures > 0
            or last_error_type in {"httperror", "httpstatuserror", "connecterror", "readerror", "jsondecodeerror"}
        )

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

    def _mark_failure(self, exc: Exception, *, latency_ms: float) -> None:
        now = datetime.now().astimezone()
        self._request_count += 1
        self._consecutive_failures += 1
        self._last_request_at = now
        self._last_error_at = now
        self._last_latency_ms = round(float(latency_ms), 2)
        self._last_error_type = exc.__class__.__name__
        self._last_error = self._error_text(exc)

    def status(self) -> dict[str, Any]:
        return {
            "provider": str(self.config.provider or "openai_compatible"),
            "model": str(self.config.model or ""),
            "enabled": bool(self.config.enabled),
            "configured": self._configured(),
            "ready": bool(
                self.is_enabled()
                and self._client_state() != "closed"
                and self._consecutive_failures == 0
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
            "smoke_check_enabled": bool(self.config.smoke_check_enabled),
            "last_smoke_check_at": self._isoformat(self._last_smoke_check_at),
            "last_smoke_check_ok_at": self._isoformat(self._last_smoke_check_ok_at),
            "last_smoke_check_error": self._last_smoke_check_error,
        }

    def _timeout(self) -> httpx.Timeout:
        timeout_sec = max(float(self.config.timeout_sec or 20.0), 5.0)
        connect_timeout = max(1.0, min(float(self.config.connect_timeout_sec or timeout_sec), timeout_sec))
        write_timeout = max(1.0, min(float(self.config.write_timeout_sec or timeout_sec), timeout_sec))
        pool_timeout = max(1.0, min(float(self.config.pool_timeout_sec or timeout_sec), timeout_sec))
        return httpx.Timeout(connect=connect_timeout, read=timeout_sec, write=write_timeout, pool=pool_timeout)

    def _normalize_text(self, text: str) -> str:
        normalized = " ".join(str(text or "").strip().split())
        max_chars = max(256, int(self.config.max_text_chars or 6000))
        return normalized[:max_chars]

    def _normalize_vector(self, vector: list[float]) -> list[float]:
        if not vector:
            return []
        if not self.config.normalize_embeddings:
            return [float(item) for item in vector]
        norm = math.sqrt(sum(float(item) * float(item) for item in vector))
        if norm <= 1e-12:
            return [float(item) for item in vector]
        return [float(item) / norm for item in vector]

    def _endpoint_openai(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        return f"{base}/embeddings"

    def _endpoint_ollama(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/api/embed") or base.endswith("/api/embeddings"):
            return base
        return f"{base}/api/embed"

    @staticmethod
    def _status_code_from_error(exc: Exception) -> Optional[int]:
        try:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            return int(status_code) if status_code is not None else None
        except Exception:
            return None

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> Optional[float]:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw in (None, ""):
            return None
        try:
            return max(0.0, float(raw))
        except Exception:
            return None

    @staticmethod
    def _response_text(exc: Exception) -> str:
        try:
            response = getattr(exc, "response", None)
            if response is None:
                return ""
            return str(getattr(response, "text", "") or "")
        except Exception:
            return ""

    def _is_retryable_unknown_model_error(self, exc: Exception) -> bool:
        if self._status_code_from_error(exc) != 400:
            return False
        text = self._response_text(exc).lower()
        if "unknown model" not in text:
            return False
        return any(marker in text for marker in ("-global", "-data"))

    def _should_retry_request_error(self, exc: Exception) -> bool:
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
            return True
        status_code = self._status_code_from_error(exc)
        if status_code in {429, 502, 503, 504, 529}:
            return True
        return self._is_retryable_unknown_model_error(exc)

    def _retry_backoff_delay(self, attempt: int, exc: Exception, *, base_delay: float, max_delay: float = 10.0) -> float:
        retry_after = self._retry_after_seconds(exc)
        if retry_after is not None:
            return min(max(retry_after, base_delay), max_delay)
        multiplier = 2.0 if self._status_code_from_error(exc) in {429, 502, 503, 504, 529} else 1.0
        delay = max(0.0, float(base_delay or 0.0)) * max(1.0, multiplier * (2 ** max(int(attempt) - 1, 0)))
        return min(delay, max_delay)

    async def _embed_openai_compatible(self, normalized: str, *, model: str) -> list[float]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {"model": model, "input": normalized, "encoding_format": "float"}
        attempts = max(1, int(self.config.retry_count or 0) + 1)
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(self._endpoint_openai(), headers=headers, json=payload, timeout=self._timeout())
                response.raise_for_status()
                try:
                    body = response.json()
                except json.JSONDecodeError as exc:
                    raise StrategyTextEmbeddingError(f"embedding response invalid json: {exc}") from exc
                rows = list(body.get("data") or []) if isinstance(body, dict) else []
                if not rows:
                    raise StrategyTextEmbeddingError("embedding response missing data")
                embedding = rows[0].get("embedding") if isinstance(rows[0], dict) else None
                if not isinstance(embedding, list) or not embedding:
                    raise StrategyTextEmbeddingError("embedding response missing vector")
                return [float(item) for item in embedding]
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt >= attempts or not self._should_retry_request_error(exc):
                    raise
                await asyncio.sleep(
                    self._retry_backoff_delay(
                        attempt,
                        exc,
                        base_delay=float(self.config.retry_backoff_sec or 0.0),
                    )
                )
        if last_exc is not None:
            raise last_exc
        raise StrategyTextEmbeddingError("embedding request failed without response")

    async def _embed_ollama(self, normalized: str, *, model: str) -> list[float]:
        payload = {"model": model, "input": normalized}
        response = await self._client.post(self._endpoint_ollama(), json=payload, timeout=self._timeout())
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict):
            if isinstance(body.get("embeddings"), list) and body.get("embeddings"):
                first = body["embeddings"][0]
                if isinstance(first, list) and first:
                    return [float(item) for item in first]
            if isinstance(body.get("embedding"), list) and body.get("embedding"):
                return [float(item) for item in body.get("embedding")]
        raise StrategyTextEmbeddingError("ollama embedding response missing vector")

    def _load_sentence_transformer(self):
        if self._sentence_transformer is not None:
            return self._sentence_transformer
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            raise StrategyTextEmbeddingError(f"sentence_transformers unavailable: {exc}") from exc
        model_name = str(self.config.local_model_path or self.config.model or "").strip()
        if not model_name:
            raise StrategyTextEmbeddingError("sentence_transformers requires model/local_model_path")
        self._sentence_transformer = SentenceTransformer(model_name)
        return self._sentence_transformer

    async def _embed_sentence_transformers(self, normalized: str) -> list[float]:
        model = await asyncio.to_thread(self._load_sentence_transformer)
        vector = await asyncio.to_thread(
            model.encode,
            normalized,
            normalize_embeddings=False,
            convert_to_numpy=False,
            show_progress_bar=False,
        )
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        if not isinstance(vector, list) or not vector:
            raise StrategyTextEmbeddingError("sentence_transformers returned empty vector")
        return [float(item) for item in vector]

    def _embed_hash_fallback(self, normalized: str) -> list[float]:
        dims = max(32, int(self.config.hash_dimensions or 256))
        vector = [0.0] * dims
        text = f" {normalized} "
        grams = []
        for size in (2, 3, 4):
            grams.extend(text[idx: idx + size] for idx in range(max(len(text) - size + 1, 0)))
        if not grams:
            grams = [normalized]
        for gram in grams:
            digest = hashlib.sha1(gram.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:2], "big") % dims
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            vector[bucket] += sign
        return vector

    async def _embed_with_provider(self, provider: str, normalized: str, *, model: str) -> list[float]:
        if provider in {"openai_compatible", "ollama"}:
            await self.ensure_client()
        if provider == "openai_compatible":
            return await self._embed_openai_compatible(normalized, model=model)
        if provider == "ollama":
            return await self._embed_ollama(normalized, model=model)
        if provider == "sentence_transformers":
            return await self._embed_sentence_transformers(normalized)
        if provider == "hash_fallback":
            return self._embed_hash_fallback(normalized)
        raise StrategyTextEmbeddingError(f"unsupported embedding provider: {provider}")

    async def _embed_with_runtime_fallback(
        self,
        provider: str,
        normalized: str,
        *,
        model: str,
    ) -> tuple[list[float], str, Optional[str]]:
        try:
            return await self._embed_with_provider(provider, normalized, model=model), provider, None
        except Exception as exc:
            if provider != "hash_fallback" and self.config.allow_hash_fallback:
                return self._embed_hash_fallback(normalized), "hash_fallback", self._error_text(exc)
            raise

    async def smoke_check(self, *, force: bool = False) -> dict[str, Any]:
        if not self.is_enabled():
            return {"status": "disabled"}
        if not bool(self.config.smoke_check_enabled) and not force:
            return {"status": "disabled"}

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

        provider = self._resolve_provider()
        resolved_model = str(self.config.model or self.config.local_model_path or "").strip()
        normalized = self._normalize_text("strategy text embedding smoke check")
        started = time.perf_counter()
        try:
            values, used_provider, fallback_error = await self._embed_with_runtime_fallback(
                provider,
                normalized,
                model=resolved_model,
            )
            normalized_values = self._normalize_vector(values)
            latency_ms = (time.perf_counter() - started) * 1000
            self._mark_success(latency_ms=latency_ms)
            self._last_smoke_check_at = now
            self._last_smoke_check_ok_at = now
            self._last_smoke_check_error = None
            return {
                "status": "passed",
                "provider": used_provider,
                "requested_provider": provider,
                "model": resolved_model or None,
                "vector_length": len(normalized_values),
                "latency_ms": round(latency_ms, 2),
                "fallback_used": used_provider != provider,
                "fallback_error": fallback_error,
            }
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            self._mark_failure(exc, latency_ms=latency_ms)
            self._last_smoke_check_at = now
            self._last_smoke_check_error = self._error_text(exc)
            return {
                "status": "failed",
                "provider": provider,
                "model": resolved_model or None,
                "error_type": exc.__class__.__name__,
                "error": self._error_text(exc),
                "latency_ms": round(latency_ms, 2),
            }

    async def embed_text(self, text: str, *, model: Optional[str] = None) -> list[float]:
        normalized = self._normalize_text(text)
        if not normalized:
            raise StrategyTextEmbeddingError("embedding text is empty")
        provider = self._resolve_provider()
        resolved_model = str(model or self.config.model or self.config.local_model_path or "").strip()
        cache_key = hashlib.sha1(f"{provider}\n{resolved_model}\n{normalized}".encode("utf-8")).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return list(cached)
        started = time.perf_counter()
        try:
            values, _used_provider, _fallback_error = await self._embed_with_runtime_fallback(
                provider,
                normalized,
                model=resolved_model,
            )
            normalized_values = self._normalize_vector(values)
            self._cache[cache_key] = normalized_values
            self._mark_success(latency_ms=(time.perf_counter() - started) * 1000)
            return list(normalized_values)
        except Exception as exc:
            self._mark_failure(exc, latency_ms=(time.perf_counter() - started) * 1000)
            raise

    async def embed_text_with_info(self, text: str, *, model: Optional[str] = None) -> dict[str, Any]:
        normalized = self._normalize_text(text)
        if not normalized:
            raise StrategyTextEmbeddingError("embedding text is empty")
        provider = self._resolve_provider()
        resolved_model = str(model or self.config.model or self.config.local_model_path or "").strip()
        cache_key = hashlib.sha1(f"{provider}\n{resolved_model}\n{normalized}".encode("utf-8")).hexdigest()
        cached = self._cache.get(cache_key)
        cached_info = dict(self._cache_info.get(cache_key) or {})
        if cached is not None:
            return {
                "embedding": list(cached),
                "provider": str(cached_info.get("provider") or provider),
                "requested_provider": str(cached_info.get("requested_provider") or provider),
                "model": str(cached_info.get("model") or resolved_model or "") or None,
                "fallback_used": bool(cached_info.get("fallback_used")),
                "fallback_error": cached_info.get("fallback_error"),
                "cached": True,
                "vector_dim": int(cached_info.get("vector_dim") or len(cached)),
            }
        started = time.perf_counter()
        try:
            values, used_provider, fallback_error = await self._embed_with_runtime_fallback(
                provider,
                normalized,
                model=resolved_model,
            )
            normalized_values = self._normalize_vector(values)
            info = {
                "provider": used_provider,
                "requested_provider": provider,
                "model": resolved_model or None,
                "fallback_used": used_provider != provider,
                "fallback_error": fallback_error,
                "vector_dim": len(normalized_values),
            }
            self._cache[cache_key] = normalized_values
            self._cache_info[cache_key] = dict(info)
            self._mark_success(latency_ms=(time.perf_counter() - started) * 1000)
            return {
                "embedding": list(normalized_values),
                **info,
                "cached": False,
            }
        except Exception as exc:
            self._mark_failure(exc, latency_ms=(time.perf_counter() - started) * 1000)
            raise


_text_embedding_service: Optional[StrategyTextEmbeddingService] = None


def get_strategy_text_embedding_service() -> StrategyTextEmbeddingService:
    global _text_embedding_service
    if _text_embedding_service is None:
        _text_embedding_service = StrategyTextEmbeddingService()
    return _text_embedding_service


async def close_strategy_text_embedding_service() -> None:
    global _text_embedding_service
    service = _text_embedding_service
    _text_embedding_service = None
    if service is None:
        return
    await service.close()
