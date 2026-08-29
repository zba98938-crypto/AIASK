from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


_LOADED_ENV_PATH: Path | None = None
_LOADED_ENV_SOURCE = "none"

LLM_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "AIASK_AGENT_IMAGE_MODEL",
    "AIASK_AGENT_MODEL",
    "AIASK_AGENT_MODEL_PROVIDER",
    "AIASK_AGENT_MODEL_PROVIDERS",
    "AIASK_AGENT_MODEL_TIMEOUT",
    "AIASK_AGENT_PROMPT_CACHE_ENABLED",
    "AIASK_AGENT_PROMPT_CACHE_RECENT_MESSAGES",
    "AIASK_AGENT_PROMPT_CACHE_STRATEGY",
    "AIASK_AGENT_CONTEXT_MAX_TOKENS",
    "AIASK_AGENT_MAX_TOKENS",
    "AIASK_AGENT_TTS_FORMAT",
    "AIASK_AGENT_TTS_PROVIDER",
    "AIASK_AGENT_TTS_VOICE",
    "AIASK_AGENT_VISION_MODEL",
    "AIASK_AGENT_VISION_PROVIDER",
    "AIASK_MOA_AGGREGATOR_MODEL",
    "AIASK_MOA_REFERENCE_MODELS",
    "AIASK_VIDEO_API_KEY",
    "AIASK_VIDEO_API_URL",
    "AIASK_VIDEO_BASE_URL",
    "AIASK_VIDEO_DURATION_SECONDS",
    "AIASK_VIDEO_MODEL",
    "AIASK_VIDEO_PROVIDER",
    "AIASK_VIDEO_SIZE",
    "AIASK_VOICE_STT_PROVIDER",
    "AIASK_VOICE_TTS_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_API_KEYS",
    "OPENAI_BASE_URL",
}

LLM_ENV_PREFIXES = (
    "AIASK_AGENT_MODEL_",
    "AIASK_AGENT_IMAGE_",
    "AIASK_AGENT_VISION_",
    "AIASK_AGENT_TTS_",
    "AIASK_MOA_",
    "AIASK_VIDEO_",
    "AIASK_VOICE_",
    "ANTHROPIC_",
    "DASHSCOPE_",
    "DEEPSEEK_",
    "MOONSHOT_",
    "OPENAI_",
    "QWEN_",
    "ZHIPU_",
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _disabled() -> bool:
    explicit = str(os.getenv("AIASK_AGENT_LOAD_PROJECT_ENV", "")).strip().lower()
    if explicit in {"0", "false", "no", "off"}:
        return True
    return _truthy(os.getenv("AIASK_AGENT_DISABLE_PROJECT_ENV"))


def _llm_env_file_priority_disabled() -> bool:
    explicit = str(os.getenv("AIASK_LLM_ENV_FILE_PRIORITY", "")).strip().lower()
    if explicit in {"0", "false", "no", "off"}:
        return True
    return _truthy(os.getenv("AIASK_DISABLE_LLM_ENV_FILE_PRIORITY"))


def _provider_pool_api_key_env_names(raw: str) -> set[str]:
    import json

    content = str(raw or "").strip()
    if not content:
        return set()
    try:
        parsed = json.loads(content)
    except Exception:
        return set()
    items = parsed if isinstance(parsed, list) else parsed.get("providers") if isinstance(parsed, dict) else []
    names: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("api_key_env") or item.get("credential_env") or "").strip()
        if name:
            names.add(name)
    return names


def is_llm_env_key(key: str, values: dict[str, str] | None = None) -> bool:
    name = str(key or "").strip()
    if not name:
        return False
    if name in LLM_ENV_KEYS:
        return True
    if any(name.startswith(prefix) for prefix in LLM_ENV_PREFIXES):
        return True
    provider_pool = ""
    if values is not None:
        provider_pool = values.get("AIASK_AGENT_MODEL_PROVIDERS", "")
    if name in _provider_pool_api_key_env_names(provider_pool):
        return True
    return False


def discover_project_root(start: Path | None = None) -> Path | None:
    starts = [start or Path.cwd(), Path(__file__).resolve()]
    seen: set[str] = set()
    for raw in starts:
        path = raw if raw.is_dir() else raw.parent
        for candidate in (path, *path.parents):
            try:
                key = str(candidate.resolve())
            except OSError:
                key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if (candidate / "AGENT.md").exists() and (candidate / "packages").is_dir():
                return candidate
            if (candidate / ".env").exists() and (candidate / "packages").is_dir() and (candidate / "desktop").is_dir():
                return candidate
    return None


