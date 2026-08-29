from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import os
import shlex
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any
from uuid import uuid4

from .approvals import ApprovalStore, command_requires_approval, public_approval
from .control_context import control_context_active
from .intents import ActionIntentStore, public_intent
from .numeric import bounded_float, bounded_int
from .plugin_runtime import NativePluginManager
from .process_registry import ProcessRegistry
from .paths import aiask_agent_home
from .terminal_backends import TerminalBackendError, TerminalBackendManager, TerminalInvocation, backend_status, command_targets_aiask_runtime
from .tools.policy import ToolPolicy, build_policy_from_env


def _envelope(
    success: bool,
    *,
    data: Any = None,
    error: str | None = None,
    tool_name: str,
    level: str,
    target: str | None = None,
    idempotent: bool = True,
) -> dict[str, Any]:
    return {
        "success": bool(success),
        "data": data,
        "error": error,
        "meta": {
            "trace_id": f"aiask-agent:{tool_name}:{int(time.time() * 1000)}:{uuid4().hex[:8]}",
            "source_chain": ["aiask_agent.general_tools"],
            "side_effect": {
                "level": level,
                "target": target or tool_name,
                "confirmation_required": False,
                "idempotent": idempotent,
            },
        },
    }


class WorkspaceGuard:
    def __init__(self, policy: ToolPolicy | None = None) -> None:
        self.policy = policy or build_policy_from_env()
        self.roots = tuple(Path(root).expanduser().resolve() for root in self.policy.workspace_roots)

    def resolve(self, raw: str | None, *, must_exist: bool = False) -> Path:
        token = str(raw or ".").strip() or "."
        path = Path(token).expanduser()
        if not path.is_absolute():
            path = self.roots[0] / path
        resolved = path.resolve()
        if must_exist and not resolved.exists():
            raise FileNotFoundError(str(resolved))
        if not self._is_allowed(resolved):
            allowed = ", ".join(str(root) for root in self.roots)
            raise PermissionError(f"path is outside allowed AIASK Agent workspace roots: {resolved}; roots={allowed}")
        return resolved

    def _is_allowed(self, path: Path) -> bool:
        for root in self.roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False


def _limit_bytes(value: bytes, limit: int) -> tuple[str, bool]:
    limited = bounded_int(limit, default=65536, minimum=1, maximum=1024 * 1024)
    truncated = len(value) > limited
    return value[:limited].decode("utf-8", errors="replace"), truncated


def _is_binary(raw: bytes) -> bool:
    if not raw:
        return False
    sample = raw[:4096]
    return b"\x00" in sample


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _sanitized_env() -> dict[str, str]:
    blocked = ("KEY", "TOKEN", "SECRET", "PASSWORD", "COOKIE", "AUTH", "CREDENTIAL")
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(token in upper for token in blocked):
            continue
        env[key] = value
    return env


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mutation_verification(path: Path, guard: WorkspaceGuard, *, operation: str, before_sha256: str | None = None) -> dict[str, Any]:
    exists = path.exists()
    stat = path.stat() if exists else None
    after_sha256 = _sha256_file(path)
    return {
        "operation": operation,
        "path": str(path),
        "exists": exists,
        "is_file": bool(path.is_file()) if exists else False,
        "allowed_workspace": guard._is_allowed(path.resolve()),
        "size_bytes": stat.st_size if stat else None,
        "mtime": stat.st_mtime if stat else None,
        "sha256": after_sha256,
        "before_sha256": before_sha256,
        "changed": before_sha256 != after_sha256,
    }


def _checkpoint_public(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint_id": metadata.get("checkpoint_id"),
        "path": metadata.get("path"),
        "reason": metadata.get("reason"),
        "created_at": metadata.get("created_at"),
        "existed": bool(metadata.get("existed")),
        "size_bytes": metadata.get("size_bytes"),
        "sha256": metadata.get("sha256"),
    }


def _checkpoint_root(state_path: Path | None) -> Path:
    base = Path(state_path).expanduser().parent if state_path is not None else aiask_agent_home()
    return base / "file_checkpoints"


