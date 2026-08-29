from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request


def create_connectors_router(
    *,
    require_full: Callable[[Request], Any],
    connector_manager_factory: Callable[..., Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/connectors")
    async def connectors_list_api(request: Request, type: str | None = None, category: str | None = None) -> dict[str, Any]:
        require_full(request)
        connectors = connector_manager_factory(include_daemon=True).list_all()
        if type:
            connectors = [connector for connector in connectors if connector.type == type]
        if category:
            connectors = [connector for connector in connectors if connector.category == category]
        return {"object": "list", "data": [asdict(connector) for connector in connectors]}

    @router.get("/v1/connectors/summary")
    async def connectors_summary_api(request: Request) -> dict[str, Any]:
        require_full(request)
        return {"object": "connector.summary", "data": connector_manager_factory().summary()}

    @router.get("/v1/connectors/{connector_type}/{name}")
    async def connector_detail_api(request: Request, connector_type: str, name: str) -> dict[str, Any]:
        require_full(request)
        connector = connector_manager_factory().get(f"{connector_type}:{name}")
        if connector is None:
            raise HTTPException(status_code=404, detail=f"Connector not found: {connector_type}/{name}")
        return {"object": "connector.detail", "data": asdict(connector)}

    @router.post("/v1/connectors/{connector_type}/{name}/test")
    async def connector_test_api(request: Request, connector_type: str, name: str) -> dict[str, Any]:
        require_full(request)
        connector = connector_manager_factory().get(f"{connector_type}:{name}")
        if connector is None:
            raise HTTPException(status_code=404, detail=f"Connector not found: {connector_type}/{name}")
        result = {
            "connector_id": f"{connector_type}:{name}",
            "configured": connector.configured,
            "connected": connector.connected,
            "status": connector.status,
            "missing_env": connector.missing_env,
        }
        return {"object": "connector.test", "data": result}

    return router
