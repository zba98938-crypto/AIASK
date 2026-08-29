from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import build_default_tool_registry
from aiask_agent.tools.policy import ToolPolicy, ToolPolicyEngine


def _write_minimal_curated_graph(root: Path) -> Path:
    curated = root / "curated"
    curated.mkdir(parents=True)
    nodes = [
        {
            "id": "services_aiaskapi_aiaskapi",
            "label": "AiaskApi",
            "file_type": "code",
            "source_file": "desktop/src/services/aiaskApi.ts",
            "source_location": "L62",
        },
        {
            "id": "packages_agent_src_aiask_agent_server_py",
            "label": "server.py",
            "file_type": "code",
            "source_file": "packages/agent/src/aiask_agent/server.py",
            "source_location": "L1",
        },
        {
            "id": "endpoint_v1_mcp_servers",
            "label": "/v1/mcp/servers",
            "file_type": "endpoint",
            "source_file": "aiask:endpoints",
            "source_location": None,
        },
    ]
    links = [
        {
            "source": "services_aiaskapi_aiaskapi",
            "target": "endpoint_v1_mcp_servers",
            "relation": "calls",
            "endpoint_relation": "calls_endpoint",
            "confidence": "EXTRACTED",
            "source_file": "desktop/src/services/aiaskApi.ts",
            "source_location": "L185",
        },
        {
            "source": "packages_agent_src_aiask_agent_server_py",
            "target": "endpoint_v1_mcp_servers",
            "relation": "calls",
            "endpoint_relation": "serves_endpoint",
            "confidence": "EXTRACTED",
            "source_file": "packages/agent/src/aiask_agent/server.py",
            "source_location": "L2209",
        },
    ]
    (curated / "core.graph.json").write_text(
        json.dumps({"directed": True, "nodes": nodes, "links": links}, indent=2),
        encoding="utf-8",
    )
    (curated / "endpoint-map.json").write_text(
        json.dumps(
            {
                "endpoint_count": 1,
                "matched_count": 1,
                "server_only_count": 0,
                "desktop_only_count": 0,
                "endpoints": [
                    {
                        "path": "/v1/mcp/servers",
                        "server": [{"method": "GET", "handler": "mcp_servers", "line": 2209}],
                        "desktop": [
                            {
                                "source_file": "desktop/src/services/aiaskApi.ts",
                                "line": 185,
                                "literal": "/v1/mcp/servers?all=true",
                            }
                        ],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (curated / "CURATED_SUMMARY.json").write_text(
        json.dumps(
            {
                "original": {"nodes": 3, "edges": 2},
                "core": {"nodes": 3, "edges": 2},
                "packages": {"agent": {"nodes": 1, "edges": 1}, "desktop": {"nodes": 1, "edges": 1}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return curated


def test_code_graph_tool_is_general_full_only(tmp_path) -> None:
    finance = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "finance.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("finance_safe", False, (str(tmp_path),))),
    )
    assert "agent_code_graph_query" not in finance.names()

    general = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "general.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )
    assert "agent_code_graph_query" in general.names()
    catalog_item = next(item for item in general.catalog if item["name"] == "agent_code_graph_query")
    assert catalog_item["category"] == "general_read"
    assert catalog_item["side_effect"] == "read_only"


def test_code_graph_tool_queries_minimal_curated_graph(tmp_path) -> None:
    curated = _write_minimal_curated_graph(tmp_path / "graph")
    registry = build_default_tool_registry(
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        policy_engine=ToolPolicyEngine(ToolPolicy("general_full", True, (str(tmp_path),))),
    )

    summary = asyncio.run(
        registry.call_tool("agent_code_graph_query", {"action": "summary", "graph_dir": str(curated)})
    )
    assert summary["success"] is True
    assert summary["data"]["core"] == {"nodes": 3, "edges": 2}
    assert summary["meta"]["side_effect"]["level"] == "read_only"

    endpoint = asyncio.run(
        registry.call_tool(
            "agent_code_graph_query",
            {"action": "endpoint", "query": "/v1/mcp/servers", "graph_dir": str(curated)},
        )
    )
    assert endpoint["success"] is True
    assert endpoint["data"]["matches"][0]["server"][0]["handler"] == "mcp_servers"

    explained = asyncio.run(
        registry.call_tool(
            "agent_code_graph_query",
            {"action": "explain", "node": "endpoint_v1_mcp_servers", "graph_dir": str(curated), "limit": 5},
        )
    )
    assert explained["success"] is True
    neighbor_labels = {item["node"]["label"] for item in explained["data"]["neighbors"]}
    assert {"AiaskApi", "server.py"} <= neighbor_labels

    affected = asyncio.run(
        registry.call_tool(
            "agent_code_graph_query",
            {"action": "affected", "node": "/v1/mcp/servers", "graph_dir": str(curated), "depth": 1},
        )
    )
    assert affected["success"] is True
    affected_labels = {item["node"]["label"] for item in affected["data"]["nodes"]}
    assert {"AiaskApi", "server.py"} <= affected_labels