def _create_file_checkpoint(path: Path, guard: WorkspaceGuard, checkpoint_dir: Path, *, reason: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not guard._is_allowed(resolved):
        raise PermissionError(f"path is outside allowed AIASK Agent workspace roots: {resolved}")
    if resolved.exists() and not resolved.is_file():
        raise IsADirectoryError(str(resolved))
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_id = f"fchk_{uuid4().hex}"
    backup_path: Path | None = None
    stat = resolved.stat() if resolved.exists() else None
    if resolved.exists():
        backup_path = checkpoint_dir / f"{checkpoint_id}.blob"
        backup_path.write_bytes(resolved.read_bytes())
    metadata = {
        "checkpoint_id": checkpoint_id,
        "path": str(resolved),
        "reason": str(reason or "manual").strip()[:500] or "manual",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "created_at_epoch": time.time(),
        "existed": bool(resolved.exists()),
        "size_bytes": stat.st_size if stat else 0,
        "sha256": _sha256_file(resolved),
        "backup_path": str(backup_path) if backup_path else None,
    }
    (checkpoint_dir / f"{checkpoint_id}.json").write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return metadata


def _load_checkpoint_metadata(checkpoint_dir: Path, *, checkpoint_id: str | None = None, path: Path | None = None) -> dict[str, Any]:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    target_path = str(path.resolve()) if path is not None else None
    for metadata_path in checkpoint_dir.glob("fchk_*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if checkpoint_id and metadata.get("checkpoint_id") != checkpoint_id:
            continue
        if target_path and metadata.get("path") != target_path:
            continue
        metadata["_metadata_path"] = str(metadata_path)
        records.append(metadata)
    if not records:
        if checkpoint_id:
            raise FileNotFoundError(f"checkpoint not found: {checkpoint_id}")
        raise FileNotFoundError(f"checkpoint not found for path: {target_path}")
    records.sort(key=lambda item: float(item.get("created_at_epoch") or 0), reverse=True)
    return records[0]


def _python_diagnostics(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() != ".py" or not path.exists() or not path.is_file():
        return []
    import py_compile

    try:
        py_compile.compile(str(path), doraise=True)
        return [{"path": str(path), "language": "python", "status": "passed", "source": "py_compile"}]
    except py_compile.PyCompileError as exc:
        error = exc.exc_value
        return [
            {
                "path": str(path),
                "language": "python",
                "status": "failed",
                "source": "py_compile",
                "message": str(error),
                "line": getattr(error, "lineno", None),
                "offset": getattr(error, "offset", None),
            }
        ]
    except Exception as exc:
        return [{"path": str(path), "language": "python", "status": "failed", "source": "py_compile", "message": str(exc)}]


def _json_diagnostics(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() != ".json" or not path.exists() or not path.is_file():
        return []
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return [{"path": str(path), "language": "json", "status": "passed", "source": "json"}]
    except json.JSONDecodeError as exc:
        return [
            {
                "path": str(path),
                "language": "json",
                "status": "failed",
                "source": "json",
                "message": exc.msg,
                "line": exc.lineno,
                "offset": exc.colno,
            }
        ]


def _toml_diagnostics(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() != ".toml" or not path.exists() or not path.is_file():
        return []
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
        return [{"path": str(path), "language": "toml", "status": "passed", "source": "tomllib"}]
    except tomllib.TOMLDecodeError as exc:
        return [{"path": str(path), "language": "toml", "status": "failed", "source": "tomllib", "message": str(exc)}]


def _yaml_diagnostics(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() not in {".yaml", ".yml"} or not path.exists() or not path.is_file():
        return []
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return [
            {
                "path": str(path),
                "language": "yaml",
                "status": "skipped",
                "source": "pyyaml",
                "message": "PyYAML not installed; YAML diagnostics skipped (best-effort).",
            }
        ]
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
        return [{"path": str(path), "language": "yaml", "status": "passed", "source": "pyyaml"}]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return [
            {
                "path": str(path),
                "language": "yaml",
                "status": "failed",
                "source": "pyyaml",
                "message": str(getattr(exc, "problem", exc)),
                "line": (mark.line + 1) if mark is not None else None,
                "offset": (mark.column + 1) if mark is not None else None,
            }
        ]


def _write_diagnostics(path: Path) -> list[dict[str, Any]]:
    return [*_python_diagnostics(path), *_json_diagnostics(path), *_toml_diagnostics(path), *_yaml_diagnostics(path)]


def build_general_tool_handlers(
    policy: ToolPolicy | None = None,
    *,
    state_path: Path | None = None,
    intent_store: ActionIntentStore | None = None,
) -> dict[str, Any]:
    guard = WorkspaceGuard(policy)
    intents = intent_store or ActionIntentStore()
    processes = ProcessRegistry(state_path)
    approvals = ApprovalStore(state_path)
    checkpoint_dir = _checkpoint_root(state_path)
    session_cwds: dict[str, str] = {}
    processes.recover_running(allowed_roots=guard.roots)
    terminal_manager = TerminalBackendManager(processes, allowed_roots=guard.roots)
    plugin_manager = NativePluginManager()

    def _runtime_process_scope_error(
        arguments: dict[str, Any],
        *,
        tool_name: str,
        action: str,
    ) -> dict[str, Any] | None:
        runtime_context = dict(arguments.get("_aiask_runtime_context") or {})
        runtime_session_id = str(runtime_context.get("session_id") or "").strip()
        if not runtime_session_id:
            return None
        requested_session_id = str(arguments.get("session_id") or "").strip()
        if action == "list":
            if requested_session_id and requested_session_id != runtime_session_id:
                payload = _envelope(
                    False,
                    error="process tool cannot target a different session than the active runtime",
                    tool_name=tool_name,
                    level="process_control",
                    target=requested_session_id,
                    idempotent=False,
                )
                payload["error_code"] = "PROCESS_SESSION_SCOPE_VIOLATION"
                payload["meta"]["process_session_guard"] = {
                    "blocked": True,
                    "reason": "runtime_context_scope_mismatch",
                }
                return payload
            arguments["session_id"] = runtime_session_id
            return None
        process_id = str(arguments.get("process_id") or "").strip()
        if not process_id:
            return None
        item = processes.get(process_id)
        if item is None:
            return None
        target_session_id = str(item.get("session_id") or "").strip()
        if target_session_id != runtime_session_id:
            payload = _envelope(
                False,
                error="process tool cannot access a process from a different session than the active runtime",
                tool_name=tool_name,
                level="process_control",
                target=process_id,
                idempotent=False,
            )
            payload["error_code"] = "PROCESS_SESSION_SCOPE_VIOLATION"
            payload["meta"]["process_session_guard"] = {
                "blocked": True,
                "reason": "runtime_context_process_session_mismatch",
            }
            return payload
        return None

    def _runtime_terminal_session(
        arguments: dict[str, Any],
        *,
        tool_name: str,
    ) -> tuple[str, dict[str, Any] | None]:
        runtime_context = dict(arguments.get("_aiask_runtime_context") or {})
        runtime_session_id = str(runtime_context.get("session_id") or "").strip()
        requested_session_id = str(arguments.get("session_id") or "").strip()
        if runtime_session_id and requested_session_id and requested_session_id != runtime_session_id:
            payload = _envelope(
                False,
                error="terminal tool cannot target a different session than the active runtime",
                tool_name=tool_name,
                level="process_execution",
                target=requested_session_id,
                idempotent=False,
            )
            payload["error_code"] = "TERMINAL_SESSION_SCOPE_VIOLATION"
            payload["meta"]["process_session_guard"] = {
                "blocked": True,
                "reason": "runtime_context_scope_mismatch",
            }
            return requested_session_id, payload
        return requested_session_id or runtime_session_id, None

    def _file_action_intent_required(
        *,
        tool_name: str,
        action: str,
        arguments: dict[str, Any],
        target: str,
        rationale: str,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in dict(arguments or {}).items()
            if not str(key).startswith("_aiask_")
        }
        params["_state_path"] = str(state_path) if state_path is not None else ""
        params["_workspace_roots"] = [str(root) for root in guard.roots]
        params["_created_by_agent_file_tool"] = tool_name
        intent = intents.create(
            action=action,
            params=params,
            user_id=arguments.get("user_id"),
            rationale=str(arguments.get("rationale") or rationale),
            ttl_seconds=bounded_int(arguments.get("ttl_seconds"), default=86400, minimum=60, maximum=604800),
        )
        intent_id = str(intent.get("intent_id") or "")
        payload = _envelope(
            False,
            data={
                "required_tool": "agent_action_intent_get",
                "required_confirmation_endpoint": f"/intents/{intent_id}/confirm",
                "intent": public_intent(intent),
                "action_intent_required": True,
                "executed": False,
            },
            error="filesystem mutation requires ActionIntent confirmation",
            tool_name=tool_name,
            level="filesystem_write",
            target=target,
            idempotent=False,
        )
        payload["error_code"] = "ACTION_INTENT_REQUIRED"
        payload["meta"]["side_effect"]["confirmation_required"] = True
        payload["meta"]["action_intent_required"] = True
        return payload

    def _browser_action_intent_required(
        *,
        tool_name: str,
        action: str,
        arguments: dict[str, Any],
        target: str,
        rationale: str,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in dict(arguments or {}).items()
            if not str(key).startswith("_aiask_")
        }
        params["_created_by_agent_browser_tool"] = tool_name
        intent = intents.create(
            action=action,
            params=params,
            user_id=arguments.get("user_id"),
            rationale=str(arguments.get("rationale") or rationale),
            ttl_seconds=bounded_int(arguments.get("ttl_seconds"), default=86400, minimum=60, maximum=604800),
        )
        intent_id = str(intent.get("intent_id") or "")
        payload = _envelope(
            False,
            data={
                "required_tool": "agent_action_intent_get",
                "required_confirmation_endpoint": f"/intents/{intent_id}/confirm",
                "intent": public_intent(intent),
                "action_intent_required": True,
                "executed": False,
            },
            error="browser automation action requires ActionIntent confirmation",
            tool_name=tool_name,
            level="browser_state",
            target=target,
            idempotent=False,
        )
        payload["error_code"] = "ACTION_INTENT_REQUIRED"
        payload["meta"]["side_effect"]["confirmation_required"] = True
        payload["meta"]["action_intent_required"] = True
        return payload

    def _execution_action_intent_required(
        *,
        tool_name: str,
        action: str,
        arguments: dict[str, Any],
        target: str,
        level: str,
        created_by_key: str,
        rationale: str,
        error: str,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in dict(arguments or {}).items()
            if not str(key).startswith("_aiask_") and key != "approval_id"
        }
        params["_state_path"] = str(state_path) if state_path is not None else ""
        params["_workspace_roots"] = [str(root) for root in guard.roots]
        params[created_by_key] = tool_name
        intent = intents.create(
            action=action,
            params=params,
            user_id=arguments.get("user_id"),
            rationale=str(arguments.get("rationale") or rationale),
            ttl_seconds=bounded_int(arguments.get("ttl_seconds"), default=86400, minimum=60, maximum=604800),
        )
        intent_id = str(intent.get("intent_id") or "")
        payload = _envelope(
            False,
            data={
                "required_tool": "agent_action_intent_get",
                "required_confirmation_endpoint": f"/intents/{intent_id}/confirm",
                "intent": public_intent(intent),
                "action_intent_required": True,
                "executed": False,
            },
            error=error,
            tool_name=tool_name,
            level=level,
            target=target,
            idempotent=False,
        )
        payload["error_code"] = "ACTION_INTENT_REQUIRED"
        payload["meta"]["side_effect"]["confirmation_required"] = True
        payload["meta"]["action_intent_required"] = True
        return payload

    async def file_read(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_read"
        try:
            path = guard.resolve(arguments.get("path"), must_exist=True)
            if not path.is_file():
                raise IsADirectoryError(str(path))
            raw = path.read_bytes()
            if _is_binary(raw):
                raise ValueError("binary file reads are blocked by AIASK file safety")
            content, truncated = _limit_bytes(raw, bounded_int(arguments.get("max_bytes"), default=262144, minimum=1, maximum=1024 * 1024))
            return _envelope(
                True,
                data={"path": str(path), "content": content, "bytes": len(raw), "truncated": truncated},
                tool_name=tool,
                level="read_only",
                target=str(path),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def file_write(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_write"
        try:
            path = guard.resolve(arguments.get("path"))
            if not control_context_active():
                return _file_action_intent_required(
                    tool_name=tool,
                    action="file.write",
                    arguments=arguments,
                    target=str(path),
                    rationale="Workspace file writes require ActionIntent confirmation.",
                )
            before_sha256 = _sha256_file(path)
            checkpoint = None
            if bool(arguments.get("checkpoint", True)):
                checkpoint = _create_file_checkpoint(path, guard, checkpoint_dir, reason=str(arguments.get("checkpoint_reason") or "pre-write"))
            if bool(arguments.get("create_parent_dirs", False)):
                path.parent.mkdir(parents=True, exist_ok=True)
            content = str(arguments.get("content") or "")
            path.write_text(content, encoding="utf-8")
            return _envelope(
                True,
                data={
                    "path": str(path),
                    "bytes": len(content.encode("utf-8")),
                    "checkpoint": _checkpoint_public(checkpoint) if checkpoint else None,
                    "mutation_verification": _mutation_verification(path, guard, operation="write", before_sha256=before_sha256),
                    "diagnostics": _write_diagnostics(path),
                },
                tool_name=tool,
                level="filesystem_write",
                target=str(path),
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="filesystem_write", idempotent=False)

    async def file_list(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_list"
        try:
            root = guard.resolve(arguments.get("path"), must_exist=True)
            limit = bounded_int(arguments.get("limit"), default=200, minimum=1, maximum=1000)
            iterator = root.rglob("*") if bool(arguments.get("recursive", False)) else root.iterdir()
            items: list[dict[str, Any]] = []
            for item in iterator:
                if not guard._is_allowed(item.resolve()):
                    continue
                items.append(
                    {
                        "path": str(item),
                        "name": item.name,
                        "type": "directory" if item.is_dir() else "file",
                        "bytes": item.stat().st_size if item.is_file() else None,
                    }
                )
                if len(items) >= limit:
                    break
            return _envelope(True, data={"path": str(root), "items": items}, tool_name=tool, level="read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def file_search(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_search"
        try:
            root = guard.resolve(arguments.get("path"), must_exist=True)
            query = str(arguments.get("query") or "")
            if not query:
                raise ValueError("query is required")
            limit = bounded_int(arguments.get("limit"), default=50, minimum=1, maximum=200)
            files = [root] if root.is_file() else list(root.rglob("*"))
            matches: list[dict[str, Any]] = []
            for file_path in files:
                if len(matches) >= limit:
                    break
                if not file_path.is_file() or not guard._is_allowed(file_path.resolve()):
                    continue
                try:
                    for lineno, line in enumerate(file_path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if query.lower() in line.lower():
                            matches.append({"path": str(file_path), "line": lineno, "text": line[:500]})
                            if len(matches) >= limit:
                                break
                except Exception:
                    continue
            return _envelope(True, data={"query": query, "matches": matches}, tool_name=tool, level="read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def file_patch(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_patch"
        try:
            path = guard.resolve(arguments.get("path"), must_exist=True)
            if not control_context_active():
                return _file_action_intent_required(
                    tool_name=tool,
                    action="file.patch",
                    arguments=arguments,
                    target=str(path),
                    rationale="Workspace file patches require ActionIntent confirmation.",
                )
            before_sha256 = _sha256_file(path)
            checkpoint = None
            if bool(arguments.get("checkpoint", True)):
                checkpoint = _create_file_checkpoint(path, guard, checkpoint_dir, reason=str(arguments.get("checkpoint_reason") or "pre-patch"))
            old = str(arguments.get("old") or "")
            new = str(arguments.get("new") or "")
            if not old:
                raise ValueError("old text is required")
            count = bounded_int(arguments.get("count"), default=1, minimum=1)
            text = path.read_text(encoding="utf-8")
            if old not in text:
                candidates = difflib.get_close_matches(old, text.splitlines(), n=1, cutoff=0.75)
                if not candidates:
                    raise ValueError("old text was not found")
                old = candidates[0]
            updated = text.replace(old, new, count)
            path.write_text(updated, encoding="utf-8")
            return _envelope(
                True,
                data={
                    "path": str(path),
                    "replacements": min(count, text.count(old)),
                    "checkpoint": _checkpoint_public(checkpoint) if checkpoint else None,
                    "mutation_verification": _mutation_verification(path, guard, operation="patch", before_sha256=before_sha256),
                    "diagnostics": _write_diagnostics(path),
                },
                tool_name=tool,
                level="filesystem_write",
                target=str(path),
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="filesystem_write", idempotent=False)

    async def file_mutation_verify(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_mutation_verify"
        try:
            path = guard.resolve(arguments.get("path"), must_exist=False)
            operation = str(arguments.get("operation") or "verify").strip().lower() or "verify"
            before_sha256 = str(arguments.get("before_sha256") or "").strip() or None
            data = _mutation_verification(path, guard, operation=operation, before_sha256=before_sha256)
            if bool(arguments.get("include_diagnostics", True)):
                data["diagnostics"] = _write_diagnostics(path)
            return _envelope(True, data=data, tool_name=tool, level="read_only", target=str(path))
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def file_checkpoint(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_checkpoint"
        try:
            path = guard.resolve(arguments.get("path"), must_exist=False)
            if not control_context_active():
                return _file_action_intent_required(
                    tool_name=tool,
                    action="file.checkpoint",
                    arguments=arguments,
                    target=str(path),
                    rationale="Workspace file checkpoints require ActionIntent confirmation.",
                )
            checkpoint = _create_file_checkpoint(path, guard, checkpoint_dir, reason=str(arguments.get("reason") or "manual"))
            return _envelope(
                True,
                data={"checkpoint": _checkpoint_public(checkpoint)},
                tool_name=tool,
                level="filesystem_write",
                target=str(path),
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="filesystem_write", idempotent=False)

    async def file_rollback(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_file_rollback"
        try:
            checkpoint_id = str(arguments.get("checkpoint_id") or "").strip() or None
            path_arg = str(arguments.get("path") or "").strip()
            if not checkpoint_id and not path_arg:
                raise ValueError("checkpoint_id or path is required")
            path = guard.resolve(path_arg, must_exist=False) if path_arg else None
            target_preview = str(path) if path is not None else str(checkpoint_id or "file_checkpoint")
            if not control_context_active():
                return _file_action_intent_required(
                    tool_name=tool,
                    action="file.rollback",
                    arguments=arguments,
                    target=target_preview,
                    rationale="Workspace file rollbacks require ActionIntent confirmation.",
                )
            checkpoint = _load_checkpoint_metadata(checkpoint_dir, checkpoint_id=checkpoint_id, path=path)
            target = guard.resolve(str(checkpoint.get("path") or ""), must_exist=False)
            before_sha256 = _sha256_file(target)
            pre_rollback = _create_file_checkpoint(
                target,
                guard,
                checkpoint_dir,
                reason=str(arguments.get("reason") or f"pre-rollback:{checkpoint.get('checkpoint_id')}"),
            )
            if checkpoint.get("existed"):
                backup_raw = str(checkpoint.get("backup_path") or "").strip()
                if not backup_raw:
                    raise FileNotFoundError("checkpoint backup is missing")
                backup_path = Path(backup_raw).resolve()
                backup_path.relative_to(checkpoint_dir.resolve())
                if not backup_path.exists() or not backup_path.is_file():
                    raise FileNotFoundError(f"checkpoint backup not found: {checkpoint.get('checkpoint_id')}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup_path.read_bytes())
            else:
                if target.exists():
                    if not target.is_file():
                        raise IsADirectoryError(str(target))
                    target.unlink()
            data = {
                "rolled_back_to": _checkpoint_public(checkpoint),
                "pre_rollback_checkpoint": _checkpoint_public(pre_rollback),
                "mutation_verification": _mutation_verification(target, guard, operation="rollback", before_sha256=before_sha256),
                "diagnostics": _write_diagnostics(target),
            }
            return _envelope(True, data=data, tool_name=tool, level="filesystem_write", target=str(target), idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="filesystem_write", idempotent=False)

    async def terminal(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_terminal"
        try:
            command = str(arguments.get("command") or "").strip()
            if not command:
                raise ValueError("command is required")
            session_id, scope_error = _runtime_terminal_session(arguments, tool_name=tool)
            if scope_error is not None:
                return scope_error
            backend = str(arguments.get("backend") or "local").strip().lower() or "local"
            cwd_raw = arguments.get("cwd")
            if cwd_raw is None and session_id and session_id in session_cwds:
                cwd_raw = session_cwds[session_id]
            if backend in {"local", "docker", "singularity"}:
                cwd = guard.resolve(cwd_raw or ".", must_exist=True)
                cwd_value = str(cwd)
            else:
                cwd_value = str(cwd_raw or ".")
            if not control_context_active():
                intent_arguments = {**arguments, "cwd": cwd_value, "backend": backend}
                if session_id:
                    intent_arguments["session_id"] = session_id
                return _execution_action_intent_required(
                    tool_name=tool,
                    action="terminal.run",
                    arguments=intent_arguments,
                    target=cwd_value,
                    level="process_execution",
                    created_by_key="_created_by_agent_terminal_tool",
                    rationale="Terminal command execution requires ActionIntent confirmation.",
                    error="terminal command execution requires ActionIntent confirmation",
                )
            approval_id = str(arguments.get("approval_id") or "").strip()
            if command_targets_aiask_runtime(command) or command_requires_approval(command):
                approval = approvals.get(approval_id) if approval_id else None
                if not approval or approval.get("status") != "approved":
                    reason = "command targets the AIASK server or gateway runtime" if command_targets_aiask_runtime(command) else "dangerous command requires control-plane approval"
                    pending = approvals.create(
                        tool_name=tool,
                        action="terminal",
                        arguments={"command": command, "cwd": arguments.get("cwd") or "."},
                        reason=reason,
                    )
                    payload = _envelope(
                        False,
                        data={"approval": public_approval(pending)},
                        error="approval required",
                        tool_name=tool,
                        level="process_execution",
                        idempotent=False,
                    )
                    payload["error_code"] = "APPROVAL_REQUIRED"
                    return payload
            if command.lower().startswith("aiask-allow "):
                command = command[len("aiask-allow ") :].strip()
            backend_info = backend_status(backend)
            if session_id:
                session_cwds[session_id] = cwd_value
            timeout = bounded_float(arguments.get("timeout_seconds"), default=30.0, minimum=1.0, maximum=300.0)
            max_output = bounded_int(arguments.get("max_output_bytes"), default=65536, minimum=1, maximum=1024 * 1024)
            env = _sanitized_env()
            for key in list(arguments.get("env_allowlist") or []):
                key = str(key or "").strip()
                if key and key in os.environ:
                    env[key] = os.environ[key]
            data = await terminal_manager.execute(
                TerminalInvocation(
                    command=command,
                    cwd=cwd_value,
                    backend=backend,
                    session_id=session_id or None,
                    stdin=arguments.get("stdin"),
                    background=bool(arguments.get("background", False)),
                    pty=bool(arguments.get("pty", False)),
                    image=arguments.get("image"),
                    resource_limits=dict(arguments.get("resource_limits") or {}),
                    timeout_seconds=timeout,
                    max_output_bytes=max_output,
                    env=env,
                    notify_on_complete=bool(arguments.get("notify_on_complete", False)),
                )
            )
            data, plugin_events = await plugin_manager.transform_terminal_output(command=command, output=data)
            if plugin_events:
                data["plugin_events"] = plugin_events
            return _envelope(
                True,
                data=data,
                tool_name=tool,
                level="process_execution",
                target=cwd_value,
                idempotent=False,
            )
        except TerminalBackendError as exc:
            payload = _envelope(
                False,
                data={"backend": exc.status.to_dict() if exc.status else None},
                error=str(exc),
                tool_name=tool,
                level="process_execution",
                idempotent=False,
            )
            payload["error_code"] = exc.code
            return payload
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="process_execution", idempotent=False)

    async def process(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_process"
        action = str(arguments.get("action") or "list").strip().lower()
        try:
            scope_error = _runtime_process_scope_error(arguments, tool_name=tool, action=action)
            if scope_error is not None:
                return scope_error
            if action == "list":
                data = {
                    "items": processes.list(
                        session_id=arguments.get("session_id"),
                        limit=bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=1000),
                    )
                }
            elif action == "read":
                process_id = str(arguments.get("process_id") or "").strip()
                data = processes.read_output(
                    process_id,
                    max_bytes=bounded_int(arguments.get("max_output_bytes"), default=65536, minimum=1, maximum=1024 * 1024),
                    tail=bool(arguments.get("tail", True)),
                )
            elif action == "kill":
                if not control_context_active():
                    return _execution_action_intent_required(
                        tool_name=tool,
                        action="process.kill",
                        arguments=arguments,
                        target=str(arguments.get("process_id") or "process"),
                        level="process_control",
                        created_by_key="_created_by_agent_process_tool",
                        rationale="Process termination requires ActionIntent confirmation.",
                        error="process termination requires ActionIntent confirmation",
                    )
                data = await terminal_manager.process_action(arguments)
            elif action == "wait":
                if not control_context_active():
                    return _execution_action_intent_required(
                        tool_name=tool,
                        action="process.wait",
                        arguments=arguments,
                        target=str(arguments.get("process_id") or "process"),
                        level="process_control",
                        created_by_key="_created_by_agent_process_tool",
                        rationale="Process wait control requires ActionIntent confirmation.",
                        error="process wait control requires ActionIntent confirmation",
                    )
                data = await terminal_manager.process_action(arguments)
            elif action == "watch":
                if not control_context_active():
                    return _execution_action_intent_required(
                        tool_name=tool,
                        action="process.watch",
                        arguments=arguments,
                        target=str(arguments.get("process_id") or "process"),
                        level="process_control",
                        created_by_key="_created_by_agent_process_tool",
                        rationale="Process watch control requires ActionIntent confirmation.",
                        error="process watch control requires ActionIntent confirmation",
                    )
                data = await terminal_manager.process_action(arguments)
            else:
                raise ValueError(f"unsupported process action: {action}")
            return _envelope(True, data=data, tool_name=tool, level="process_control", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="process_control", idempotent=False)

    async def execute_python(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_execute_python"
        code = str(arguments.get("code") or "")
        if not code:
            return _envelope(False, error="code is required", tool_name=tool, level="code_execution", idempotent=False)
        cwd = guard.resolve(arguments.get("cwd") or str(guard.roots[0]), must_exist=True)
        if not control_context_active():
            return _execution_action_intent_required(
                tool_name=tool,
                action="code.execute_python",
                arguments={**arguments, "cwd": str(cwd)},
                target=str(cwd),
                level="code_execution",
                created_by_key="_created_by_agent_code_tool",
                rationale="Python code execution requires ActionIntent confirmation.",
                error="python code execution requires ActionIntent confirmation",
            )
        with tempfile.TemporaryDirectory(prefix="aiask_agent_code_") as tmp:
            script = Path(tmp) / "snippet.py"
            script.write_text(code, encoding="utf-8")
            next_args = dict(arguments)
            next_args["command"] = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            next_args["cwd"] = str(cwd)
            result = await terminal(next_args)
            result["meta"]["source_chain"].append("aiask_agent.general_tools.execute_python")
            result["meta"]["side_effect"]["level"] = "code_execution"
            return result

    browser_state: dict[str, Any] = {"playwright": None, "browser": None, "page": None, "console": []}

    async def ensure_page() -> Any:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError("browser automation requires optional dependency: playwright") from exc
        if browser_state["page"] is not None:
            return browser_state["page"]
        browser_state["playwright"] = await async_playwright().start()
        browser_state["browser"] = await browser_state["playwright"].chromium.launch(headless=True)
        browser_state["page"] = await browser_state["browser"].new_page()
        browser_state["page"].on(
            "console",
            lambda msg: browser_state["console"].append(
                {"type": msg.type, "text": msg.text, "timestamp": int(time.time() * 1000)}
            ),
        )
        return browser_state["page"]

    async def browser_navigate(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_navigate"
        url = str(arguments.get("url") or "").strip()
        try:
            if not url:
                raise ValueError("url is required")
            if not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.navigate",
                    arguments=arguments,
                    target=url,
                    rationale="Browser navigation requires ActionIntent confirmation.",
                )
            page = await ensure_page()
            response = await page.goto(url, wait_until="domcontentloaded")
            return _envelope(
                True,
                data={"url": page.url, "status": response.status if response else None, "title": await page.title()},
                tool_name=tool,
                level="browser_state",
                target=url,
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    async def browser_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_snapshot"
        try:
            page = await ensure_page()
            return _envelope(
                True,
                data={"url": page.url, "title": await page.title(), "text": (await page.locator("body").inner_text())[:20000]},
                tool_name=tool,
                level="read_only",
                target=page.url,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def browser_click(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_click"
        try:
            selector = str(arguments.get("selector") or "").strip()
            if not selector:
                raise ValueError("selector is required")
            if not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.click",
                    arguments=arguments,
                    target=selector,
                    rationale="Browser clicks require ActionIntent confirmation.",
                )
            page = await ensure_page()
            await page.locator(selector).first.click()
            return _envelope(
                True,
                data={"url": page.url, "selector": selector},
                tool_name=tool,
                level="browser_state",
                target=selector,
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    async def browser_type(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_type"
        try:
            selector = str(arguments.get("selector") or "").strip()
            text = str(arguments.get("text") or "")
            if not selector:
                raise ValueError("selector is required")
            if not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.type",
                    arguments=arguments,
                    target=selector,
                    rationale="Browser text input requires ActionIntent confirmation.",
                )
            page = await ensure_page()
            await page.locator(selector).first.fill(text)
            return _envelope(
                True,
                data={"url": page.url, "selector": selector, "chars": len(text)},
                tool_name=tool,
                level="browser_state",
                target=selector,
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    async def browser_extract(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_extract"
        try:
            selector = str(arguments.get("selector") or "body").strip() or "body"
            page = await ensure_page()
            text = await page.locator(selector).first.inner_text()
            return _envelope(
                True,
                data={"url": page.url, "selector": selector, "text": text[:20000], "truncated": len(text) > 20000},
                tool_name=tool,
                level="read_only",
                target=selector,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def browser_scroll(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_scroll"
        try:
            amount = bounded_int(arguments.get("amount"), default=600, minimum=1, maximum=5000)
            direction = str(arguments.get("direction") or "down").lower()
            selector = str(arguments.get("selector") or "").strip()
            if not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.scroll",
                    arguments=arguments,
                    target=selector or direction,
                    rationale="Browser scrolling requires ActionIntent confirmation.",
                )
            page = await ensure_page()
            dx = amount if direction == "right" else -amount if direction == "left" else 0
            dy = amount if direction == "down" else -amount if direction == "up" else 0
            if selector:
                await page.locator(selector).first.evaluate("(el, delta) => el.scrollBy(delta.x, delta.y)", {"x": dx, "y": dy})
            else:
                await page.mouse.wheel(dx, dy)
            return _envelope(True, data={"url": page.url, "direction": direction, "amount": amount}, tool_name=tool, level="browser_state", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    async def browser_back(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_back"
        try:
            if not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.back",
                    arguments=arguments,
                    target="browser_history",
                    rationale="Browser history navigation requires ActionIntent confirmation.",
                )
            page = await ensure_page()
            response = await page.go_back(wait_until="domcontentloaded")
            return _envelope(True, data={"url": page.url, "status": response.status if response else None}, tool_name=tool, level="browser_state", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    async def browser_press(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_press"
        try:
            key = str(arguments.get("key") or "").strip()
            if not key:
                raise ValueError("key is required")
            if not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.press",
                    arguments=arguments,
                    target=key,
                    rationale="Browser key presses require ActionIntent confirmation.",
                )
            page = await ensure_page()
            await page.keyboard.press(key)
            return _envelope(True, data={"url": page.url, "key": key}, tool_name=tool, level="browser_state", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    async def browser_get_images(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_get_images"
        try:
            page = await ensure_page()
            limit = bounded_int(arguments.get("limit"), default=30, minimum=1, maximum=100)
            images = await page.locator("img").evaluate_all(
                "(imgs, limit) => imgs.slice(0, limit).map((img) => ({src: img.currentSrc || img.src, alt: img.alt || '', width: img.naturalWidth || img.width, height: img.naturalHeight || img.height}))",
                limit,
            )
            return _envelope(True, data={"url": page.url, "images": images}, tool_name=tool, level="read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def browser_vision(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_vision"
        try:
            page = await ensure_page()
            shot = await page.screenshot(full_page=False)
            return _envelope(
                True,
                data={"url": page.url, "title": await page.title(), "screenshot_bytes": len(shot), "prompt": arguments.get("prompt")},
                tool_name=tool,
                level="read_only",
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def browser_console(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_console"
        limit = bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=500)
        return _envelope(True, data={"messages": list(browser_state["console"])[-limit:]}, tool_name=tool, level="read_only")

    async def browser_cdp(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_browser_cdp"
        try:
            method = str(arguments.get("method") or "").strip()
            if not method:
                raise ValueError("method is required")
            if not method.startswith(("Runtime.", "Page.", "DOM.", "Network.")):
                raise PermissionError("CDP method is outside the AIASK allowed namespaces")
            if not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.cdp",
                    arguments=arguments,
                    target=method,
                    rationale="Browser CDP commands require ActionIntent confirmation.",
                )
            page = await ensure_page()
            session = await page.context.new_cdp_session(page)
            result = await session.send(method, dict(arguments.get("params") or {}))
            return _envelope(True, data={"method": method, "result": result}, tool_name=tool, level="browser_state", idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    async def computer_use(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_computer_use"
        action = str(arguments.get("action") or "status").strip().lower()
        enabled = _truthy_env("AIASK_AGENT_ENABLE_COMPUTER_USE")
        base_status = {
            "configured": enabled,
            "enabled": enabled,
            "backend": "browser_session",
            "os_desktop_control": False,
            "required_env": ["AIASK_AGENT_ENABLE_COMPUTER_USE"],
            "actions": ["status", "screenshot", "browser_click", "browser_type", "browser_key"],
        }
        if action == "status":
            return _envelope(True, data=base_status, tool_name=tool, level="read_only")
        if not enabled:
            return _envelope(
                False,
                data=base_status,
                error="computer_use is disabled",
                tool_name=tool,
                level="browser_state",
                idempotent=False,
            )
        try:
            if action in {"browser_click", "browser_type", "browser_key"} and not control_context_active():
                return _browser_action_intent_required(
                    tool_name=tool,
                    action="browser.computer_use",
                    arguments=arguments,
                    target=str(arguments.get("selector") or arguments.get("key") or action),
                    rationale="Computer-use browser interaction requires ActionIntent confirmation.",
                )
            page = await ensure_page()
            if action == "screenshot":
                shot = await page.screenshot(full_page=bool(arguments.get("full_page", False)))
                include_base64 = bool(arguments.get("include_base64", False))
                data: dict[str, Any] = {
                    **base_status,
                    "url": page.url,
                    "title": await page.title(),
                    "screenshot_bytes": len(shot),
                    "image_format": "png",
                }
                if include_base64:
                    data["screenshot_base64"] = base64.b64encode(shot).decode("ascii")
                return _envelope(True, data=data, tool_name=tool, level="read_only", target=page.url)
            if action == "browser_click":
                selector = str(arguments.get("selector") or "").strip()
                if not selector:
                    raise ValueError("selector is required")
                await page.locator(selector).first.click()
                return _envelope(True, data={**base_status, "url": page.url, "selector": selector}, tool_name=tool, level="browser_state", target=selector, idempotent=False)
            if action == "browser_type":
                selector = str(arguments.get("selector") or "").strip()
                text = str(arguments.get("text") or "")
                if not selector:
                    raise ValueError("selector is required")
                await page.locator(selector).first.fill(text)
                return _envelope(True, data={**base_status, "url": page.url, "selector": selector, "chars": len(text)}, tool_name=tool, level="browser_state", target=selector, idempotent=False)
            if action == "browser_key":
                key = str(arguments.get("key") or "").strip()
                if not key:
                    raise ValueError("key is required")
                await page.keyboard.press(key)
                return _envelope(True, data={**base_status, "url": page.url, "key": key}, tool_name=tool, level="browser_state", target=key, idempotent=False)
            raise ValueError(f"unsupported computer_use action: {action}")
        except Exception as exc:
            return _envelope(False, data=base_status, error=str(exc), tool_name=tool, level="browser_state", idempotent=False)

    return {
        "agent_file_read": file_read,
        "agent_file_write": file_write,
        "agent_file_list": file_list,
        "agent_file_search": file_search,
        "agent_file_patch": file_patch,
        "agent_file_mutation_verify": file_mutation_verify,
        "agent_file_checkpoint": file_checkpoint,
        "agent_file_rollback": file_rollback,
        "agent_terminal": terminal,
        "agent_process": process,
        "agent_execute_python": execute_python,
        "agent_computer_use": computer_use,
        "agent_browser_navigate": browser_navigate,
        "agent_browser_snapshot": browser_snapshot,
        "agent_browser_click": browser_click,
        "agent_browser_type": browser_type,
        "agent_browser_extract": browser_extract,
        "agent_browser_scroll": browser_scroll,
        "agent_browser_back": browser_back,
        "agent_browser_press": browser_press,
        "agent_browser_get_images": browser_get_images,
        "agent_browser_vision": browser_vision,
        "agent_browser_console": browser_console,
        "agent_browser_cdp": browser_cdp,
    }
