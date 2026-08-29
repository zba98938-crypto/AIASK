from __future__ import annotations

import ast
import importlib.util
import json
import os
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import aiask_agent_home, default_state_db_path
from .session_store import now_iso


LOCKED_CONFIG_FIELDS = {
    "tokenizer_name",
    "rollout_server_url",
    "use_wandb",
    "max_token_length",
    "max_num_workers",
    "worker_timeout",
    "total_steps",
    "steps_per_eval",
    "max_batches_offpolicy",
    "inference_weight",
    "eval_limit_ratio",
    "openai",
    "tinker",
    "slurm",
    "testing",
}


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _env_paths() -> list[Path]:
    raw = str(os.getenv("AIASK_RL_ENV_PATHS", "")).strip()
    paths = [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]
    default_path = aiask_agent_home() / "rl_environments"
    return [*paths, default_path]


def _pid_is_running(pid: object) -> bool:
    try:
        value = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, value)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(value, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _launch_args(command: str) -> list[str]:
    """Parse a configured launch command without invoking an intermediate shell."""
    text = str(command or "").strip()
    if not text:
        raise ValueError("launch command is required")
    current_python_prefix = f"{sys.executable} "
    if text.startswith(current_python_prefix):
        suffix = shlex.split(text[len(current_python_prefix):], posix=os.name != "nt")
        return [sys.executable, *suffix]
    return shlex.split(text, posix=os.name != "nt")


def configured() -> bool:
    return bool(os.getenv("TINKER_API_KEY") and os.getenv("WANDB_API_KEY"))


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _write_simple_yaml(path: Path, payload: dict[str, Any]) -> None:
    lines: list[str] = []
    for key, value in sorted(payload.items()):
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        elif value is None:
            rendered = "null"
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class RLEnvironmentInfo:
    name: str
    class_name: str
    file_path: str
    description: str = ""
    config_class: str = "BaseEnvConfig"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "class_name": self.class_name,
            "file_path": self.file_path,
            "description": self.description,
            "config_class": self.config_class,
        }


class RLAtroposStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rl_runs (
                run_id TEXT PRIMARY KEY,
                environment TEXT NOT NULL,
                config_json TEXT NOT NULL,
                status TEXT NOT NULL,
                pid INTEGER,
                log_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rl_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        return conn

    def get_state(self, key: str, default: Any = None) -> Any:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value_json FROM rl_state WHERE key = ?", (key,)).fetchone()
        return _json_load(row["value_json"], default) if row else default

    def set_state(self, key: str, value: Any) -> Any:
        ts = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rl_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True), ts),
            )
            conn.commit()
        return value

    def create_run(self, *, environment: str, config: dict[str, Any], pid: int | None, log_path: str | None, status: str) -> dict[str, Any]:
        run_id = f"rlrun_{uuid4().hex}"
        ts = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO rl_runs (run_id, environment, config_json, status, pid, log_path, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, environment, json.dumps(config, ensure_ascii=False, sort_keys=True), status, pid, log_path, None, ts, ts),
            )
            conn.commit()
        item = self.get_run(run_id)
        assert item is not None
        return item

    def update_run(self, run_id: str, *, status: str | None = None, error: str | None = None) -> dict[str, Any] | None:
        item = self.get_run(run_id)
        if item is None:
            return None
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE rl_runs SET status = ?, error = ?, updated_at = ? WHERE run_id = ?",
                (status or item["status"], error if error is not None else item.get("error"), now_iso(), run_id),
            )
            conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM rl_runs WHERE run_id = ?", (str(run_id or "").strip(),)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["config"] = _json_load(item.pop("config_json", None), {})
        return item

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM rl_runs ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit or 100), 1000)),)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["config"] = _json_load(item.pop("config_json", None), {})
            items.append(item)
        return items


