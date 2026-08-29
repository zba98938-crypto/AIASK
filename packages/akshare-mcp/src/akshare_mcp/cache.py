import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple


def _default_cache_dir() -> str:
    override = os.getenv("AKSHARE_MCP_CACHE_DIR") or os.getenv("AIASK_CACHE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    runtime_home = os.getenv("AIASK_RUNTIME_HOME")
    if runtime_home:
        return os.path.abspath(os.path.join(os.path.expanduser(runtime_home), "cache", "mcp_cache"))
    localapp = os.getenv("LOCALAPPDATA")
    if localapp:
        return os.path.abspath(os.path.join(localapp, "hermes-aiask", "cache", "mcp_cache"))
    xdg_state = os.getenv("XDG_STATE_HOME")
    if xdg_state:
        return os.path.abspath(os.path.join(os.path.expanduser(xdg_state), "hermes-aiask", "cache", "mcp_cache"))
    return os.path.abspath(os.path.join(os.path.expanduser("~"), ".aiask", "cache", "mcp_cache"))


class SimpleCache:
    """两层缓存：内存LRU + 文件缓存（原子写）"""

    def __init__(self, cache_dir: str | None = None, memory_maxsize: int = 512):
        self.cache_dir = cache_dir or _default_cache_dir()
        self.memory_maxsize = max(1, int(memory_maxsize))
        self._lock = threading.RLock()
        self._memory_cache: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._stats: Dict[str, int] = {
            "total_requests": 0,
            "hits": 0,
            "misses": 0,
            "memory_hits": 0,
            "file_hits": 0,
            "file_reads": 0,
            "file_writes": 0,
            "write_failures": 0,
            "evictions": 0,
        }

        if not os.path.exists(self.cache_dir):
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
            except Exception:
                self.cache_dir = os.path.join(tempfile.gettempdir(), "mcp_cache")
                os.makedirs(self.cache_dir, exist_ok=True)

    def _get_path(self, key: str) -> str:
        safe_key = "".join(c if c.isalnum() else "_" for c in key)
        return os.path.join(self.cache_dir, f"{safe_key}.json")

    def _is_expired(self, ts: float, ttl_seconds: float) -> bool:
        return time.time() - ts > ttl_seconds

    def _memory_get(self, key: str, ttl_seconds: float) -> Optional[Any]:
        with self._lock:
            record = self._memory_cache.get(key)
            if not record:
                return None
            ts, payload = record
            if self._is_expired(ts, ttl_seconds):
                self._memory_cache.pop(key, None)
                return None
            self._memory_cache.move_to_end(key)
            return payload

    def _memory_set(self, key: str, ts: float, value: Any) -> None:
        with self._lock:
            self._memory_cache[key] = (ts, value)
            self._memory_cache.move_to_end(key)
            while len(self._memory_cache) > self.memory_maxsize:
                self._memory_cache.popitem(last=False)
                self._stats["evictions"] += 1

    def get(self, key: str, ttl_seconds: float) -> Optional[Any]:
        path = self._get_path(key)
        with self._lock:
            self._stats["total_requests"] += 1

        payload = self._memory_get(key, ttl_seconds)
        if payload is not None:
            with self._lock:
                self._stats["hits"] += 1
                self._stats["memory_hits"] += 1
            return payload

        if not os.path.exists(path):
            with self._lock:
                self._stats["misses"] += 1
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self._stats["file_reads"] += 1

            ts = float(data.get("ts", 0))
            if self._is_expired(ts, ttl_seconds):
                with self._lock:
                    self._stats["misses"] += 1
                return None

            payload = data.get("payload")
            self._memory_set(key, ts, payload)
            with self._lock:
                self._stats["hits"] += 1
                self._stats["file_hits"] += 1
            return payload
        except Exception:
            with self._lock:
                self._stats["misses"] += 1
            return None

    def set(self, key: str, value: Any):
        path = self._get_path(key)
        ts = time.time()
        self._memory_set(key, ts, value)

        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".cache_", suffix=".tmp", dir=self.cache_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"ts": ts, "payload": value}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            with self._lock:
                os.replace(temp_path, path)
                self._stats["file_writes"] += 1
        except Exception:
            with self._lock:
                self._stats["write_failures"] += 1
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def clear(self) -> int:
        with self._lock:
            self._memory_cache.clear()
        count = 0
        if os.path.exists(self.cache_dir):
            for filename in os.listdir(self.cache_dir):
                if filename.endswith(".json"):
                    path = os.path.join(self.cache_dir, filename)
                    try:
                        os.remove(path)
                        count += 1
                    except Exception:
                        continue
        return count

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = int(self._stats["total_requests"])
            hits = int(self._stats["hits"])
            misses = int(self._stats["misses"])
            hit_rate = (hits / total) if total > 0 else 0.0
            miss_rate = (misses / total) if total > 0 else 0.0
            file_count = 0
            total_size = 0
            if os.path.exists(self.cache_dir):
                for name in os.listdir(self.cache_dir):
                    if not name.endswith(".json"):
                        continue
                    file_count += 1
                    try:
                        total_size += os.path.getsize(os.path.join(self.cache_dir, name))
                    except Exception:
                        continue
            return {
                **self._stats,
                "hit_rate": round(hit_rate, 4),
                "miss_rate": round(miss_rate, 4),
                "file_count": file_count,
                "total_size_mb": round(total_size / (1024 * 1024), 4),
                "cache_dir": self.cache_dir,
                "memory_maxsize": self.memory_maxsize,
                "memory_size": len(self._memory_cache),
            }

    def get_cache_stats(self) -> Dict[str, Any]:
        """向后兼容/语义化别名。"""
        return self.get_stats()


cache = SimpleCache()
