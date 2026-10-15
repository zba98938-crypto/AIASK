from __future__ import annotations

import importlib.util
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env_config import load_project_env, project_env_status
from .paths import aiask_agent_home
from .session_store_utils import redact_audit_text


SECRET_KEYS = {"api_key", "token", "password", "secret", "app_secret", "subscription_token"}
CONFIG_PATH_ENV = "AIASK_STOCK_DATA_SOURCES_FILE"
SEARCH_PROVIDERS = {"duckduckgo", "tavily", "brave_search", "serpapi", "exa"}


@dataclass(frozen=True)
class DataSourcePreset:
    provider: str
    label: str
    markets: tuple[str, ...]
    categories: tuple[str, ...]
    auth_type: str
    default_base_url: str | None = None
    default_host: str | None = None
    default_port: int | None = None
    required_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()
    documentation_url: str | None = None
    note: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "label": self.label,
            "markets": list(self.markets),
            "categories": list(self.categories),
            "auth_type": self.auth_type,
            "default_base_url": self.default_base_url,
            "default_host": self.default_host,
            "default_port": self.default_port,
            "required_fields": list(self.required_fields),
            "optional_fields": list(self.optional_fields),
            "env_keys": list(self.env_keys),
            "documentation_url": self.documentation_url,
            "note": self.note,
        }


