from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..session_store_utils import redact_audit_text


def create_jobs_router(
    *,
    require_api: Callable[[Request], None],
    require_control: Callable[[Request], None],
    job_store: Any,
    scheduler: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/jobs")
    async def jobs(request: Request) -> dict[str, Any]:
        require_api(request)
        return {"object": "list", "data": job_store.list()}

    @router.get("/v1/jobs/{job_id}/runs")
    async def job_runs(request: Request, job_id: str, limit: int = 100) -> dict[str, Any]:
        require_api(request)
        return {"object": "list", "job_id": job_id, "data": job_store.list_runs(job_id, limit=limit)}

    @router.post("/v1/jobs", status_code=201)
    async def job_create(request: Request) -> dict[str, Any]:
        require_control(request)
        payload = await request.json()
        try:
            job = job_store.create(
                name=str(payload.get("name") or ""),
                prompt=str(payload.get("prompt") or ""),
                schedule=payload.get("schedule"),
                interval_seconds=payload.get("interval_seconds"),
                toolset=str(payload.get("toolset") or "finance_safe"),
                enabled=bool(payload.get("enabled", True)),
                payload={key: payload.get(key) for key in ("script", "skills", "silent_pattern") if key in payload},
            )
        except Exception as exc:
            raise HTTPException(400, detail=redact_audit_text(exc)) from exc
        return {"object": "job", **job}

    @router.patch("/v1/jobs/{job_id}")
    async def job_update(request: Request, job_id: str) -> dict[str, Any]:
        require_control(request)
        payload = await request.json()
        job = job_store.update(job_id, **dict(payload or {}))
        if not job:
            raise HTTPException(404, detail=f"job not found: {job_id}")
        return {"object": "job", **job}

    @router.delete("/v1/jobs/{job_id}")
    async def job_delete(request: Request, job_id: str) -> dict[str, Any]:
        require_control(request)
        return {"id": job_id, "object": "job.deleted", "deleted": job_store.delete(job_id)}

    @router.post("/v1/jobs/{job_id}/run")
    async def job_run(request: Request, job_id: str) -> dict[str, Any]:
        require_control(request)
        result = await scheduler.run_job(job_id)
        if not result.get("success"):
            raise HTTPException(404, detail=redact_audit_text(result.get("error") or "job run failed"))
        return result

    return router
