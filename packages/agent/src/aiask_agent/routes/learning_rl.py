from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request

from .action_intents import create_external_action_intent_payload


def create_learning_rl_router(
    *,
    require_full: Callable[[Request], Any],
    intent_store: Any,
    session_store: Any,
    learning_loop_factory: Callable[[], Any],
    rl_manager_factory: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/learning/status")
    async def learning_status_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return learning_loop_factory().status()

    @router.get("/v1/learning/review")
    async def learning_review_api(request: Request, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": learning_loop_factory().review(status=status, limit=limit)}

    @router.post("/v1/learning/apply")
    async def learning_apply_api(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        proposal_id = str(payload.get("proposal_id") or "")
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="learning.apply",
            params={"proposal_id": proposal_id},
            request_payload={**payload, "proposal_id": proposal_id},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Applying a learning proposal can write memory or skills and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "learning.apply.intent"],
            object_name="learning.apply_intent",
        )

    @router.get("/v1/rl/environments")
    async def rl_environments_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": rl_manager_factory().list_environments()}

    @router.get("/v1/rl/config")
    async def rl_config_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return {"object": "rl.config", "data": rl_manager_factory().current_config()}

    @router.patch("/v1/rl/config")
    async def rl_config_patch_api(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        config = dict(payload.get("config") or payload.get("patch") or payload)
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="rl.edit_config",
            params={"config": config},
            request_payload={**payload, "config": config},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Editing RL configuration changes durable training state and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "rl.edit_config.intent"],
            object_name="rl.edit_config_intent",
        )

    @router.get("/v1/rl/runs")
    async def rl_runs_api(request: Request, limit: int = 100) -> dict[str, Any]:
        require_full(request)
        return {"object": "list", "data": rl_manager_factory().list_runs(limit=limit)}

    @router.post("/v1/rl/runs")
    async def rl_run_create_api(request: Request) -> dict[str, Any]:
        require_full(request)
        payload = dict(await request.json() or {})
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="rl.start_training",
            params={
                "environment": payload.get("environment"),
                "config": dict(payload.get("config") or {}),
            },
            request_payload=payload,
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Starting RL training can launch external processes and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "rl.start_training.intent"],
            object_name="rl.start_training_intent",
        )

    @router.get("/v1/rl/runs/{run_id}")
    async def rl_run_get_api(request: Request, run_id: str) -> dict[str, Any]:
        require_full(request)
        return {"object": "rl.run", "data": rl_manager_factory().check_status(run_id)}

    @router.post("/v1/rl/runs/{run_id}/stop")
    async def rl_run_stop_api(request: Request, run_id: str) -> dict[str, Any]:
        require_full(request)
        raw = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        payload = dict(raw or {}) if isinstance(raw, dict) else {}
        return create_external_action_intent_payload(
            intent_store=intent_store,
            session_store=session_store,
            action="rl.stop_training",
            params={"run_id": run_id},
            request_payload={**payload, "run_id": run_id},
            headers=request.headers,
            rationale=str(payload.get("rationale") or "Stopping RL training controls a managed process and requires ActionIntent confirmation."),
            source_chain=["aiask_agent.server", "rl.stop_training.intent"],
            object_name="rl.stop_training_intent",
        )

    @router.get("/v1/rl/runs/{run_id}/results")
    async def rl_run_results_api(request: Request, run_id: str) -> dict[str, Any]:
        require_full(request)
        return {"object": "rl.results", "data": rl_manager_factory().results(run_id)}

    @router.get("/v1/rl/runs/{run_id}/logs")
    async def rl_run_logs_api(request: Request, run_id: str, max_bytes: int = 65536, tail: bool = True) -> dict[str, Any]:
        require_full(request)
        return {"object": "rl.logs", "data": rl_manager_factory().logs(run_id, max_bytes=max_bytes, tail=tail)}

    return router