class RLAtroposManager:
    def __init__(self, path: Path | None = None) -> None:
        self.store = RLAtroposStore(path)

    def list_environments(self) -> dict[str, Any]:
        environments: list[dict[str, Any]] = []
        for root in _env_paths():
            if not root.exists():
                continue
            for file_path in sorted(root.rglob("*.py")):
                if file_path.name.startswith("_"):
                    continue
                environments.extend(item.to_dict() for item in self._scan_file(file_path))
        return {
            "configured": configured(),
            "readiness": self.readiness(),
            "environment_paths": [str(path) for path in _env_paths()],
            "environments": environments,
        }

    def readiness(self, environment: str | None = None) -> dict[str, Any]:
        envs = self._environment_index()
        env_name = str(environment or self.store.get_state("current_environment") or "").strip()
        package_available = _module_available("atroposlib") or _module_available("tinker_atropos")
        launch_command = str(os.getenv("AIASK_ATROPOS_LAUNCH_COMMAND") or os.getenv("AIASK_RL_START_COMMAND") or "").strip()
        checks = {
            "tinker_api_key": bool(os.getenv("TINKER_API_KEY")),
            "wandb_api_key": bool(os.getenv("WANDB_API_KEY")),
            "atropos_package": package_available,
            "launch_command": bool(launch_command),
            "environment_selected": bool(env_name),
            "environment_found": bool(env_name and env_name in envs),
        }
        missing = [key for key, ok in checks.items() if not ok and key not in {"launch_command", "atropos_package"}]
        if not checks["atropos_package"] and not checks["launch_command"]:
            missing.append("atropos_package_or_AIASK_ATROPOS_LAUNCH_COMMAND")
        ready = not missing
        return {
            "configured": ready,
            "checks": checks,
            "missing": missing,
            "environment": envs.get(env_name) if env_name else None,
            "package": {
                "atroposlib": _module_available("atroposlib"),
                "tinker_atropos": _module_available("tinker_atropos"),
            },
            "required_env": ["TINKER_API_KEY", "WANDB_API_KEY"],
            "launch_command_env": "AIASK_ATROPOS_LAUNCH_COMMAND",
        }

    def select_environment(self, name: str) -> dict[str, Any]:
        token = str(name or "").strip()
        if not token:
            raise ValueError("environment is required")
        self.store.set_state("current_environment", token)
        return {"current_environment": token}

    def current_config(self) -> dict[str, Any]:
        return {
            "configured": configured(),
            "readiness": self.readiness(),
            "current_environment": self.store.get_state("current_environment"),
            "config": self.store.get_state("current_config", {}),
            "locked_fields": sorted(LOCKED_CONFIG_FIELDS),
        }

    def edit_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        config = dict(self.store.get_state("current_config", {}) or {})
        rejected = sorted(key for key in dict(patch or {}) if key in LOCKED_CONFIG_FIELDS)
        for key, value in dict(patch or {}).items():
            if key not in LOCKED_CONFIG_FIELDS:
                config[key] = value
        self.store.set_state("current_config", config)
        return {"config": config, "rejected_locked_fields": rejected, "locked_fields": sorted(LOCKED_CONFIG_FIELDS)}

    def start_training(self, *, environment: str | None = None, config_patch: dict[str, Any] | None = None) -> dict[str, Any]:
        env_name = str(environment or self.store.get_state("current_environment") or "").strip()
        if not env_name:
            raise ValueError("environment is required")
        config = dict(self.store.get_state("current_config", {}) or {})
        if config_patch:
            config = self.edit_config(config_patch)["config"]
        self.store.set_state("current_environment", env_name)
        readiness = self.readiness(env_name)
        if not readiness["configured"]:
            return {"configured": False, "started": False, "readiness": readiness, "required_env": ["TINKER_API_KEY", "WANDB_API_KEY"]}
        log_dir = aiask_agent_home() / "logs" / "rl_training"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{env_name}-{uuid4().hex[:8]}.log"
        run_dir = log_dir / f"{env_name}-{uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        env_info = readiness.get("environment") or {}
        locked_config = self._locked_config(env_name=env_name, env_info=env_info, config=config, run_dir=run_dir)
        config_path = run_dir / "config.json"
        config_yaml_path = run_dir / "config.yaml"
        config_path.write_text(json.dumps(locked_config, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        _write_simple_yaml(config_yaml_path, locked_config)
        command = self._training_command(env_name=env_name, env_info=env_info, config_path=config_path, config_yaml_path=config_yaml_path, log_path=log_path)
        env = dict(os.environ)
        env["AIASK_RL_ENVIRONMENT"] = env_name
        env["AIASK_RL_CONFIG"] = str(config_path)
        env["AIASK_RL_CONFIG_YAML"] = str(config_yaml_path)
        env["PYTHONPATH"] = os.pathsep.join([*(str(path) for path in _env_paths() if path.exists()), env.get("PYTHONPATH", "")])
        log_fh = log_path.open("ab")
        proc = subprocess.Popen(_launch_args(command), stdout=log_fh, stderr=log_fh, cwd=str(run_dir), env=env)
        item = self.store.create_run(environment=env_name, config={**locked_config, "config_path": str(config_path), "run_dir": str(run_dir)}, pid=proc.pid, log_path=str(log_path), status="running")
        item["started"] = True
        item["configured"] = True
        item["readiness"] = readiness
        return item

    def check_status(self, run_id: str) -> dict[str, Any]:
        item = self.store.get_run(run_id)
        if item is None:
            raise FileNotFoundError(f"RL run not found: {run_id}")
        pid = item.get("pid")
        if pid and item.get("status") == "running":
            if not _pid_is_running(pid):
                item = self.store.update_run(run_id, status="completed") or item
        return item

    def stop_training(self, run_id: str) -> dict[str, Any]:
        item = self.store.get_run(run_id)
        if item is None:
            raise FileNotFoundError(f"RL run not found: {run_id}")
        pid = item.get("pid")
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        return self.store.update_run(run_id, status="stopped") or item

    def results(self, run_id: str) -> dict[str, Any]:
        item = self.check_status(run_id)
        log_path = Path(str(item.get("log_path") or ""))
        tail = ""
        if log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
        return {"run": item, "log_tail": tail}

    def logs(self, run_id: str, *, max_bytes: int = 65536, tail: bool = True) -> dict[str, Any]:
        item = self.check_status(run_id)
        log_path = Path(str(item.get("log_path") or ""))
        text = ""
        if log_path.exists():
            limit = max(1, min(int(max_bytes or 65536), 1024 * 1024))
            raw = log_path.read_bytes()
            text = (raw[-limit:] if tail else raw[:limit]).decode("utf-8", errors="replace")
        return {"run": item, "log": text, "tail": bool(tail)}

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_runs(limit=limit)

    def test_inference(self, prompt: str) -> dict[str, Any]:
        url = str(os.getenv("AIASK_RL_INFERENCE_URL") or "").strip()
        if not url:
            return {"configured": False, "prompt": prompt, "inference_url_configured": False, "error_type": "missing_endpoint"}
        payload = json.dumps({"prompt": prompt}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=float(os.getenv("AIASK_RL_INFERENCE_TIMEOUT", "30"))) as response:
                raw = response.read(1024 * 1024)
                text = raw.decode("utf-8", errors="replace")
            parsed: Any
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {"text": text[:20000]}
            return {
                "configured": True,
                "prompt": prompt,
                "inference_url_configured": True,
                "status": getattr(response, "status", None),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "response": parsed,
            }
        except urllib.error.HTTPError as exc:
            return {"configured": True, "prompt": prompt, "inference_url_configured": True, "error_type": "http_error", "status": exc.code, "error": exc.reason}
        except Exception as exc:
            return {"configured": True, "prompt": prompt, "inference_url_configured": True, "error_type": type(exc).__name__, "error": str(exc)}

    def _environment_index(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for root in _env_paths():
            if not root.exists():
                continue
            for file_path in sorted(root.rglob("*.py")):
                for item in self._scan_file(file_path):
                    result[item.name] = item.to_dict()
        return result

    @staticmethod
    def _locked_config(*, env_name: str, env_info: dict[str, Any], config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        locked = {
            "environment": env_name,
            "environment_file": env_info.get("file_path"),
            "environment_class": env_info.get("class_name"),
            "run_dir": str(run_dir),
            "created_by": "aiask_native_rl_atropos",
        }
        for key, value in dict(config or {}).items():
            if key not in LOCKED_CONFIG_FIELDS:
                locked[key] = value
        return locked

    @staticmethod
    def _training_command(*, env_name: str, env_info: dict[str, Any], config_path: Path, config_yaml_path: Path, log_path: Path) -> str:
        template = str(os.getenv("AIASK_ATROPOS_LAUNCH_COMMAND") or os.getenv("AIASK_RL_START_COMMAND") or "").strip()
        values = {
            "environment": shlex.quote(env_name),
            "env_file": shlex.quote(str(env_info.get("file_path") or "")),
            "config": shlex.quote(str(config_path)),
            "config_yaml": shlex.quote(str(config_yaml_path)),
            "log_path": shlex.quote(str(log_path)),
        }
        if template:
            return template.format(**values)
        if _module_available("tinker_atropos"):
            return f"{shlex.quote(sys.executable)} -m tinker_atropos.launch_training --config {values['config_yaml']}"
        raise RuntimeError("No Atropos launch command is available")

    @staticmethod
    def _scan_file(file_path: Path) -> list[RLEnvironmentInfo]:
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return []
        items: list[RLEnvironmentInfo] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {
                base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
                for base in node.bases
            }
            if "BaseEnv" not in base_names:
                continue
            description = ast.get_docstring(node) or f"Environment from {file_path.name}"
            env_name = file_path.stem
            config_class = "BaseEnvConfig"
            for child in node.body:
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == "name" and isinstance(child.value, ast.Constant):
                            env_name = str(child.value.value)
                        if isinstance(target, ast.Name) and target.id == "env_config_cls" and isinstance(child.value, ast.Name):
                            config_class = child.value.id
            items.append(RLEnvironmentInfo(env_name, node.name, str(file_path), description.splitlines()[0], config_class))
        return items