PRESETS: tuple[DataSourcePreset, ...] = (
    DataSourcePreset(
        provider="akshare",
        label="AKShare / AKTools",
        markets=("CN", "HK", "US", "FX", "Futures", "Options"),
        categories=("quote", "kline", "fundamental", "macro", "news"),
        auth_type="none",
        default_base_url="",
        required_fields=(),
        optional_fields=("base_url", "timeout_seconds", "user_agent"),
        env_keys=("AKSHARE_MCP_SQLITE_PATH", "AIASK_SQLITE_PATH"),
        documentation_url="https://akshare.akfamily.xyz/introduction.html",
        note="Open-source Python data collector; optional AKTools HTTP base_url can be configured.",
    ),
    DataSourcePreset(
        provider="tushare",
        label="Tushare Pro",
        markets=("CN",),
        categories=("quote", "kline", "fundamental", "calendar", "finance"),
        auth_type="token",
        default_base_url="http://api.tushare.pro",
        required_fields=("api_key",),
        optional_fields=("base_url", "timeout_seconds", "rate_limit_per_minute", "fields"),
        env_keys=("TUSHARE_TOKEN",),
        documentation_url="https://tushare.pro/document/1?doc_id=40",
        note="HTTP API posts api_name, token, params and fields; some endpoints require points/permissions.",
    ),
    DataSourcePreset(
        provider="baostock",
        label="BaoStock",
        markets=("CN",),
        categories=("quote", "kline", "fundamental", "index"),
        auth_type="login",
        required_fields=(),
        optional_fields=("username", "password", "timeout_seconds"),
        env_keys=("BAOSTOCK_USER", "BAOSTOCK_PASSWORD"),
        documentation_url="https://baostock.com/baostock/index.php/Python_API%E6%96%87%E6%A1%A3",
        note="Python SDK login source; most public data works with anonymous login.",
    ),
    DataSourcePreset(
        provider="tdx",
        label="TongDaXin HQ",
        markets=("CN",),
        categories=("quote", "kline", "local_vipdoc"),
        auth_type="host_port",
        default_host="119.147.212.81",
        default_port=7709,
        required_fields=("host", "port"),
        optional_fields=("timeout_seconds", "local_vipdoc_path"),
        env_keys=("TDX_SERVER_IP", "TDX_SERVER_PORT", "TDX_LOCAL_ONLY", "TDX_VIPDOC_PATH"),
        documentation_url=None,
        note="TCP quote server or local vipdoc source; no API key for read-only market data.",
    ),
    DataSourcePreset(
        provider="eastmoney",
        label="Eastmoney public endpoints",
        markets=("CN",),
        categories=("quote", "notice", "research", "fund_flow"),
        auth_type="none",
        default_base_url="https://datacenter-web.eastmoney.com",
        required_fields=(),
        optional_fields=("base_url", "timeout_seconds", "user_agent"),
        env_keys=(),
        documentation_url=None,
        note="Public web endpoints used as read-only fallback; no official API key flow.",
    ),
    DataSourcePreset(
        provider="qmt",
        label="QMT / XtQuant local SDK",
        markets=("CN",),
        categories=("quote", "kline", "account_readonly"),
        auth_type="local_sdk",
        required_fields=("client_path",),
        optional_fields=("account_id", "session_id", "timeout_seconds"),
        env_keys=("QMT_PATH", "QMT_ACCOUNT_ID", "QMT_SESSION_ID"),
        documentation_url=None,
        note="Local MiniQMT/XtQuant SDK source; live trade actions remain separately guarded.",
    ),
    DataSourcePreset(
        provider="alpha_vantage",
        label="Alpha Vantage",
        markets=("US", "Global"),
        categories=("quote", "kline", "fundamental", "technical", "macro"),
        auth_type="api_key",
        default_base_url="https://www.alphavantage.co/query",
        required_fields=("api_key",),
        optional_fields=("base_url", "symbol", "timeout_seconds", "rate_limit_per_minute", "entitlement"),
        env_keys=("ALPHA_VANTAGE_API_KEY",),
        documentation_url="https://www.alphavantage.co/documentation/",
        note="Global equity API; apikey query parameter is required for stock endpoints.",
    ),
    DataSourcePreset(
        provider="finnhub",
        label="Finnhub",
        markets=("US", "Global"),
        categories=("quote", "kline", "fundamental", "news", "economic"),
        auth_type="token",
        default_base_url="https://finnhub.io/api/v1",
        required_fields=("api_key",),
        optional_fields=("base_url", "symbol", "timeout_seconds", "rate_limit_per_minute"),
        env_keys=("FINNHUB_API_KEY", "FINNHUB_TOKEN"),
        documentation_url="https://finnhub.io/docs/api/introduction",
        note="Token-authenticated REST API; quote and candle tests use a sample symbol.",
    ),
    DataSourcePreset(
        provider="twelve_data",
        label="Twelve Data",
        markets=("US", "Global", "FX", "Crypto"),
        categories=("quote", "kline", "technical"),
        auth_type="api_key",
        default_base_url="https://api.twelvedata.com",
        required_fields=("api_key",),
        optional_fields=("base_url", "symbol", "interval", "timeout_seconds", "rate_limit_per_minute"),
        env_keys=("TWELVE_DATA_API_KEY",),
        documentation_url="https://twelvedata.com/docs",
        note="REST time-series API; apikey query parameter is used for tests.",
    ),
    DataSourcePreset(
        provider="polygon",
        label="Polygon / Massive",
        markets=("US",),
        categories=("quote", "kline", "reference", "fundamental", "options"),
        auth_type="api_key",
        default_base_url="https://api.polygon.io",
        required_fields=("api_key",),
        optional_fields=("base_url", "symbol", "timeout_seconds", "rate_limit_per_minute", "auth_header"),
        env_keys=("POLYGON_API_KEY", "MASSIVE_API_KEY"),
        documentation_url="https://massive.com/docs/rest/quickstart",
        note="REST market data API; apiKey query parameter or Authorization header can be used.",
    ),
    DataSourcePreset(
        provider="nasdaq_data_link",
        label="Nasdaq Data Link",
        markets=("US", "Global"),
        categories=("fundamental", "alternative", "economic"),
        auth_type="api_key",
        default_base_url="https://data.nasdaq.com/api/v3",
        required_fields=("api_key",),
        optional_fields=("base_url", "dataset", "timeout_seconds", "rate_limit_per_minute"),
        env_keys=("NASDAQ_DATA_LINK_API_KEY", "QUANDL_API_KEY"),
        documentation_url="https://docs.data.nasdaq.com/",
        note="Dataset-oriented API, useful for fundamentals/economic/alternative data rather than tick-level quotes.",
    ),
    DataSourcePreset(
        provider="duckduckgo",
        label="DuckDuckGo HTML Search",
        markets=("Global",),
        categories=("web_search", "news", "research"),
        auth_type="none",
        default_base_url="https://duckduckgo.com/html/",
        required_fields=(),
        optional_fields=("base_url", "timeout_seconds", "rate_limit_per_minute"),
        env_keys=(),
        documentation_url="https://duckduckgo.com/",
        note="No-key public fallback used by agent_web_search when no configured search provider is available.",
    ),
    DataSourcePreset(
        provider="tavily",
        label="Tavily Search",
        markets=("Global",),
        categories=("web_search", "deep_research", "news", "research"),
        auth_type="bearer",
        default_base_url="https://api.tavily.com",
        required_fields=("api_key",),
        optional_fields=("base_url", "timeout_seconds", "rate_limit_per_minute", "search_depth", "include_answer"),
        env_keys=("TAVILY_API_KEY",),
        documentation_url="https://docs.tavily.com/documentation/api-reference/endpoint/search",
        note="Deep web search API; tests call /search with Authorization: Bearer.",
    ),
    DataSourcePreset(
        provider="brave_search",
        label="Brave Search API",
        markets=("Global",),
        categories=("web_search", "news", "research"),
        auth_type="subscription_token",
        default_base_url="https://api.search.brave.com/res/v1",
        required_fields=("api_key",),
        optional_fields=("base_url", "timeout_seconds", "rate_limit_per_minute", "country", "search_lang"),
        env_keys=("BRAVE_SEARCH_API_KEY", "BRAVE_SEARCH_SUBSCRIPTION_TOKEN"),
        documentation_url="https://api-dashboard.search.brave.com/app/documentation/web-search/get-started",
        note="Web search API using X-Subscription-Token.",
    ),
    DataSourcePreset(
        provider="serpapi",
        label="SerpApi",
        markets=("Global",),
        categories=("web_search", "search_engine_results", "news", "research"),
        auth_type="api_key",
        default_base_url="https://serpapi.com/search.json",
        required_fields=("api_key",),
        optional_fields=("base_url", "timeout_seconds", "rate_limit_per_minute", "engine", "location", "google_domain"),
        env_keys=("SERPAPI_API_KEY",),
        documentation_url="https://serpapi.com/search-api",
        note="Search engine results API; tests use engine=google with api_key query parameter.",
    ),
    DataSourcePreset(
        provider="exa",
        label="Exa Search",
        markets=("Global",),
        categories=("web_search", "neural_search", "research"),
        auth_type="api_key_header",
        default_base_url="https://api.exa.ai",
        required_fields=("api_key",),
        optional_fields=("base_url", "timeout_seconds", "rate_limit_per_minute", "search_type", "include_text"),
        env_keys=("EXA_API_KEY",),
        documentation_url="https://docs.exa.ai/reference/search",
        note="Neural/similarity search API using x-api-key.",
    ),
)

