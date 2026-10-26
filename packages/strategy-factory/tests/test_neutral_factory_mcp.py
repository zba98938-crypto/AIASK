"""The factory MCP server is owned here, and its tool surface is protocol-tested.

This used to assert only that ``module.mcp`` was not None, which is why the
duplicate server in akshare-mcp could drift: the sole real protocol test lived in
``packages/agent`` and drove the *deprecated* copy. The duplicate is gone now, so
the contract is pinned against the canonical module over a real stdio session.

The exact tool set is asserted deliberately. Adding a write tool must fail here
as a set mismatch, so the read-only surface cannot grow silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


READ_ONLY_TOOLS = {
    "factory_list",
    "factory_readiness_read",
    "factory_run_read",
    "factory_runs_read",
    "factory_status_read",
    "factory_tradeability_certificates_read",
}

# tests/ -> strategy-factory/ -> packages/ -> <aiask root>
AIASK_ROOT = Path(__file__).resolve().parents[3]
SOURCE_PATHS = (
    AIASK_ROOT / "packages" / "strategy-factory" / "src",
    AIASK_ROOT / "packages" / "aiask-quant-core" / "src",
)


def _env(tmp_path: Path) -> dict[str, str]:
    runtime_home = tmp_path / "runtime"
    env = os.environ.copy()
    env.update(
        {
            "AIASK_RUNTIME_HOME": str(runtime_home),
            "AIASK_PROJECT_ROOT": str(AIASK_ROOT),
            "AIASK_SQLITE_PATH": str(runtime_home / "data" / "factory.sqlite3"),
            "PYTHONPATH": os.pathsep.join(
                [*(str(path) for path in SOURCE_PATHS), env.get("PYTHONPATH", "")]
            ),
        }
    )
    return env


def _payload(result) -> dict:
    """Read a tool result across MCP 1.x camelCase and 2.x snake_case wires."""
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    return structured or json.loads(result.content[0].text)


async def _probe(env: dict[str, str]) -> dict[str, object]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "strategy_factory.mcp_server"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {item.name for item in tools.tools}

            # Each call is captured rather than awaited bare: if a tool is renamed
            # or removed, the tool-set assertion below should be what fails, not
            # this shared fixture taking every other assertion down with it.
            async def call(tool: str, arguments: dict) -> object:
                if tool not in names:
                    return None
                try:
                    return _payload(await session.call_tool(tool, arguments))
                except Exception as exc:  # surfaced by the test that reads it
                    return exc

            catalog = await call("factory_list", {})
            readiness = await call("factory_readiness_read", {})
            missing = await call("factory_run_read", {"run_id": "factory_run_absent"})
            resource = await session.read_resource("resource://factories/readiness")
            return {
                "tools": names,
                "catalog": catalog,
                "readiness": readiness,
                "missing": missing,
                "resource": json.loads(resource.contents[0].text),
            }


@pytest.fixture(scope="module")
def probe(tmp_path_factory) -> dict[str, object]:
    """One stdio session shared by the assertions below; spawning is the slow part."""
    return asyncio.run(_probe(_env(tmp_path_factory.mktemp("factory-mcp"))))


def test_the_server_exposes_exactly_the_read_only_tools(probe) -> None:
    assert probe["tools"] == READ_ONLY_TOOLS


def test_the_catalog_names_the_four_factories(probe) -> None:
    catalog = probe["catalog"]
    assert catalog["contract_version"] == "aiask.factory_run.v1"
    assert {item["factory"] for item in catalog["factories"]} == {
        "strategy_factory",
        "factor_mining",
        "incubation",
        "market_event_ingest",
    }
    assert catalog["read_only"] is True


def test_readiness_reports_every_factory_and_stays_read_only(probe) -> None:
    readiness = probe["readiness"]
    assert readiness["read_only"] is True
    assert {check["factory"] for check in readiness["checks"]} == {
        "strategy_factory",
        "factor_mining",
        "incubation",
        "market_event_ingest",
    }


def test_an_unknown_run_id_reports_not_found_instead_of_failing(probe) -> None:
    missing = probe["missing"]
    assert missing["found"] is False
    assert missing["run"] is None


def test_the_readiness_resource_is_readable_and_read_only(probe) -> None:
    assert probe["resource"]["read_only"] is True


def test_the_module_declares_the_run_contract_version() -> None:
    import strategy_factory.mcp_server as module

    assert module.READ_ONLY_META["contract_version"] == "aiask.factory_run.v1"
    assert module.READ_ONLY_META["side_effect"]["level"] == "read_only"
