from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from .action_intents import create_external_action_intent_payload
from ..paths import aiask_agent_home
from ..session_store_utils import redact_audit_text


DesktopAssetCall = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]
DesktopFileUpload = Callable[[list[UploadFile], dict[str, Any] | None], Awaitable[dict[str, Any]]]


def _safe_upload_filename(value: Any, *, fallback: str) -> str:
    raw = str(value or fallback).replace("\\", "/").split("/")[-1]
    raw = raw.replace("\r", " ").replace("\n", " ").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in "._- ()[]" else "_" for ch in raw)
    cleaned = cleaned.strip().strip(".")[:180].strip()
    return cleaned or fallback


def _safe_upload_content_type(value: Any) -> str:
    token = str(value or "application/octet-stream").replace("\r", "").replace("\n", "").strip()
    if not token or "/" not in token:
        return "application/octet-stream"
    return token[:120]


def _desktop_upload_staging_root() -> Path:
    return (aiask_agent_home().expanduser() / "desktop-upload-staging").resolve()


def _safe_remove_staged_root(path: str | Path | None) -> None:
    if not path:
        return
    root = _desktop_upload_staging_root()
    target = Path(path).expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return
    if target.parent != root:
        return
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


async def _stage_desktop_file_upload(
    files: list[UploadFile],
    *,
    session_id: str | None,
    thread_id: str | None,
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="files is required")
    staging_root = _desktop_upload_staging_root() / uuid4().hex
    staging_root.mkdir(parents=True, exist_ok=False)
    public_files: list[dict[str, Any]] = []
    staged_files: list[dict[str, Any]] = []
    try:
        for index, file in enumerate(files):
            filename = _safe_upload_filename(getattr(file, "filename", None), fallback=f"upload_{index}")
            content_type = _safe_upload_content_type(getattr(file, "content_type", None))
            content = await file.read()
            staged_path = staging_root / f"{index:03d}_{uuid4().hex}_{filename}"
            staged_path.write_bytes(content)
            file_meta = {
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(content),
            }
            public_files.append(dict(file_meta))
            staged_files.append({**file_meta, "path": str(staged_path)})
    except Exception:
        _safe_remove_staged_root(staging_root)
        raise

    fields = {
        key: value
        for key, value in {"session_id": session_id, "thread_id": thread_id}.items()
        if value not in {None, ""}
    }
    return {
        "fields": fields,
        "files": public_files,
        "_files": staged_files,
        "_staging_root": str(staging_root),
    }


def _raise_http_error(result: dict[str, Any]) -> None:
    detail = redact_audit_text(result.get("error") or "desktop asset request failed")
    error_code = str(result.get("error_code") or "")
    status = 400
    if error_code.endswith("NOT_FOUND"):
        status = 404
    elif error_code.endswith("UPSTREAM_UNAVAILABLE"):
        status = 503
    elif error_code.endswith("UNAUTHORIZED"):
        status = 401
    raise HTTPException(status_code=status, detail=detail)


def _empty_asset_list(user_id: str, resource: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "desktop.asset",
        "success": True,
        "data": [],
        "error": None,
        "error_code": None,
        "meta": {
            "user_id": user_id,
            "resource": resource,
            "degraded": True,
            "reason": result.get("error_code") or redact_audit_text(result.get("error") or "desktop_asset_empty"),
        },
        "secrets_redacted": True,
    }


def _is_missing_asset_collection(result: dict[str, Any]) -> bool:
    return str(result.get("error_code") or "") in {"DESKTOP_API_NOT_FOUND", "DESKTOP_API_UPSTREAM_UNAVAILABLE"}