PRESET_BY_PROVIDER = {item.provider: item for item in PRESETS}


def config_path() -> Path:
    raw = str(os.getenv(CONFIG_PATH_ENV, "")).strip()
    if raw:
        return Path(raw).expanduser()
    return aiask_agent_home() / "stock_data_sources.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_store() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {"object": "aiask.stock_data_sources.store", "sources": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"object": "aiask.stock_data_sources.store", "sources": []}
    if not isinstance(payload, dict):
        return {"object": "aiask.stock_data_sources.store", "sources": []}
    sources = [dict(item) for item in list(payload.get("sources") or []) if isinstance(item, dict)]
    return {"object": "aiask.stock_data_sources.store", "sources": sources}


def _write_store(payload: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _first_env(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name, "")).strip()
        if value:
            return value
    return ""


def _secret_configured(source: dict[str, Any], *names: str) -> bool:
    for name in names:
        if str(source.get(name) or "").strip():
            return True
    return False


def _redact_source(source: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in source.items():
        lowered = str(key).lower()
        if lowered in SECRET_KEYS or any(token in lowered for token in ("api_key", "token", "secret", "password")):
            redacted[key] = "[redacted]" if str(value or "").strip() else ""
        else:
            redacted[key] = value
    redacted["api_key_configured"] = _secret_configured(source, "api_key", "token", "password", "secret")
    redacted["secrets_redacted"] = True
    return redacted


def _source_id(provider: str, name: str | None = None) -> str:
    raw = str(name or provider or "source").strip().lower()
    safe = "".join(char if char.isalnum() else "_" for char in raw).strip("_") or "source"
    return f"stock_ds_{safe}_{_now_ms()}"


def _env_source_for_preset(preset: DataSourcePreset) -> dict[str, Any] | None:
    provider = preset.provider
    if provider == "tushare":
        token = _first_env("TUSHARE_TOKEN")
        if not token:
            return None
        return {"id": "env:tushare", "provider": provider, "name": "Tushare env", "base_url": preset.default_base_url, "api_key": token, "enabled": True, "source": "env"}
    if provider == "alpha_vantage":
        key = _first_env("ALPHA_VANTAGE_API_KEY")
        if not key:
            return None
        return {"id": "env:alpha_vantage", "provider": provider, "name": "Alpha Vantage env", "base_url": preset.default_base_url, "api_key": key, "symbol": "IBM", "enabled": True, "source": "env"}
    if provider == "finnhub":
        key = _first_env("FINNHUB_API_KEY", "FINNHUB_TOKEN")
        if not key:
            return None
        return {"id": "env:finnhub", "provider": provider, "name": "Finnhub env", "base_url": preset.default_base_url, "api_key": key, "symbol": "AAPL", "enabled": True, "source": "env"}
    if provider == "twelve_data":
        key = _first_env("TWELVE_DATA_API_KEY")
        if not key:
            return None
        return {"id": "env:twelve_data", "provider": provider, "name": "Twelve Data env", "base_url": preset.default_base_url, "api_key": key, "symbol": "AAPL", "enabled": True, "source": "env"}
    if provider == "polygon":
        key = _first_env("POLYGON_API_KEY", "MASSIVE_API_KEY")
        if not key:
            return None
        return {"id": "env:polygon", "provider": provider, "name": "Polygon env", "base_url": preset.default_base_url, "api_key": key, "symbol": "AAPL", "enabled": True, "source": "env"}
    if provider == "nasdaq_data_link":
        key = _first_env("NASDAQ_DATA_LINK_API_KEY", "QUANDL_API_KEY")
        if not key:
            return None
        return {"id": "env:nasdaq_data_link", "provider": provider, "name": "Nasdaq Data Link env", "base_url": preset.default_base_url, "api_key": key, "enabled": True, "source": "env"}
    if provider == "tavily":
        key = _first_env("TAVILY_API_KEY")
        if not key:
            return None
        return {"id": "env:tavily", "provider": provider, "name": "Tavily env", "base_url": preset.default_base_url, "api_key": key, "enabled": True, "source": "env"}
    if provider == "brave_search":
        key = _first_env("BRAVE_SEARCH_API_KEY", "BRAVE_SEARCH_SUBSCRIPTION_TOKEN")
        if not key:
            return None
        return {"id": "env:brave_search", "provider": provider, "name": "Brave Search env", "base_url": preset.default_base_url, "api_key": key, "enabled": True, "source": "env"}
    if provider == "serpapi":
        key = _first_env("SERPAPI_API_KEY")
        if not key:
            return None
        return {"id": "env:serpapi", "provider": provider, "name": "SerpApi env", "base_url": preset.default_base_url, "api_key": key, "enabled": True, "source": "env"}
    if provider == "exa":
        key = _first_env("EXA_API_KEY")
        if not key:
            return None
        return {"id": "env:exa", "provider": provider, "name": "Exa env", "base_url": preset.default_base_url, "api_key": key, "enabled": True, "source": "env"}
    if provider == "tdx":
        host = _first_env("TDX_SERVER_IP")
        if not host:
            return None
        port = int(_first_env("TDX_SERVER_PORT") or preset.default_port or 7709)
        return {"id": "env:tdx", "provider": provider, "name": "TDX env", "host": host, "port": port, "enabled": True, "source": "env"}
    if provider == "qmt":
        path = _first_env("QMT_PATH")
        if not path:
            return None
        return {"id": "env:qmt", "provider": provider, "name": "QMT env", "client_path": path, "account_id": _first_env("QMT_ACCOUNT_ID"), "session_id": _first_env("QMT_SESSION_ID"), "enabled": True, "source": "env"}
    return None


def _source_configured(source: dict[str, Any], preset: DataSourcePreset) -> bool:
    for field in preset.required_fields:
        if field == "api_key":
            if not _secret_configured(source, "api_key", "token"):
                return False
        elif field == "port":
            if int(source.get("port") or 0) <= 0:
                return False
        elif not str(source.get(field) or "").strip():
            return False
    return True


def _source_status(source: dict[str, Any]) -> dict[str, Any]:
    provider = str(source.get("provider") or "").strip()
    preset = PRESET_BY_PROVIDER.get(provider)
    configured = _source_configured(source, preset) if preset else False
    enabled = bool(source.get("enabled", True))
    status = "ready" if configured and enabled else "disabled" if not enabled else "unconfigured"
    if provider in {"akshare", "baostock", "qmt"}:
        package = "akshare" if provider == "akshare" else "baostock" if provider == "baostock" else "xtquant"
        installed = importlib.util.find_spec(package) is not None
        if provider != "qmt":
            configured = configured or installed
            status = "ready" if installed and enabled else "missing_dependency"
        elif configured and not installed:
            status = "missing_dependency"
    return {"configured": bool(configured), "enabled": enabled, "status": status}


def list_stock_data_sources() -> dict[str, Any]:
    load_project_env()
    stored = _read_store()
    sources = [dict(item) for item in list(stored.get("sources") or [])]
    for preset in PRESETS:
        env_source = _env_source_for_preset(preset)
        if env_source and not any(item.get("id") == env_source["id"] for item in sources):
            sources.append(env_source)
    public_sources = []
    configured_count = 0
    ready_count = 0
    for source in sources:
        preset = PRESET_BY_PROVIDER.get(str(source.get("provider") or ""))
        status = _source_status(source)
        configured_count += 1 if status["configured"] else 0
        ready_count += 1 if status["status"] == "ready" else 0
        item = _redact_source(source)
        item.update(status)
        item["label"] = preset.label if preset else str(source.get("provider") or "unknown")
        item["auth_type"] = preset.auth_type if preset else "custom"
        item["required_fields"] = list(preset.required_fields) if preset else []
        item["optional_fields"] = list(preset.optional_fields) if preset else []
        public_sources.append(item)
    return {
        "object": "aiask.stock_data_sources",
        "status": "ready" if ready_count else "unconfigured",
        "configured_count": configured_count,
        "ready_count": ready_count,
        "presets": [item.public() for item in PRESETS],
        "sources": public_sources,
        "config_path": str(config_path()),
        "config_source": project_env_status(),
        "secrets_redacted": True,
    }


def save_stock_data_source(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    load_project_env()
    body = dict(payload or {})
    provider = str(body.get("provider") or "").strip()
    if provider not in PRESET_BY_PROVIDER:
        raise ValueError(f"unsupported stock data source provider: {provider}")
    store = _read_store()
    sources = [dict(item) for item in list(store.get("sources") or []) if isinstance(item, dict) and not str(item.get("id") or "").startswith("env:")]
    source_id = str(body.get("id") or "").strip()
    existing = next((item for item in sources if str(item.get("id") or "") == source_id), None) if source_id else None
    if not source_id:
        source_id = _source_id(provider, str(body.get("name") or provider))
    merged = dict(existing or {})
    allowed_keys = {
        "name",
        "provider",
        "enabled",
        "priority",
        "base_url",
        "api_key",
        "token",
        "host",
        "port",
        "username",
        "password",
        "client_path",
        "account_id",
        "session_id",
        "symbol",
        "interval",
        "dataset",
        "timeout_seconds",
        "rate_limit_per_minute",
        "markets",
        "notes",
        "extra",
        "search_depth",
        "include_answer",
        "country",
        "search_lang",
        "engine",
        "location",
        "google_domain",
        "search_type",
        "include_text",
    }
    for key in allowed_keys:
        if key not in body:
            continue
        value = body.get(key)
        if key in SECRET_KEYS and value in (None, "") and existing:
            continue
        merged[key] = value
    merged["id"] = source_id
    merged["provider"] = provider
    merged["name"] = str(merged.get("name") or PRESET_BY_PROVIDER[provider].label)
    merged["enabled"] = bool(merged.get("enabled", True))
    merged["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not existing:
        merged["created_at"] = merged["updated_at"]
        sources.append(merged)
    else:
        for index, item in enumerate(sources):
            if str(item.get("id") or "") == source_id:
                sources[index] = merged
                break
    _write_store({"object": "aiask.stock_data_sources.store", "sources": sources})
    public = _redact_source(merged)
    public.update(_source_status(merged))
    return {"object": "aiask.stock_data_source", "source": public, "secrets_redacted": True}


def _merge_inline_source(base: dict[str, Any] | None, inline: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in inline.items():
        lowered = str(key).lower()
        is_secret = lowered in SECRET_KEYS or any(token in lowered for token in ("api_key", "token", "secret", "password"))
        if is_secret and value in (None, "") and base:
            continue
        merged[key] = value
    return merged


def _find_source(source_id: str | None, provider: str | None, inline: dict[str, Any] | None = None) -> dict[str, Any]:
    load_project_env()
    body = dict(inline or {})
    effective_source_id = source_id or str(body.get("id") or "").strip() or None
    effective_provider = provider or str(body.get("provider") or "").strip() or None
    sources = [dict(item) for item in list(_read_store().get("sources") or [])]
    for preset in PRESETS:
        env_source = _env_source_for_preset(preset)
        if env_source:
            sources.append(env_source)
    if body:
        stored = None
        if effective_source_id:
            stored = next((item for item in sources if str(item.get("id") or "") == effective_source_id), None)
        if stored:
            return _merge_inline_source(stored, body)
        if body.get("provider"):
            return body
    for item in sources:
        if effective_source_id and str(item.get("id") or "") == effective_source_id:
            return item
        if effective_provider and str(item.get("provider") or "") == effective_provider:
            return item
    if effective_provider in PRESET_BY_PROVIDER:
        preset = PRESET_BY_PROVIDER[effective_provider]
        return {"provider": effective_provider, "name": preset.label, "base_url": preset.default_base_url, "host": preset.default_host, "port": preset.default_port, **body}
    return body


def _timeout(source: dict[str, Any]) -> float:
    try:
        return max(1.0, min(float(source.get("timeout_seconds") or 8), 60.0))
    except Exception:
        return 8.0


def _http_json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float = 8.0) -> tuple[int, Any]:
    data = None
    request_headers = {"User-Agent": "AIASK-StockDataSource-Test/1.0", **dict(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - user-configured stock data endpoints are intentionally tested.
        raw = response.read(2_000_000).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw_preview": raw[:500]}
        return int(response.status), payload


def _query_from_payload(source: dict[str, Any]) -> str:
    return str(source.get("query") or source.get("q") or "AIASK data source smoke test").strip()


def _results_count(data: Any, *paths: str) -> int:
    if not isinstance(data, dict):
        return 0
    for path in paths:
        current: Any = data
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = None
                break
        if isinstance(current, list):
            return len(current)
    return 0


def _test_network_source(source: dict[str, Any], mode: str) -> dict[str, Any]:
    provider = str(source.get("provider") or "").strip()
    preset = PRESET_BY_PROVIDER[provider]
    base_url = str(source.get("base_url") or preset.default_base_url or "").strip().rstrip("/")
    api_key = str(source.get("api_key") or source.get("token") or "").strip()
    symbol = str(source.get("symbol") or ("IBM" if provider == "alpha_vantage" else "AAPL")).strip()
    timeout = _timeout(source)
    if "api_key" in preset.required_fields and not api_key:
        return {"success": False, "status": "unconfigured", "error_code": "API_KEY_REQUIRED", "error": "API key or token is required.", "configured": False}
    if provider == "tushare":
        payload = {"api_name": "trade_cal", "token": api_key, "params": {"exchange": "", "start_date": "20260101", "end_date": "20260110"}, "fields": "exchange,cal_date,is_open"}
        status, data = _http_json(base_url or "http://api.tushare.pro", method="POST", body=payload, timeout=timeout)
        code = int(data.get("code") or 0) if isinstance(data, dict) and str(data.get("code") or "").lstrip("-").isdigit() else None
        rows = list(((data.get("data") or {}).get("items") or []) if isinstance(data, dict) else [])
        ok = status < 400 and code in {0, None} and (mode == "connectivity" or bool(rows))
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": len(rows), "provider_code": code, "error": None if ok else str(data.get("msg") if isinstance(data, dict) else "request failed")}
    if provider == "alpha_vantage":
        query = urllib.parse.urlencode({"function": "TIME_SERIES_DAILY", "symbol": symbol, "apikey": api_key, "outputsize": "compact"})
        status, data = _http_json(f"{base_url or 'https://www.alphavantage.co/query'}?{query}", timeout=timeout)
        ok = status < 400 and isinstance(data, dict) and not any(key in data for key in ("Error Message", "Information", "Note")) and (mode == "connectivity" or "Time Series (Daily)" in data)
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "symbol": symbol, "sample_count": len(data.get("Time Series (Daily)", {})) if isinstance(data, dict) else 0, "error": None if ok else str((data or {}).get("Error Message") or (data or {}).get("Information") or (data or {}).get("Note") or "request failed")}
    if provider == "finnhub":
        query = urllib.parse.urlencode({"symbol": symbol, "token": api_key})
        status, data = _http_json(f"{base_url or 'https://finnhub.io/api/v1'}/quote?{query}", timeout=timeout)
        ok = status < 400 and isinstance(data, dict) and any(key in data for key in ("c", "pc", "t"))
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "symbol": symbol, "sample_count": 1 if ok else 0, "error": None if ok else str((data or {}).get("error") or "request failed")}
    if provider == "twelve_data":
        query = urllib.parse.urlencode({"symbol": symbol, "interval": str(source.get("interval") or "1day"), "outputsize": "1", "apikey": api_key})
        status, data = _http_json(f"{base_url or 'https://api.twelvedata.com'}/time_series?{query}", timeout=timeout)
        values = list(data.get("values") or []) if isinstance(data, dict) else []
        ok = status < 400 and (mode == "connectivity" or bool(values)) and str((data or {}).get("status") or "ok") != "error"
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "symbol": symbol, "sample_count": len(values), "error": None if ok else str((data or {}).get("message") or "request failed")}
    if provider == "polygon":
        query = urllib.parse.urlencode({"active": "true", "limit": "1", "apiKey": api_key})
        status, data = _http_json(f"{base_url or 'https://api.polygon.io'}/v3/reference/tickers?{query}", timeout=timeout)
        results = list(data.get("results") or []) if isinstance(data, dict) else []
        ok = status < 400 and str((data or {}).get("status") or "").upper() not in {"ERROR", "NOT_AUTHORIZED"} and (mode == "connectivity" or bool(results))
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": len(results), "error": None if ok else str((data or {}).get("error") or "request failed")}
    if provider == "nasdaq_data_link":
        query = urllib.parse.urlencode({"api_key": api_key})
        status, data = _http_json(f"{base_url or 'https://data.nasdaq.com/api/v3'}/databases?{query}", timeout=timeout)
        databases = list(data.get("databases") or []) if isinstance(data, dict) else []
        ok = status < 400 and (mode == "connectivity" or bool(databases))
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": len(databases), "error": None if ok else str((data or {}).get("quandl_error") or "request failed")}
    if provider == "duckduckgo":
        query = urllib.parse.urlencode({"q": _query_from_payload(source)})
        status, data = _http_json(f"{base_url or 'https://duckduckgo.com/html/'}?{query}", timeout=timeout)
        ok = status < 500
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": 1 if ok else 0, "error": None if ok else "request failed", "preview": data.get("raw_preview") if isinstance(data, dict) else None}
    if provider == "tavily":
        payload = {
            "query": _query_from_payload(source),
            "max_results": 1 if mode == "connectivity" else 3,
            "search_depth": str(source.get("search_depth") or "basic"),
            "include_answer": bool(source.get("include_answer", False)),
        }
        status, data = _http_json(f"{base_url or 'https://api.tavily.com'}/search", method="POST", body=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        count = _results_count(data, "results")
        ok = status < 400 and (mode == "connectivity" or count > 0)
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": count, "error": None if ok else str((data or {}).get("error") or "request failed")}
    if provider == "brave_search":
        query = urllib.parse.urlencode({"q": _query_from_payload(source), "count": "1" if mode == "connectivity" else "3"})
        status, data = _http_json(f"{base_url or 'https://api.search.brave.com/res/v1'}/web/search?{query}", headers={"Accept": "application/json", "X-Subscription-Token": api_key}, timeout=timeout)
        count = _results_count(data, "web.results", "results")
        ok = status < 400 and (mode == "connectivity" or count > 0)
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": count, "error": None if ok else str((data or {}).get("error") or "request failed")}
    if provider == "serpapi":
        query = urllib.parse.urlencode({"engine": str(source.get("engine") or "google"), "q": _query_from_payload(source), "api_key": api_key, "num": "3"})
        status, data = _http_json(f"{base_url or 'https://serpapi.com/search.json'}?{query}", timeout=timeout)
        count = _results_count(data, "organic_results", "news_results")
        ok = status < 400 and not (isinstance(data, dict) and data.get("error")) and (mode == "connectivity" or count > 0)
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": count, "error": None if ok else str((data or {}).get("error") or "request failed")}
    if provider == "exa":
        payload = {"query": _query_from_payload(source), "numResults": 1 if mode == "connectivity" else 3, "type": str(source.get("search_type") or "auto")}
        status, data = _http_json(f"{base_url or 'https://api.exa.ai'}/search", method="POST", body=payload, headers={"x-api-key": api_key}, timeout=timeout)
        count = _results_count(data, "results")
        ok = status < 400 and (mode == "connectivity" or count > 0)
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": count, "error": None if ok else str((data or {}).get("error") or "request failed")}
    if provider == "eastmoney":
        url = base_url or "https://datacenter-web.eastmoney.com"
        status, data = _http_json(url, timeout=timeout)
        ok = status < 500
        return {"success": ok, "status": "ready" if ok else "failed", "http_status": status, "sample_count": 1 if ok else 0, "error": None if ok else "request failed", "preview": data.get("raw_preview") if isinstance(data, dict) else None}
    return {"success": False, "status": "unsupported", "error_code": "UNSUPPORTED_TEST", "error": f"unsupported provider test: {provider}"}


def _test_local_source(source: dict[str, Any], mode: str) -> dict[str, Any]:
    provider = str(source.get("provider") or "").strip()
    if provider == "akshare":
        # "akshare" here names the AIASK AKShare MCP server this project ships, not
        # the upstream akshare Python package. That package was never a declared
        # dependency and is not installed; probing for it reported a data source
        # as ready when what was being checked did not exist.
        mcp_installed = importlib.util.find_spec("akshare_mcp") is not None
        return {
            "success": mcp_installed,
            "status": "ready" if mcp_installed else "missing_dependency",
            "sample_count": 1 if mcp_installed else 0,
            "dependency": "akshare_mcp" if mcp_installed else None,
            "error": None if mcp_installed else "Install the akshare-mcp package (part of this project).",
        }
    if provider == "baostock":
        installed = importlib.util.find_spec("baostock") is not None
        return {"success": installed, "status": "ready" if installed else "missing_dependency", "sample_count": 1 if installed and mode == "connectivity" else 0, "dependency": "baostock" if installed else None, "error": None if installed else "Install baostock Python package."}
    if provider == "qmt":
        installed = importlib.util.find_spec("xtquant") is not None
        client_path = str(source.get("client_path") or _first_env("QMT_PATH") or "").strip()
        path_ok = bool(client_path and Path(client_path).expanduser().exists())
        ok = installed and path_ok
        return {"success": ok, "status": "ready" if ok else "missing_dependency" if not installed else "unconfigured", "sample_count": 1 if ok else 0, "dependency": "xtquant" if installed else None, "client_path_configured": bool(client_path), "client_path_exists": path_ok, "error": None if ok else "Install xtquant and configure an existing QMT_PATH/client_path."}
    if provider == "tdx":
        host = str(source.get("host") or PRESET_BY_PROVIDER["tdx"].default_host or "").strip()
        port = int(source.get("port") or PRESET_BY_PROVIDER["tdx"].default_port or 7709)
        timeout = _timeout(source)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {"success": True, "status": "ready", "host": host, "port": port, "sample_count": 1}
        except Exception as exc:
            return {
                "success": False,
                "status": "failed",
                "host": host,
                "port": port,
                "sample_count": 0,
                "error_code": "TCP_CONNECT_FAILED",
                "error": redact_audit_text(exc),
            }
    return {"success": False, "status": "unsupported", "error_code": "UNSUPPORTED_TEST", "error": f"unsupported local provider test: {provider}"}


def test_stock_data_source(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = dict(payload or {})
    mode = str(body.get("mode") or "connectivity").strip().lower()
    if mode not in {"connectivity", "sample"}:
        mode = "connectivity"
    inline_source = body.get("source") if isinstance(body.get("source"), dict) else None
    if inline_source is None and any(key in body for key in ("api_key", "token", "password", "base_url", "host", "port", "client_path")):
        inline_source = body
    source = _find_source(str(body.get("id") or "").strip() or None, str(body.get("provider") or "").strip() or None, inline_source)
    provider = str(source.get("provider") or body.get("provider") or "").strip()
    if provider not in PRESET_BY_PROVIDER:
        return {"object": "aiask.stock_data_source_test", "success": False, "status": "unsupported", "provider": provider, "error_code": "UNSUPPORTED_PROVIDER", "error": f"unsupported provider: {provider}", "secrets_redacted": True}
    source["provider"] = provider
    started = time.perf_counter()
    try:
        if provider in {"akshare", "baostock", "qmt", "tdx"}:
            result = _test_local_source(source, mode)
        else:
            result = _test_network_source(source, mode)
    except urllib.error.HTTPError as exc:
        result = {"success": False, "status": "failed", "http_status": exc.code, "error_code": "HTTP_ERROR", "error": redact_audit_text(exc)}
    except urllib.error.URLError as exc:
        result = {"success": False, "status": "failed", "error_code": "NETWORK_ERROR", "error": redact_audit_text(exc.reason)}
    except Exception as exc:
        result = {"success": False, "status": "failed", "error_code": exc.__class__.__name__, "error": redact_audit_text(exc)}
    if result.get("error") is not None:
        result["error"] = redact_audit_text(result.get("error"))
    if isinstance(result.get("preview"), str):
        result["preview"] = redact_audit_text(result.get("preview"))
    result.update(
        {
            "object": "aiask.stock_data_source_test",
            "provider": provider,
            "mode": mode,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "configured": _source_configured(source, PRESET_BY_PROVIDER[provider]),
            "source": _redact_source(source),
            "secrets_redacted": True,
        }
    )
    return result


def enabled_search_sources() -> list[dict[str, Any]]:
    load_project_env()
    sources = [dict(item) for item in list(_read_store().get("sources") or [])]
    for preset in PRESETS:
        env_source = _env_source_for_preset(preset)
        if env_source:
            sources.append(env_source)
    configured = []
    for source in sources:
        provider = str(source.get("provider") or "").strip()
        if provider not in SEARCH_PROVIDERS:
            continue
        if not bool(source.get("enabled", True)):
            continue
        preset = PRESET_BY_PROVIDER.get(provider)
        if preset and _source_configured(source, preset):
            configured.append(source)
    configured.sort(key=lambda item: int(item.get("priority") or 100))
    if not configured:
        configured.append({"id": "builtin:duckduckgo", "provider": "duckduckgo", "name": "DuckDuckGo fallback", "base_url": PRESET_BY_PROVIDER["duckduckgo"].default_base_url, "enabled": True, "source": "builtin"})
    return configured


def search_source_by_provider(provider: str | None = None) -> dict[str, Any] | None:
    requested = str(provider or "").strip()
    sources = enabled_search_sources()
    if requested:
        for source in sources:
            if str(source.get("provider") or "") == requested or str(source.get("id") or "") == requested:
                return source
    return sources[0] if sources else None
