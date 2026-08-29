from __future__ import annotations

import os
from pathlib import Path


LLM_ENV_KEYS = {
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "OPENAI_BASE_URL",
}

LLM_ENV_PREFIXES = (
    "AI_VALIDATION_",
    "ANTHROPIC_",
    "DASHSCOPE_",
    "DEEPSEEK_",
    "FACTOR_LLM_",
    "FINANCIAL_SEMANTIC_",
    "MOONSHOT_",
    "OPENAI_",
    "QWEN_",
    "STRATEGY_FACTORY_AI_VALIDATION_",
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


def _is_llm_env_key(key: str) -> bool:
    name = str(key or "").strip()
    return bool(name and (name in LLM_ENV_KEYS or any(name.startswith(prefix) for prefix in LLM_ENV_PREFIXES)))


def _project_root_candidates(explicit_path: str | None = None) -> list[Path]:
    configured = explicit_path or os.getenv("STRATEGY_FACTORY_ENV_FILE", "").strip() or os.getenv("AIASK_ENV_FILE", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    for start in (Path.cwd(), Path(__file__).resolve()):
        path = start if start.is_dir() else start.parent
        for candidate in (path, *path.parents):
            env_path = candidate / ".env"
            if env_path.exists() and (candidate / "packages").is_dir():
                candidates.append(env_path)
                break
    candidates.append(Path.cwd() / ".env")
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.expanduser().resolve())
        except OSError:
            key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        result.append(path.expanduser())
    return result


def _parse_env_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_strategy_llm_env(*, explicit_path: str | None = None) -> Path | None:
    """Load Strategy Factory LLM config from .env with file priority."""
    for env_path in _project_root_candidates(explicit_path=explicit_path):
        if not env_path.exists() or not env_path.is_file():
            continue
        try:
            values = _parse_env_lines(env_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for key, value in values.items():
            if not _is_llm_env_key(key):
                continue
            if _llm_env_file_priority_disabled():
                os.environ.setdefault(key, value)
            else:
                os.environ[key] = value
        return env_path
    return None