def create_desktop_assets_router(
    *,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], None],
    require_full: Callable[[Request], Any],
    intent_store: Any,
    session_store: Any,
    desktop_asset_call: DesktopAssetCall,
    desktop_file_upload: DesktopFileUpload,
) -> APIRouter:
    router = APIRouter()

    def _asset_intent(
        *,
        request: Request,
        payload: dict[str, Any],
        action: str,
        params: dict[str, Any],
        object_name: str,
    ) -> dict[str, Any]:
        require_full(request)
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action=action,
            params=params,
            request_payload=payload,
            headers=request.headers,
            rationale=str(
                payload.get("rationale")
                or "Mutating Desktop financial workspace assets requires ActionIntent confirmation."
            ),
            source_chain=["aiask_agent.server", f"{action}.intent"],
            object_name=object_name,
        )

    @router.get("/v1/desktop/users/{user_id}/strategies")
    async def desktop_user_strategies(request: Request, user_id: str) -> dict[str, Any]:
        require_api(request)
        result = await desktop_asset_call("GET", f"/v1/users/{user_id}/strategies", None)
        if not result.get("success"):
            if _is_missing_asset_collection(result):
                return _empty_asset_list(user_id, "strategies", result)
            _raise_http_error(result)
        return result

    @router.post("/v1/desktop/users/{user_id}/strategies")
    async def desktop_user_strategy_create(request: Request, user_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.strategy_create",
            params={"user_id": user_id, "payload": payload},
            object_name="desktop_asset.strategy_create_intent",
        )

    @router.patch("/v1/desktop/users/{user_id}/strategies/{strategy_id}")
    async def desktop_user_strategy_update(request: Request, user_id: str, strategy_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.strategy_update",
            params={"user_id": user_id, "strategy_id": strategy_id, "payload": payload},
            object_name="desktop_asset.strategy_update_intent",
        )

    @router.delete("/v1/desktop/users/{user_id}/strategies/{strategy_id}")
    async def desktop_user_strategy_delete(request: Request, user_id: str, strategy_id: str) -> dict[str, Any]:
        return _asset_intent(
            request=request,
            payload={},
            action="desktop_asset.strategy_delete",
            params={"user_id": user_id, "strategy_id": strategy_id},
            object_name="desktop_asset.strategy_delete_intent",
        )

    @router.post("/v1/desktop/users/{user_id}/strategies/reorder")
    async def desktop_user_strategy_reorder(request: Request, user_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.strategy_reorder",
            params={"user_id": user_id, "payload": payload},
            object_name="desktop_asset.strategy_reorder_intent",
        )

    @router.get("/v1/desktop/users/{user_id}/stock-pools")
    async def desktop_user_stock_pools(request: Request, user_id: str) -> dict[str, Any]:
        require_api(request)
        result = await desktop_asset_call("GET", f"/v1/users/{user_id}/stock-pools", None)
        if not result.get("success"):
            if _is_missing_asset_collection(result):
                return _empty_asset_list(user_id, "stock_pools", result)
            _raise_http_error(result)
        return result

    @router.post("/v1/desktop/users/{user_id}/stock-pools")
    async def desktop_user_stock_pool_create(request: Request, user_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.stock_pool_create",
            params={"user_id": user_id, "payload": payload},
            object_name="desktop_asset.stock_pool_create_intent",
        )

    @router.patch("/v1/desktop/users/{user_id}/stock-pools/{pool_id}")
    async def desktop_user_stock_pool_update(request: Request, user_id: str, pool_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.stock_pool_update",
            params={"user_id": user_id, "pool_id": pool_id, "payload": payload},
            object_name="desktop_asset.stock_pool_update_intent",
        )

    @router.delete("/v1/desktop/users/{user_id}/stock-pools/{pool_id}")
    async def desktop_user_stock_pool_delete(request: Request, user_id: str, pool_id: str) -> dict[str, Any]:
        return _asset_intent(
            request=request,
            payload={},
            action="desktop_asset.stock_pool_delete",
            params={"user_id": user_id, "pool_id": pool_id},
            object_name="desktop_asset.stock_pool_delete_intent",
        )

    @router.post("/v1/desktop/users/{user_id}/stock-pools/reorder")
    async def desktop_user_stock_pool_reorder(request: Request, user_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.stock_pool_reorder",
            params={"user_id": user_id, "payload": payload},
            object_name="desktop_asset.stock_pool_reorder_intent",
        )

    @router.post("/v1/desktop/users/{user_id}/stock-pools/{pool_id}/stocks")
    async def desktop_user_stock_pool_add_stock(request: Request, user_id: str, pool_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.stock_pool_add_stock",
            params={"user_id": user_id, "pool_id": pool_id, "payload": payload},
            object_name="desktop_asset.stock_pool_add_stock_intent",
        )

    @router.delete("/v1/desktop/users/{user_id}/stock-pools/{pool_id}/stocks/{stock_code}")
    async def desktop_user_stock_pool_remove_stock(request: Request, user_id: str, pool_id: str, stock_code: str) -> dict[str, Any]:
        return _asset_intent(
            request=request,
            payload={},
            action="desktop_asset.stock_pool_remove_stock",
            params={"user_id": user_id, "pool_id": pool_id, "stock_code": stock_code},
            object_name="desktop_asset.stock_pool_remove_stock_intent",
        )

    @router.post("/v1/desktop/users/{user_id}/stock-pools/{pool_id}/stocks/batch-remove")
    async def desktop_user_stock_pool_batch_remove(request: Request, user_id: str, pool_id: str) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.stock_pool_batch_remove",
            params={"user_id": user_id, "pool_id": pool_id, "payload": payload},
            object_name="desktop_asset.stock_pool_batch_remove_intent",
        )

    @router.post("/v1/desktop/files/upload")
    async def desktop_files_upload(
        request: Request,
        files: list[UploadFile] = File(...),
        session_id: str | None = Form(None),
        thread_id: str | None = Form(None),
    ) -> dict[str, Any]:
        require_full(request)
        staged = await _stage_desktop_file_upload(
            files,
            session_id=session_id,
            thread_id=thread_id,
        )
        request_payload = {
            "session_id": session_id,
            "thread_id": thread_id,
            "files": staged["files"],
        }
        result = create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="desktop_asset.file_upload",
            params=staged,
            request_payload=request_payload,
            headers=request.headers,
            rationale="Uploading Desktop financial workspace files requires ActionIntent confirmation.",
            source_chain=["aiask_agent.server", "desktop_asset.file_upload.intent"],
            object_name="desktop_asset.file_upload_intent",
        )
        if not result.get("success"):
            _safe_remove_staged_root(staged.get("_staging_root"))
        return result

    @router.get("/v1/desktop/files")
    async def desktop_files_list(request: Request, user_id: str = "default") -> dict[str, Any]:
        require_api(request)
        result = await desktop_asset_call("GET", f"/v1/files?user_id={user_id}", None)
        if not result.get("success"):
            _raise_http_error(result)
        return result

    @router.post("/v1/desktop/files/save")
    async def desktop_files_save(request: Request) -> dict[str, Any]:
        payload = dict(await request.json() or {})
        return _asset_intent(
            request=request,
            payload=payload,
            action="desktop_asset.file_save",
            params={"payload": payload},
            object_name="desktop_asset.file_save_intent",
        )

    @router.delete("/v1/desktop/files/{file_id}")
    async def desktop_files_delete(request: Request, file_id: str) -> dict[str, Any]:
        return _asset_intent(
            request=request,
            payload={},
            action="desktop_asset.file_delete",
            params={"file_id": file_id},
            object_name="desktop_asset.file_delete_intent",
        )

    @router.post("/v1/sessions/batch/archive")
    async def desktop_sessions_batch_archive(request: Request) -> dict[str, Any]:
        require_control(request)
        payload = dict(await request.json() or {})
        session_ids = [str(item).strip() for item in list(payload.get("session_ids") or []) if str(item).strip()]
        if not session_ids:
            raise HTTPException(400, detail="session_ids is required")

        results = []
        for session_id in session_ids:
            try:
                archived = session_store.set_session_archived(
                    session_id,
                    archived=True,
                    reason=str(payload.get("reason") or "desktop batch archive"),
                    actor=str(payload.get("actor") or payload.get("user_id") or "control_token"),
                )
                results.append({"session_id": session_id, "success": True, "data": archived})
            except FileNotFoundError:
                results.append({"session_id": session_id, "success": False, "error": "session_not_found", "error_code": "SESSION_NOT_FOUND"})
            except Exception as exc:
                results.append({"session_id": session_id, "success": False, "error": redact_audit_text(exc), "error_code": "SESSION_ARCHIVE_FAILED"})

        return {
            "object": "aiask.session_archive_batch",
            "success": True,
            "data": {
                "results": results,
                "archived_count": sum(1 for item in results if item.get("success")),
                "failed_count": sum(1 for item in results if not item.get("success")),
            },
            "error": None,
            "error_code": None,
            "secrets_redacted": True,
        }

    return router
