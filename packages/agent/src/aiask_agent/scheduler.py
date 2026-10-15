from __future__ import annotations

import asyncio
import subprocess
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .general_tools import WorkspaceGuard, _sanitized_env
from .paths import default_state_db_path
from .session_store import now_iso
from .paths import aiask_agent_home
import logging

logger = logging.getLogger(__name__)


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    import json

    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


class AgentJobStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_jobs (
                job_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule TEXT,
                interval_seconds INTEGER,
                toolset TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_job_runs (
                job_run_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                status TEXT NOT NULL,
                response_id TEXT,
                run_id TEXT,
                error TEXT,
                duration_ms INTEGER,
                payload_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_job_runs_job ON agent_job_runs(job_id, started_at)")
        conn.commit()
        return conn

    def create(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str | None = None,
        interval_seconds: int | None = None,
        toolset: str = "finance_safe",
        enabled: bool = True,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(name or "").strip():
            raise ValueError("job name is required")
        if not str(prompt or "").strip():
            raise ValueError("job prompt is required")
        interval = int(interval_seconds or 0) or None
        next_run_at = self._next_run_at(schedule=schedule, interval_seconds=interval) if enabled else None
        job_id = f"job_{uuid4().hex}"
        ts = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO agent_jobs
                    (job_id, name, prompt, schedule, interval_seconds, toolset, enabled,
                     payload_json, next_run_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    prompt,
                    schedule,
                    interval,
                    toolset,
                    1 if enabled else 0,
                    _dumps(dict(payload or {})),
                    next_run_at,
                    ts,
                    ts,
                ),
            )
            conn.commit()
        item = self.get(job_id)
        assert item is not None
        return item

    def get(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (str(job_id or "").strip(),)).fetchone()
        return self._row(row)

    def list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM agent_jobs ORDER BY created_at DESC").fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        current = self.get(job_id)
        if current is None:
            return None
        allowed = {"name", "prompt", "schedule", "interval_seconds", "toolset", "enabled", "payload"}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "payload":
                assignments.append("payload_json = ?")
                values.append(_dumps(dict(value or {})))
            else:
                assignments.append(f"{key} = ?")
                if key == "enabled":
                    values.append(1 if bool(value) else 0)
                elif key == "interval_seconds":
                    values.append(int(value or 0) or None)
                else:
                    values.append(value)
        if {"schedule", "interval_seconds", "enabled"} & set(fields):
            next_schedule = fields.get("schedule", current.get("schedule"))
            next_interval = fields.get("interval_seconds", current.get("interval_seconds"))
            interval = int(next_interval or 0) or None
            enabled = bool(fields.get("enabled", current.get("enabled")))
            assignments.append("next_run_at = ?")
            values.append(self._next_run_at(schedule=next_schedule, interval_seconds=interval) if enabled else None)
        if not assignments:
            return self.get(job_id)
        assignments.append("updated_at = ?")
        values.append(now_iso())
        values.append(str(job_id or "").strip())
        with closing(self._connect()) as conn:
            conn.execute(f"UPDATE agent_jobs SET {', '.join(assignments)} WHERE job_id = ?", tuple(values))
            conn.commit()
        return self.get(job_id)

    def delete(self, job_id: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM agent_jobs WHERE job_id = ?", (str(job_id or "").strip(),))
            conn.commit()
            return cur.rowcount > 0

    def due_jobs(self, now: float | None = None) -> list[dict[str, Any]]:
        ts = float(now or time.time())
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM agent_jobs WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
                (ts,),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def mark_ran(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        next_run_at = self._next_run_at(schedule=job.get("schedule"), interval_seconds=job.get("interval_seconds"))
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE agent_jobs SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE job_id = ?",
                (now_iso(), next_run_at, now_iso(), job_id),
            )
            conn.commit()

    def record_run_start(self, job_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        job_run_id = f"jobrun_{uuid4().hex}"
        started_at = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO agent_job_runs
                    (job_run_id, job_id, status, payload_json, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_run_id, str(job_id or "").strip(), "running", _dumps(dict(payload or {})), started_at),
            )
            conn.commit()
        return {"job_run_id": job_run_id, "job_id": job_id, "status": "running", "started_at": started_at}

    def record_run_finish(
        self,
        job_run_id: str,
        *,
        status: str,
        response_id: str | None = None,
        run_id: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        finished_at = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE agent_job_runs
                SET status = ?, response_id = ?, run_id = ?, error = ?, duration_ms = ?,
                    payload_json = ?, finished_at = ?
                WHERE job_run_id = ?
                """,
                (
                    str(status or "completed"),
                    response_id,
                    run_id,
                    error,
                    duration_ms,
                    _dumps(dict(payload or {})),
                    finished_at,
                    str(job_run_id or "").strip(),
                ),
            )
            conn.commit()
        return self.get_run(job_run_id)

    def get_run(self, job_run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_job_runs WHERE job_run_id = ?",
                (str(job_run_id or "").strip(),),
            ).fetchone()
        return self._run_row(row)

    def list_runs(self, job_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if job_id:
            clauses.append("job_id = ?")
            values.append(str(job_id))
        values.append(max(1, min(int(limit or 100), 500)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_job_runs {where} ORDER BY started_at DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._run_row(row)) is not None]

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["payload"] = _loads(item.pop("payload_json", None), {})
        return item

    @staticmethod
    def _run_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json", None), {})
        return item

    @staticmethod
    def _next_run_at(*, schedule: str | None = None, interval_seconds: int | None = None) -> float | None:
        if interval_seconds:
            return time.time() + max(1, int(interval_seconds))
        raw = str(schedule or "").strip()
        if not raw:
            return None
        parts = raw.split()
        if len(parts) != 5:
            return None
        minute = parts[0]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if minute == "*":
            return (now + timedelta(minutes=1)).timestamp()
        if minute.startswith("*/"):
            try:
                step = max(1, min(int(minute[2:]), 59))
            except ValueError as _swallowed:
                logger.warning("packages/agent/src/aiask_agent/scheduler.py:312: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return None
            next_minute = ((now.minute // step) + 1) * step
            candidate = now.replace(minute=0) + timedelta(minutes=next_minute)
            return candidate.timestamp()
        try:
            fixed = int(minute)
        except ValueError as _swallowed:
            logger.warning("packages/agent/src/aiask_agent/scheduler.py:319: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return None
        if fixed < 0 or fixed > 59:
            return None
        candidate = now.replace(minute=fixed)
        if candidate <= now:
            candidate += timedelta(hours=1)
        return candidate.timestamp()


class BackgroundScheduler:
    def __init__(self, *, runtime: Any, store: AgentJobStore | None = None, poll_seconds: float = 1.0) -> None:
        self.runtime = runtime
        self.store = store or AgentJobStore(getattr(runtime.session_store, "path", None))
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="aiask-agent-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    async def run_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if not job:
            return {"success": False, "error": f"job not found: {job_id}", "error_code": "NOT_FOUND"}
        started = time.time()
        run_record = self.store.record_run_start(job_id, {"job": {k: job.get(k) for k in ("job_id", "name", "toolset")}})
        payload = dict(job.get("payload") or {})
        try:
            if payload.get("stock_radar") is True and payload.get("action") == "run_once":
                from .adapters.confirmed_actions import _execute_stock_radar

                result = await _execute_stock_radar("run_once", dict(payload.get("run_params") or {}))
                result_dict = result if isinstance(result, dict) else {"success": True, "data": result, "error": None}
                success = bool(result_dict.get("success") is not False)
                self.store.mark_ran(job_id)
                finished = self.store.record_run_finish(
                    str(run_record["job_run_id"]),
                    status="completed" if success else "failed",
                    error=None if success else str(result_dict.get("error") or result_dict.get("error_code") or "stock radar job failed"),
                    duration_ms=int((time.time() - started) * 1000),
                    payload={"stock_radar": result_dict},
                )
                return {
                    "success": success,
                    "data": {
                        "job": self.store.get(job_id),
                        "job_run": finished,
                        "stock_radar": result_dict.get("data"),
                    },
                    "error": None if success else str(result_dict.get("error") or result_dict.get("error_code") or "stock radar job failed"),
                    "error_code": None if success else result_dict.get("error_code"),
                }
            prompt = self._build_prompt(job)
            result = await self.runtime.run([{"role": "user", "content": prompt}], user_id=None)
            self.store.mark_ran(job_id)
            silent_pattern = str(dict(job.get("payload") or {}).get("silent_pattern") or "").strip()
            silent = bool(silent_pattern and silent_pattern in result.content)
            finished = self.store.record_run_finish(
                str(run_record["job_run_id"]),
                status="completed",
                response_id=result.response_id,
                run_id=result.run_id,
                duration_ms=int((time.time() - started) * 1000),
                payload={"silent": silent},
            )
            return {
                "success": True,
                "data": {
                    "job": self.store.get(job_id),
                    "job_run": finished,
                    "response_id": result.response_id,
                    "run_id": result.run_id,
                    "silent": silent,
                },
                "error": None,
            }
        except Exception as exc:
            finished = self.store.record_run_finish(
                str(run_record["job_run_id"]),
                status="failed",
                error=str(exc),
                duration_ms=int((time.time() - started) * 1000),
                payload={"job_id": job_id},
            )
            return {
                "success": False,
                "data": {"job": self.store.get(job_id), "job_run": finished},
                "error": str(exc),
                "error_code": "JOB_RUN_FAILED",
            }

    def _workspace_guard(self) -> WorkspaceGuard:
        policy = getattr(getattr(getattr(self.runtime, "tool_registry", None), "policy_engine", None), "policy", None)
        return WorkspaceGuard(policy)

    def _resolve_script_path(self, script: str) -> Path | None:
        raw = str(script or "").strip()
        if not raw:
            return None
        path = self._workspace_guard().resolve(raw)
        if not path.exists():
            return None
        if not path.is_file():
            raise IsADirectoryError(str(path))
        return path

    def _build_prompt(self, job: dict[str, Any]) -> str:
        payload = dict(job.get("payload") or {})
        parts: list[str] = [
            "[SYSTEM: You are running as a scheduled AIASK cron job. Keep output concise and auditable.]",
        ]
        script = str(payload.get("script") or "").strip()
        if script:
            path = self._resolve_script_path(script)
            if path is not None:
                try:
                    proc = subprocess.run(
                        [str(path)],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=30,
                        check=False,
                        env=_sanitized_env(),
                    )
                    parts.append(f"[SCRIPT stdout]\n{proc.stdout[:12000]}")
                    if proc.stderr:
                        parts.append(f"[SCRIPT stderr]\n{proc.stderr[:4000]}")
                except Exception as exc:
                    parts.append(f"[SCRIPT error]\n{exc}")
        skill_root = aiask_agent_home() / "skills"
        for skill in [str(item).strip() for item in list(payload.get("skills") or []) if str(item).strip()]:
            path = skill_root / skill / "SKILL.md"
            if path.exists():
                parts.append(f"[SKILL {skill}]\n{path.read_text(encoding='utf-8', errors='replace')[:20000]}")
        parts.append(str(job["prompt"]))
        return "\n\n".join(parts)

    def _loop(self) -> None:
        while not self._stop.is_set():
            for job in self.store.due_jobs():
                try:
                    asyncio.run(self.run_job(job["job_id"]))
                except Exception:
                    self.store.mark_ran(job["job_id"])
            self._stop.wait(self.poll_seconds)
