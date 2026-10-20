"""A bad tool argument must produce an error envelope, not kill the server.

Handlers parse numbers before their own try, so an argument of the wrong JSON
type used to propagate out of `_handle_jsonrpc`, out of the stdio loop, and
terminate the process with no response at all.
"""

from __future__ import annotations

import importlib
import json

import pytest

SERVER_MODULES = [
    "aiask_finance_mcp.eastmoney.server",
    "aiask_finance_mcp.tongdaxin.server",
    "aiask_finance_mcp.tonghuashun.server",
    "aiask_finance_mcp.qmt.server",
]


def _boom(_arguments):
    raise ValueError("invalid literal for int() with base 10: 'twenty'")


@pytest.mark.parametrize("module_name", SERVER_MODULES)
def test_handler_exception_becomes_an_error_envelope(module_name: str) -> None:
    module = importlib.import_module(module_name)
    module.TOOL_HANDLERS["_injected_boom"] = _boom

    response = module._handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "_injected_boom", "arguments": {"limit": "twenty"}},
        }
    )

    assert response is not None
    assert response["id"] == 7
    result = response["result"]
    assert result["isError"] is True
    body = json.loads(result["content"][0]["text"])
    assert body["success"] is False
    assert body["error_code"] == "TOOL_EXECUTION_FAILED"
    assert body["tool"] == "_injected_boom"
    assert "invalid literal for int()" in body["error"]


@pytest.mark.parametrize("module_name", SERVER_MODULES)
def test_unknown_tool_is_an_error_envelope_and_not_a_crash(module_name: str) -> None:
    module = importlib.import_module(module_name)
    response = module._handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "definitely_not_a_tool", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
