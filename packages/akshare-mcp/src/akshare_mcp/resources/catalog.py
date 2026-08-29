"""Catalog resources for AI-facing MCP discovery."""

from __future__ import annotations

from typing import Any

from ..tools.search import _infer_tool_category, _is_hidden_tool, _iter_registered_tools
from ..tools.tool_catalog import get_tool_contract, get_workflow_guide, list_tool_contracts


def build_server_capabilities_payload(mcp) -> dict[str, Any]:
    tool_names = sorted(
        str(name)
        for name, tool in _iter_registered_tools(mcp)
        if name and not _is_hidden_tool(str(name), tool)
    )
    prompt_names = sorted(prompt.name for prompt in mcp._prompt_manager.list_prompts())
    resource_templates = sorted(
        template.uri_template for template in mcp._resource_manager.list_templates()
    )
    resource_uris = sorted(resource.uri for resource in mcp._resource_manager.list_resources())
    contracts = list_tool_contracts()
    workflow_tools = [
        str(item.get("name") or "")
        for item in contracts
        if "workflow" in list(item.get("tags") or [])
    ]
    return {
        "server": "AKShare Stock Data Server v2",
        "tools": {
            "count": len(tool_names),
            "sample": tool_names[:24],
            "workflow_tools": workflow_tools,
        },
        "resources": {
            "count": len(resource_uris),
            "uris": resource_uris,
        },
        "resource_templates": {
            "count": len(resource_templates),
            "uris": resource_templates,
        },
        "prompts": {
            "count": len(prompt_names),
            "names": prompt_names,
        },
        "ai_catalog": {
            "contract_version": "ai_tool_contract_v1",
            "tool_contract_count": len(contracts),
            "recommended_entrypoints": workflow_tools,
        },
    }


def build_tool_catalog_payload(mcp) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for name, tool in sorted(_iter_registered_tools(mcp), key=lambda item: str(item[0] or "")):
        if not name or _is_hidden_tool(str(name), tool):
            continue
        contract = get_tool_contract(str(name))
        description = getattr(tool, "description", None)
        if not description:
            fn = getattr(tool, "fn", None)
            description = getattr(fn, "__doc__", None) if fn else None
        rows.append(
            {
                "name": str(name),
                "category": _infer_tool_category(str(name), tool),
                "description": str(description or "").strip() or None,
                "contract": contract,
            }
        )
    return {
        "contract_version": "ai_tool_contract_v1",
        "count": len(rows),
        "tools": rows,
    }


def register(mcp) -> None:
    @mcp.resource(
        "resource://server/capabilities",
        name="server_capabilities",
        title="Server Capabilities",
        description="Runtime MCP capabilities and implemented native objects",
        mime_type="application/json",
    )
    async def server_capabilities() -> dict[str, Any]:
        return build_server_capabilities_payload(mcp)

    @mcp.resource(
        "resource://server/tool-catalog",
        name="tool_catalog",
        title="Tool Catalog",
        description="AI-facing tool catalog with contracts, examples, output schema and side-effect levels",
        mime_type="application/json",
    )
    async def tool_catalog() -> dict[str, Any]:
        return build_tool_catalog_payload(mcp)

    @mcp.resource(
        "resource://workflow/{name}/guide",
        name="workflow_guide",
        title="Workflow Guide",
        description="Guide for a recommended AI-facing workflow surface",
        mime_type="application/json",
    )
    async def workflow_guide(name: str) -> dict[str, Any]:
        guide = get_workflow_guide(name)
        if guide is not None:
            return guide
        return {
            "name": str(name or "").strip(),
            "found": False,
            "error": f"workflow guide not found: {name}",
            "available": sorted(["factor-governance", "stock-analysis", "stock-deep-analysis", "strategy-promotion"]),
        }
