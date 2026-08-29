import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


def _default_whitelist_path() -> Path:
    env_path = os.getenv("TUSHARE_WHITELIST_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path(__file__).resolve().parent / "config" / "tushare_proxy_whitelist.json"


@lru_cache(maxsize=1)
def load_tushare_whitelist(path: Optional[str] = None) -> dict[str, Any]:
    target = Path(path).expanduser() if path else _default_whitelist_path()
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_api_supported(api_name: str, path: Optional[str] = None) -> Optional[bool]:
    payload = load_tushare_whitelist(path)
    supported = payload.get("supported") if isinstance(payload, dict) else None
    if not supported:
        return None
    return api_name in supported