def _dedupe(paths: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    result: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, source in paths:
        try:
            key = str(path.expanduser().resolve())
        except OSError:
            key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        result.append((path.expanduser(), source))
    return result


def project_env_candidates(explicit_path: str | None = None) -> list[tuple[Path, str]]:
    configured_path = explicit_path or os.getenv("AIASK_AGENT_ENV_FILE", "").strip() or os.getenv("AIASK_ENV_FILE", "").strip()
    candidates: list[tuple[Path, str]] = []
    if configured_path:
        candidates.append((Path(configured_path), "explicit"))
    root = discover_project_root()
    if root is not None:
        candidates.append((root / ".env", "project_root"))
    candidates.append((Path.cwd() / ".env", "cwd"))
    return _dedupe(candidates)


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


def _dotenv_values(path: Path) -> dict[str, str]:
    try:
        from dotenv import dotenv_values

        parsed = dotenv_values(path)
        return {str(key): str(value) for key, value in parsed.items() if key and value is not None}
    except Exception:
        pass
    try:
        return _parse_env_lines(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


_ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _quote_env_value(value: str) -> str:
    raw = str(value or "")
    if raw == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]+", raw):
        return raw
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def writable_project_env_path(explicit_path: str | None = None) -> tuple[Path, str]:
    candidates = project_env_candidates(explicit_path=explicit_path)
    for env_path, source in candidates:
        if env_path.exists() and env_path.is_file():
            return env_path, source
    if candidates:
        return candidates[0]
    root = discover_project_root()
    if root is not None:
        return root / ".env", "project_root"
    return Path.cwd() / ".env", "cwd"


def update_project_env_values(
    updates: dict[str, str | None],
    *,
    explicit_path: str | None = None,
    force_load: bool = True,
) -> dict[str, Any]:
    """Persist selected environment variables without returning secret values."""
    env_path, source = writable_project_env_path(explicit_path=explicit_path)
    normalized: dict[str, str] = {}
    for key, value in dict(updates or {}).items():
        name = str(key or "").strip().upper()
        if not name or not _ENV_KEY_PATTERN.match(name):
            raise ValueError(f"invalid environment variable name: {key}")
        if not is_llm_env_key(name, {**normalized, name: str(value or "")}):
            raise ValueError(f"refusing to update non-LLM environment variable: {name}")
        normalized[name] = str(value or "")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    created = not env_path.exists()
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines() if env_path.exists() else []
    except OSError:
        lines = []

    remaining = dict(normalized)
    output: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        candidate = stripped
        if candidate.startswith("export "):
            candidate = candidate[7:].strip()
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key in remaining:
            output.append(f"{key}={_quote_env_value(remaining.pop(key))}")
        else:
            output.append(raw)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# AIASK model configuration managed by Desktop")
        for key in sorted(remaining):
            output.append(f"{key}={_quote_env_value(remaining[key])}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    for key, value in normalized.items():
        os.environ[key] = value
    load_project_env(explicit_path=str(env_path), override=True, force=force_load)
    return {
        "path": str(env_path),
        "source": source,
        "created": created,
        "updated_keys": sorted(normalized),
        "secrets_redacted": True,
    }


def load_project_env(
    *,
    explicit_path: str | None = None,
    override: bool = False,
    force: bool = False,
) -> Path | None:
    """Load the project-root .env into os.environ without exposing values."""
    global _LOADED_ENV_PATH, _LOADED_ENV_SOURCE
    if _disabled() and not force and not explicit_path:
        _LOADED_ENV_PATH = None
        _LOADED_ENV_SOURCE = "disabled"
        return None
    for env_path, source in project_env_candidates(explicit_path=explicit_path):
        if not env_path.exists() or not env_path.is_file():
            continue
        values = _dotenv_values(env_path)
        for key, value in values.items():
            if override or (is_llm_env_key(key, values) and not _llm_env_file_priority_disabled()):
                os.environ[key] = value
            else:
                os.environ.setdefault(key, value)
        _LOADED_ENV_PATH = env_path
        _LOADED_ENV_SOURCE = source
        return env_path
    return None


def project_env_status() -> dict[str, Any]:
    return {
        "loaded": _LOADED_ENV_PATH is not None,
        "path": str(_LOADED_ENV_PATH) if _LOADED_ENV_PATH is not None else None,
        "source": _LOADED_ENV_SOURCE,
        "secrets_redacted": True,
    }
