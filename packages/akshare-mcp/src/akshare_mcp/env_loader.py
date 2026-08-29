"""MCP 服务统一 .env 加载器。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


LLM_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "FINANCIAL_SEMANTIC_API_KEY",
    "FINANCIAL_SEMANTIC_BASE_URL",
    "FINANCIAL_SEMANTIC_CONNECT_TIMEOUT_SEC",
    "FINANCIAL_SEMANTIC_ENABLED",
    "FINANCIAL_SEMANTIC_MAX_DOCS",
    "FINANCIAL_SEMANTIC_MAX_TEXT_CHARS",
    "FINANCIAL_SEMANTIC_MODEL",
    "FINANCIAL_SEMANTIC_POOL_TIMEOUT_SEC",
    "FINANCIAL_SEMANTIC_PROVIDER",
    "FINANCIAL_SEMANTIC_TEMPERATURE",
    "FINANCIAL_SEMANTIC_TIMEOUT_SEC",
    "FINANCIAL_SEMANTIC_WRITE_TIMEOUT_SEC",
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "OPENAI_BASE_URL",
}

LLM_ENV_PREFIXES = (
    "ANTHROPIC_",
    "DASHSCOPE_",
    "DEEPSEEK_",
    "FACTOR_LLM_",
    "FINANCIAL_SEMANTIC_",
    "MOONSHOT_",
    "OPENAI_",
    "QWEN_",
    "STRATEGY_FACTORY_LLM_",
    "STRATEGY_EMBEDDING_",
    "STRATEGY_LLM_",
    "STRATEGY_PIPELINE_STAGE_",
    "ZHIPU_",
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _llm_env_file_priority_disabled() -> bool:
    explicit = str(os.getenv("AIASK_LLM_ENV_FILE_PRIORITY", "")).strip().lower()
    if explicit in {"0", "false", "no", "off"}:
        return True
    return _truthy(os.getenv("AIASK_DISABLE_LLM_ENV_FILE_PRIORITY"))


def is_llm_env_key(key: str) -> bool:
    name = str(key or "").strip()
    if not name:
        return False
    return name in LLM_ENV_KEYS or any(name.startswith(prefix) for prefix in LLM_ENV_PREFIXES)


def get_mcp_env_candidates(explicit_path: Optional[str] = None) -> list[Path]:
    env_from_var = explicit_path or os.getenv('AKSHARE_MCP_ENV', '').strip()
    candidates = [
        Path.cwd() / '.env',
        Path(__file__).resolve().parents[4] / '.env',  # 项目根目录
    ]
    if env_from_var:
        candidates.insert(0, Path(env_from_var))
    seen: set[str] = set()
    result: list[Path] = []
    for item in candidates:
        try:
            key = str(item.resolve())
        except Exception:
            key = str(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _parse_env_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in str(text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values


def load_mcp_env(
    *,
    explicit_path: Optional[str] = None,
    override: bool = False,
    only_keys: Optional[Iterable[str]] = None,
    only_prefixes: Optional[Iterable[str]] = None,
) -> Optional[Path]:
    key_filter = set(str(item).strip() for item in (only_keys or []) if str(item).strip())
    prefix_filter = tuple(str(item).strip() for item in (only_prefixes or []) if str(item).strip())
    for env_path in get_mcp_env_candidates(explicit_path=explicit_path):
        if not env_path.exists() or not env_path.is_file():
            continue
        try:
            content = env_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        values = _parse_env_lines(content)
        for key, value in values.items():
            if key_filter and key not in key_filter:
                continue
            if prefix_filter and not any(key.startswith(prefix) for prefix in prefix_filter):
                continue
            if override or (is_llm_env_key(key) and not _llm_env_file_priority_disabled()):
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)
        return env_path
    return None


def load_mcp_llm_env(
    *,
    explicit_path: Optional[str] = None,
    only_keys: Optional[Iterable[str]] = None,
    only_prefixes: Optional[Iterable[str]] = None,
) -> Optional[Path]:
    """Load LLM-related .env values with file priority over process env."""
    return load_mcp_env(
        explicit_path=explicit_path,
        override=True,
        only_keys=only_keys,
        only_prefixes=only_prefixes,
    )
