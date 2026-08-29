from __future__ import annotations

import json
import os
import signal
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import default_state_db_path
from .session_store import now_iso


class ProcessRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    @staticmethod
    def new_process_id() -> str:
        return f"proc_{uuid4().hex}"

    @property
    def spool_dir(self) -> Path:
        return self.path.parent / "process-spool"

    def spool_paths(self, process_id: str) -> tuple[Path, Path]:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        return self.spool_dir / f"{process_id}.stdout.log", self.spool_dir / f"{process_id}.stderr.log"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS process_records (
                process_id TEXT PRIMARY KEY,
                session_id TEXT,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL,
                status TEXT NOT NULL,
                returncode INTEGER,
                stdout TEXT,
                stderr TEXT,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def record(
        self,
        *,
        command: str,
        cwd: str,
        status: str,
        process_id: str | None = None,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        process_id = process_id or self.new_process_id()
        ts = now_iso()
        next_metadata = dict(metadata or {})
        if next_metadata.get("background") and not next_metadata.get("stdout_path"):
            stdout_path, stderr_path = self.spool_paths(process_id)
            next_metadata.update(
                {
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "output_path": str(stdout_path),
                }
            )
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO process_records
                    (process_id, session_id, command, cwd, status, returncode, stdout, stderr, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    process_id,
                    session_id,
                    command,
                    cwd,
                    status,
                    returncode,
                    stdout,
                    stderr,
                    json.dumps(next_metadata, ensure_ascii=False, sort_keys=True),
                    ts,
                    ts,
                ),
            )
            conn.commit()
        item = self.get(process_id)
        assert item is not None
        return item

    def update(
        self,
        process_id: str,
        *,
        status: str | None = None,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = self.get(process_id)
        if current is None:
            return None
        next_metadata = dict(current.get("metadata") or {})
        if metadata:
            next_metadata.update(metadata)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE process_records
                SET status = ?, returncode = ?, stdout = ?, stderr = ?, metadata_json = ?, updated_at = ?
                WHERE process_id = ?
                """,
                (
                    status if status is not None else current.get("status"),
                    returncode if returncode is not None else current.get("returncode"),
                    stdout if stdout is not None else current.get("stdout") or "",
                    stderr if stderr is not None else current.get("stderr") or "",
                    json.dumps(next_metadata, ensure_ascii=False, sort_keys=True),
                    now_iso(),
                    process_id,
                ),
            )
            conn.commit()
        return self.get(process_id)

    def get(self, process_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM process_records WHERE process_id = ?",
                (str(process_id or "").strip(),),
            ).fetchone()
        return self._row(row)

    def list(self, *, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        values: list[Any] = []
        where = ""
        if session_id:
            where = "WHERE session_id = ?"
            values.append(session_id)
        values.append(max(1, min(int(limit or 100), 1000)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM process_records {where} ORDER BY created_at DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def recover_running(self, *, allowed_roots: tuple[Path, ...] | None = None) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        for item in self.list(limit=1000):
            if item.get("status") not in {"running", "detached_running"}:
                continue
            metadata = dict(item.get("metadata") or {})
            pid = self._metadata_pid(metadata)
            if not pid or not self._is_aiask_owned(item):
                continue
            if self._pid_alive(pid):
                if allowed_roots and not self._cwd_allowed(str(item.get("cwd") or ""), allowed_roots):
                    continue
                updated = self.update(
                    str(item["process_id"]),
                    status="detached_running",
                    metadata={"attached": False, "recovered": True, "last_seen_at": now_iso()},
                )
            else:
                updated = self.update(
                    str(item["process_id"]),
                    status="completed_unknown",
                    metadata={"attached": False, "recovered": True, "last_seen_at": now_iso()},
                )
            if updated:
                recovered.append(updated)
        return recovered

    def refresh(self, process_id: str) -> dict[str, Any] | None:
        item = self.get(process_id)
        if item is None:
            return None
        if item.get("status") not in {"running", "detached_running"}:
            return item
        pid = self._metadata_pid(dict(item.get("metadata") or {}))
        if pid and self._pid_alive(pid):
            return self.update(process_id, metadata={"last_seen_at": now_iso()}) or item
        return self.update(process_id, status="completed_unknown", metadata={"attached": False, "last_seen_at": now_iso()}) or item

    def read_output(self, process_id: str, *, max_bytes: int = 65536, tail: bool = True) -> dict[str, Any]:
        item = self.refresh(process_id)
        if item is None:
            raise ValueError(f"process not found: {process_id}")
        metadata = dict(item.get("metadata") or {})
        stdout_path = Path(str(metadata.get("stdout_path") or "")) if metadata.get("stdout_path") else None
        stderr_path = Path(str(metadata.get("stderr_path") or "")) if metadata.get("stderr_path") else None
        return {
            "process": item,
            "stdout": self._read_file_or_value(stdout_path, str(item.get("stdout") or ""), max_bytes=max_bytes, tail=tail),
            "stderr": self._read_file_or_value(stderr_path, str(item.get("stderr") or ""), max_bytes=max_bytes, tail=tail),
            "tail": bool(tail),
        }

    def kill(self, process_id: str, *, allowed_roots: tuple[Path, ...] | None = None) -> dict[str, Any]:
        item = self.get(process_id)
        if item is None:
            return {"killed": False, "error": f"process not found: {process_id}"}
        if item.get("status") in {"running", "detached_running"}:
            metadata = dict(item.get("metadata") or {})
            pid = self._metadata_pid(metadata)
            if not pid:
                return {"killed": False, "error": "process pid is not recorded", "process": item}
            if not self._is_aiask_owned(item):
                return {"killed": False, "error": "process is not AIASK-owned", "process": item}
            if allowed_roots and not self._cwd_allowed(str(item.get("cwd") or ""), allowed_roots):
                return {"killed": False, "error": "process cwd is outside allowed workspace roots", "process": item}
            if not self._pid_alive(pid):
                updated = self.update(process_id, status="completed_unknown", metadata={"attached": False, "last_seen_at": now_iso()})
                return {"killed": False, "error": "process is no longer running", "process": updated}
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 3
            while time.time() < deadline and self._pid_alive(pid):
                time.sleep(0.05)
            if self._pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            updated = self.update(process_id, status="killed", metadata={"attached": False, "last_seen_at": now_iso()})
            return {"killed": True, "process": updated}
        return {"killed": False, "error": "process already completed", "process": item}

    @staticmethod
    def _metadata_pid(metadata: dict[str, Any]) -> int | None:
        try:
            pid = int(metadata.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                handle = kernel32.OpenProcess(0x1000, False, int(pid))
                if not handle:
                    return False
                try:
                    exit_code = wintypes.DWORD()
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return False
                    return int(exit_code.value) == 259
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _is_aiask_owned(item: dict[str, Any]) -> bool:
        metadata = dict(item.get("metadata") or {})
        return bool(metadata.get("aiask_managed") or metadata.get("background"))

    @staticmethod
    def _cwd_allowed(cwd: str, roots: tuple[Path, ...]) -> bool:
        try:
            path = Path(cwd).expanduser().resolve()
        except OSError:
            return False
        for root in roots:
            try:
                path.relative_to(root.expanduser().resolve())
                return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _read_file_or_value(path: Path | None, fallback: str, *, max_bytes: int, tail: bool) -> str:
        limit = max(1, min(int(max_bytes or 65536), 1024 * 1024))
        if path is None or not path.exists():
            return fallback[-limit:] if tail else fallback[:limit]
        with path.open("rb") as fh:
            if tail:
                try:
                    fh.seek(max(0, path.stat().st_size - limit))
                except OSError:
                    pass
            raw = fh.read(limit)
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        raw = item.pop("metadata_json", None)
        try:
            item["metadata"] = json.loads(raw or "{}")
        except Exception:
            item["metadata"] = {}
        return item
