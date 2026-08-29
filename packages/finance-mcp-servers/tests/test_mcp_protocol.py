"""Tests for finance MCP servers — protocol compliance."""

from __future__ import annotations

import json
from typing import Any

import pytest


def _call_jsonrpc(handler, method: str, params: dict | None = None, req_id: int = 1) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        request["params"] = params
    return handler(request)


def _assert_resource_prompt_capabilities(result: dict[str, Any]) -> None:
    capabilities = result["result"]["capabilities"]
    assert capabilities["tools"]["listChanged"] is False
    assert capabilities["resources"]["listChanged"] is False
    assert capabilities["prompts"]["listChanged"] is False


def _assert_empty_resources_and_prompts(handler) -> None:
    resources = _call_jsonrpc(handler, "resources/list")
    prompts = _call_jsonrpc(handler, "prompts/list")
    assert resources["result"]["resources"] == []
    assert prompts["result"]["prompts"] == []


def _jsonrpc_tool_content(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["result"]["content"][0]["text"])


def _assert_trade_risk_side_effect(envelope: dict[str, Any]) -> None:
    side_effect = envelope["meta"]["side_effect"]
    assert side_effect["level"] == "trade_risk"
    assert side_effect["confirmation_required"] is True
    assert side_effect["explicit_token_required"] is True
    assert side_effect["idempotent"] is False


@pytest.mark.parametrize(
    ("module_name", "tool_name"),
    [
        ("aiask_finance_mcp.tongdaxin.server", "tdx_realtime_quote"),
        ("aiask_finance_mcp.eastmoney.server", "em_realtime_quote"),
        ("aiask_finance_mcp.tonghuashun.server", "ths_query_balance"),
        ("aiask_finance_mcp.qmt.server", "qmt_query_account"),
    ],
)
def test_mcp_public_envelopes_redact_error_text_and_data(module_name, tool_name) -> None:
    module = __import__(module_name, fromlist=["_envelope"])

    envelope = module._envelope(
        False,
        data={
            "broker_token": "mcp-data-broker-secret",
            "nested": {
                "accessToken": "mcp-data-access-secret",
                "url": "https://broker.test/callback?access_token=mcp-query-secret",
            },
        },
        error=(
            "failed password=mcp-error-password "
            "Authorization: Bearer mcp-error-bearer "
            "broker_token=mcp-error-broker"
        ),
        tool=tool_name,
    )
    raw = json.dumps(envelope, ensure_ascii=False)

    assert "mcp-data-broker-secret" not in raw
    assert "mcp-data-access-secret" not in raw
    assert "mcp-query-secret" not in raw
    assert "mcp-error-password" not in raw
    assert "mcp-error-bearer" not in raw
    assert "mcp-error-broker" not in raw
    assert envelope["data"]["broker_token"] == "[redacted]"
    assert envelope["data"]["nested"]["accessToken"] == "[redacted]"
    assert "password=[redacted]" in envelope["error"]
    assert "Authorization: Bearer [redacted]" in envelope["error"]
    assert "broker_token=[redacted]" in envelope["error"]


@pytest.mark.parametrize(
    "module_name",
    [
        "aiask_finance_mcp.tongdaxin.server",
        "aiask_finance_mcp.eastmoney.server",
        "aiask_finance_mcp.tonghuashun.server",
        "aiask_finance_mcp.qmt.server",
    ],
)
def test_mcp_jsonrpc_errors_redact_secret_text(module_name) -> None:
    module = __import__(module_name, fromlist=["_handle_jsonrpc"])

    unknown_tool = _call_jsonrpc(
        module._handle_jsonrpc,
        "tools/call",
        {"name": "missing broker_token=unknown-tool-secret", "arguments": {}},
    )
    unknown_method = _call_jsonrpc(
        module._handle_jsonrpc,
        "missing password=unknown-method-secret Authorization: Bearer unknown-method-bearer",
    )
    raw = json.dumps({"tool": unknown_tool, "method": unknown_method}, ensure_ascii=False)

    assert "unknown-tool-secret" not in raw
    assert "unknown-method-secret" not in raw
    assert "unknown-method-bearer" not in raw
    assert _jsonrpc_tool_content(unknown_tool)["error"] == "Unknown tool: missing broker_token=[redacted]"
    assert unknown_method["error"]["message"] == "Method not found: missing password=[redacted] Authorization: Bearer [redacted]"


@pytest.mark.parametrize(
    ("module_name", "query_tool", "live_tool"),
    [
        ("aiask_finance_mcp.tonghuashun.server", "ths_query_orders", "ths_place_order"),
        ("aiask_finance_mcp.qmt.server", "qmt_query_orders", "qmt_place_order"),
    ],
)
def test_broker_order_query_is_read_only_but_live_order_is_trade_risk(module_name, query_tool, live_tool) -> None:
    module = __import__(module_name, fromlist=["_envelope"])

    assert module._envelope(True, data={}, tool=query_tool)["meta"]["side_effect"] == "read_only"
    _assert_trade_risk_side_effect(module._envelope(True, data={}, tool=live_tool))


class TestTongdaxinProtocol:
    def test_initialize(self):
        from aiask_finance_mcp.tongdaxin.server import _handle_jsonrpc

        result = _call_jsonrpc(_handle_jsonrpc, "initialize")
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == 1
        assert result["result"]["protocolVersion"] == "2024-11-05"
        assert result["result"]["serverInfo"]["name"] == "aiask-finance-tongdaxin"
        _assert_resource_prompt_capabilities(result)

    def test_tools_list(self):
        from aiask_finance_mcp.tongdaxin.server import _handle_jsonrpc

        result = _call_jsonrpc(_handle_jsonrpc, "tools/list")
        tools = result["result"]["tools"]
        assert len(tools) >= 6
        tool_names = [t["name"] for t in tools]
        assert "tdx_realtime_quote" in tool_names
        assert "tdx_kline_history" in tool_names
        assert "tdx_minute_data" in tool_names
        assert "tdx_tick_data" in tool_names
        assert "tdx_finance_info" in tool_names
        assert "tdx_market_snapshot" in tool_names

    def test_resources_and_prompts_list(self):
        from aiask_finance_mcp.tongdaxin.server import _handle_jsonrpc

        _assert_empty_resources_and_prompts(_handle_jsonrpc)

    def test_tools_call_missing_code(self):
        from aiask_finance_mcp.tongdaxin.server import _handle_jsonrpc

        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {"name": "tdx_realtime_quote", "arguments": {}},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        assert content["success"] is False
        assert "codes" in content["error"].lower() or "required" in content["error"].lower()

    def test_tools_call_unknown_tool(self):
        from aiask_finance_mcp.tongdaxin.server import _handle_jsonrpc

        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {"name": "nonexistent_tool", "arguments": {}},
        )
        assert result["result"]["isError"] is True

    def test_unknown_method(self):
        from aiask_finance_mcp.tongdaxin.server import _handle_jsonrpc

        result = _call_jsonrpc(_handle_jsonrpc, "unknown/method")
        assert "error" in result
        assert result["error"]["code"] == -32601

    def test_notification_returns_none(self):
        from aiask_finance_mcp.tongdaxin.server import _handle_jsonrpc

        result = _handle_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert result is None


class TestTonghuashunProtocol:
    def test_initialize(self):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        result = _call_jsonrpc(_handle_jsonrpc, "initialize")
        assert result["result"]["serverInfo"]["name"] == "aiask-finance-tonghuashun"
        _assert_resource_prompt_capabilities(result)

    def test_tools_list_default_hides_live_tools(self, monkeypatch):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        monkeypatch.delenv("AIASK_FINANCE_ENABLE_LIVE_TOOLS", raising=False)
        monkeypatch.delenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", raising=False)
        result = _call_jsonrpc(_handle_jsonrpc, "tools/list")
        tools = result["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "ths_query_balance" in tool_names
        assert "ths_query_position" in tool_names
        assert "ths_place_order" not in tool_names
        assert "ths_cancel_order" not in tool_names

    def test_tools_list_live_tools_require_explicit_opt_in(self, monkeypatch):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        monkeypatch.setenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", "1")
        result = _call_jsonrpc(_handle_jsonrpc, "tools/list")
        tool_names = [t["name"] for t in result["result"]["tools"]]
        assert "ths_place_order" in tool_names
        assert "ths_cancel_order" in tool_names

    def test_resources_and_prompts_list(self):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        _assert_empty_resources_and_prompts(_handle_jsonrpc)

    def test_place_order_validation(self):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        # Missing required fields
        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {"name": "ths_place_order", "arguments": {"code": "600519"}},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        assert content["success"] is False
        assert "disabled by default" in content["error"]

    def test_live_tool_call_rejects_before_token_when_default_disabled(self, monkeypatch):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        monkeypatch.delenv("AIASK_FINANCE_ENABLE_LIVE_TOOLS", raising=False)
        monkeypatch.delenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", raising=False)
        monkeypatch.setenv("AIASK_FINANCE_THS_BROKER_TOKEN", "test-token")
        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {
                "name": "ths_place_order",
                "arguments": {
                    "code": "600519",
                    "price": 100.0,
                    "amount": 100,
                    "direction": "buy",
                    "broker_token": "test-token",
                },
            },
        )
        content = json.loads(result["result"]["content"][0]["text"])
        assert result["result"]["isError"] is True
        assert content["success"] is False
        assert "disabled by default" in content["error"]

    def test_cancel_order_validation(self, monkeypatch):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        monkeypatch.setenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.setenv("AIASK_FINANCE_THS_BROKER_TOKEN", "test-token")
        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {"name": "ths_cancel_order", "arguments": {"broker_token": "test-token"}},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        assert content["success"] is False
        assert "entrust_no" in content["error"].lower()
        _assert_trade_risk_side_effect(content)

    def test_place_order_success_keeps_trade_risk_metadata(self, monkeypatch):
        from aiask_finance_mcp.tonghuashun import server

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def place_order(self, *, code: str, price: float, amount: int, direction: str) -> dict[str, Any]:
                self.calls.append({"code": code, "price": price, "amount": amount, "direction": direction})
                return {"entrust_no": "THS-1", "broker_token": "must-not-leak"}

        fake_client = FakeClient()
        monkeypatch.setenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.setenv("AIASK_FINANCE_THS_BROKER_TOKEN", "expected-token")
        monkeypatch.setattr(server, "_get_client", lambda: fake_client)

        result = _call_jsonrpc(
            server._handle_jsonrpc,
            "tools/call",
            {
                "name": "ths_place_order",
                "arguments": {
                    "code": "600519",
                    "price": 100.0,
                    "amount": 100,
                    "direction": "buy",
                    "broker_token": "expected-token",
                },
            },
        )
        content = _jsonrpc_tool_content(result)

        assert result["result"]["isError"] is False
        assert content["success"] is True
        assert fake_client.calls == [{"code": "600519", "price": 100.0, "amount": 100, "direction": "buy"}]
        assert content["data"]["order"]["broker_token"] == "[redacted]"
        _assert_trade_risk_side_effect(content)

    def test_cancel_order_success_keeps_trade_risk_metadata(self, monkeypatch):
        from aiask_finance_mcp.tonghuashun import server

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def cancel_order(self, *, entrust_no: str) -> dict[str, Any]:
                self.calls.append(entrust_no)
                return {"cancelled": True}

        fake_client = FakeClient()
        monkeypatch.setenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.setenv("AIASK_FINANCE_THS_BROKER_TOKEN", "expected-token")
        monkeypatch.setattr(server, "_get_client", lambda: fake_client)

        result = _call_jsonrpc(
            server._handle_jsonrpc,
            "tools/call",
            {"name": "ths_cancel_order", "arguments": {"entrust_no": "E123", "broker_token": "expected-token"}},
        )
        content = _jsonrpc_tool_content(result)

        assert result["result"]["isError"] is False
        assert content["success"] is True
        assert fake_client.calls == ["E123"]
        _assert_trade_risk_side_effect(content)

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("ths_place_order", {"code": "600519", "price": 100.0, "amount": 100, "direction": "buy"}),
            ("ths_cancel_order", {"entrust_no": "E123"}),
        ],
    )
    def test_trade_risk_tools_reject_when_server_token_unset(self, monkeypatch, tool_name, arguments):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        monkeypatch.setenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.delenv("AIASK_FINANCE_THS_BROKER_TOKEN", raising=False)
        result = _call_jsonrpc(_handle_jsonrpc, "tools/call", {"name": tool_name, "arguments": {**arguments, "broker_token": "anything"}})
        content = json.loads(result["result"]["content"][0]["text"])

        assert result["result"]["isError"] is True
        assert content["success"] is False
        assert content["error_code"] == "TRADE_RISK_TOKEN_REQUIRED"
        assert content["meta"]["side_effect"]["level"] == "trade_risk"
        assert content["meta"]["side_effect"]["explicit_token_required"] is True

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("ths_place_order", {"code": "600519", "price": 100.0, "amount": 100, "direction": "buy"}),
            ("ths_cancel_order", {"entrust_no": "E123"}),
        ],
    )
    @pytest.mark.parametrize("broker_token", ["", "wrong-token"])
    def test_trade_risk_tools_reject_missing_or_mismatched_token(self, monkeypatch, tool_name, arguments, broker_token):
        from aiask_finance_mcp.tonghuashun.server import _handle_jsonrpc

        monkeypatch.setenv("AIASK_FINANCE_THS_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.setenv("AIASK_FINANCE_THS_BROKER_TOKEN", "expected-token")
        call_args = dict(arguments)
        if broker_token:
            call_args["broker_token"] = broker_token
        result = _call_jsonrpc(_handle_jsonrpc, "tools/call", {"name": tool_name, "arguments": call_args})
        content = json.loads(result["result"]["content"][0]["text"])

        assert result["result"]["isError"] is True
        assert content["success"] is False
        assert content["error_code"] == "TRADE_RISK_TOKEN_REQUIRED"
        assert content["meta"]["side_effect"]["confirmation_required"] is True
        assert content["meta"]["side_effect"]["explicit_token_required"] is True


class TestEastmoneyProtocol:
    def test_initialize(self):
        from aiask_finance_mcp.eastmoney.server import _handle_jsonrpc

        result = _call_jsonrpc(_handle_jsonrpc, "initialize")
        assert result["result"]["serverInfo"]["name"] == "aiask-finance-eastmoney"
        _assert_resource_prompt_capabilities(result)

    def test_tools_list(self):
        from aiask_finance_mcp.eastmoney.server import _handle_jsonrpc

        result = _call_jsonrpc(_handle_jsonrpc, "tools/list")
        tools = result["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "em_realtime_quote" in tool_names
        assert "em_kline_history" in tool_names
        assert "em_fund_info" in tool_names
        assert "em_news_flow" in tool_names
        assert "em_dragon_tiger_list" in tool_names

    def test_resources_and_prompts_list(self):
        from aiask_finance_mcp.eastmoney.server import _handle_jsonrpc

        _assert_empty_resources_and_prompts(_handle_jsonrpc)

    def test_kline_missing_code(self):
        from aiask_finance_mcp.eastmoney.server import _handle_jsonrpc

        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {"name": "em_kline_history", "arguments": {}},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        assert content["success"] is False


class TestQmtProtocol:
    def test_initialize(self):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        result = _call_jsonrpc(_handle_jsonrpc, "initialize")
        assert result["result"]["serverInfo"]["name"] == "aiask-finance-qmt"
        _assert_resource_prompt_capabilities(result)

    def test_tools_list_default_hides_live_tools(self, monkeypatch):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        monkeypatch.delenv("AIASK_FINANCE_ENABLE_LIVE_TOOLS", raising=False)
        monkeypatch.delenv("AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS", raising=False)
        result = _call_jsonrpc(_handle_jsonrpc, "tools/list")
        tools = result["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "qmt_query_account" in tool_names
        assert "qmt_place_order" not in tool_names
        assert "qmt_cancel_order" not in tool_names

    def test_tools_list_live_tools_require_explicit_opt_in(self, monkeypatch):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        monkeypatch.setenv("AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS", "1")
        result = _call_jsonrpc(_handle_jsonrpc, "tools/list")
        tool_names = [t["name"] for t in result["result"]["tools"]]
        assert "qmt_place_order" in tool_names
        assert "qmt_cancel_order" in tool_names

    def test_resources_and_prompts_list(self):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        _assert_empty_resources_and_prompts(_handle_jsonrpc)

    def test_place_order_validation(self):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {"name": "qmt_place_order", "arguments": {}},
        )
        content = json.loads(result["result"]["content"][0]["text"])
        assert content["success"] is False
        assert "disabled by default" in content["error"]

    def test_live_tool_call_rejects_before_token_when_default_disabled(self, monkeypatch):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        monkeypatch.delenv("AIASK_FINANCE_ENABLE_LIVE_TOOLS", raising=False)
        monkeypatch.delenv("AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS", raising=False)
        monkeypatch.setenv("AIASK_FINANCE_QMT_BROKER_TOKEN", "test-token")
        result = _call_jsonrpc(
            _handle_jsonrpc,
            "tools/call",
            {
                "name": "qmt_place_order",
                "arguments": {
                    "code": "600519",
                    "price": 100.0,
                    "volume": 100,
                    "direction": "buy",
                    "broker_token": "test-token",
                },
            },
        )
        content = json.loads(result["result"]["content"][0]["text"])
        assert result["result"]["isError"] is True
        assert content["success"] is False
        assert "disabled by default" in content["error"]

    def test_place_order_success_keeps_trade_risk_metadata(self, monkeypatch):
        from aiask_finance_mcp.qmt import server

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def place_order(self, *, code: str, price: float, volume: int, direction: str) -> dict[str, Any]:
                self.calls.append({"code": code, "price": price, "volume": volume, "direction": direction})
                return {"order_id": 123, "broker_token": "must-not-leak"}

        fake_client = FakeClient()
        monkeypatch.setenv("AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.setenv("AIASK_FINANCE_QMT_BROKER_TOKEN", "expected-token")
        monkeypatch.setattr(server, "_get_client", lambda: fake_client)

        result = _call_jsonrpc(
            server._handle_jsonrpc,
            "tools/call",
            {
                "name": "qmt_place_order",
                "arguments": {
                    "code": "600519",
                    "price": 100.0,
                    "volume": 100,
                    "direction": "buy",
                    "broker_token": "expected-token",
                },
            },
        )
        content = _jsonrpc_tool_content(result)

        assert result["result"]["isError"] is False
        assert content["success"] is True
        assert fake_client.calls == [{"code": "600519", "price": 100.0, "volume": 100, "direction": "buy"}]
        assert content["data"]["broker_token"] == "[redacted]"
        _assert_trade_risk_side_effect(content)

    def test_cancel_order_success_keeps_trade_risk_metadata(self, monkeypatch):
        from aiask_finance_mcp.qmt import server

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[int] = []

            def cancel_order(self, *, order_id: int) -> dict[str, Any]:
                self.calls.append(order_id)
                return {"order_id": order_id, "cancel_result": 0}

        fake_client = FakeClient()
        monkeypatch.setenv("AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.setenv("AIASK_FINANCE_QMT_BROKER_TOKEN", "expected-token")
        monkeypatch.setattr(server, "_get_client", lambda: fake_client)

        result = _call_jsonrpc(
            server._handle_jsonrpc,
            "tools/call",
            {"name": "qmt_cancel_order", "arguments": {"order_id": 123, "broker_token": "expected-token"}},
        )
        content = _jsonrpc_tool_content(result)

        assert result["result"]["isError"] is False
        assert content["success"] is True
        assert fake_client.calls == [123]
        _assert_trade_risk_side_effect(content)

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("qmt_place_order", {"code": "600519", "price": 100.0, "volume": 100, "direction": "buy"}),
            ("qmt_cancel_order", {"order_id": 123}),
        ],
    )
    def test_trade_risk_tools_reject_when_server_token_unset(self, monkeypatch, tool_name, arguments):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        monkeypatch.setenv("AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.delenv("AIASK_FINANCE_QMT_BROKER_TOKEN", raising=False)
        result = _call_jsonrpc(_handle_jsonrpc, "tools/call", {"name": tool_name, "arguments": {**arguments, "broker_token": "anything"}})
        content = json.loads(result["result"]["content"][0]["text"])

        assert result["result"]["isError"] is True
        assert content["success"] is False
        assert content["error_code"] == "TRADE_RISK_TOKEN_REQUIRED"
        assert content["meta"]["side_effect"]["level"] == "trade_risk"
        assert content["meta"]["side_effect"]["explicit_token_required"] is True

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("qmt_place_order", {"code": "600519", "price": 100.0, "volume": 100, "direction": "buy"}),
            ("qmt_cancel_order", {"order_id": 123}),
        ],
    )
    @pytest.mark.parametrize("broker_token", ["", "wrong-token"])
    def test_trade_risk_tools_reject_missing_or_mismatched_token(self, monkeypatch, tool_name, arguments, broker_token):
        from aiask_finance_mcp.qmt.server import _handle_jsonrpc

        monkeypatch.setenv("AIASK_FINANCE_QMT_ENABLE_LIVE_TOOLS", "1")
        monkeypatch.setenv("AIASK_FINANCE_QMT_BROKER_TOKEN", "expected-token")
        call_args = dict(arguments)
        if broker_token:
            call_args["broker_token"] = broker_token
        result = _call_jsonrpc(_handle_jsonrpc, "tools/call", {"name": tool_name, "arguments": call_args})
        content = json.loads(result["result"]["content"][0]["text"])

        assert result["result"]["isError"] is True
        assert content["success"] is False
        assert content["error_code"] == "TRADE_RISK_TOKEN_REQUIRED"
        assert content["meta"]["side_effect"]["confirmation_required"] is True
        assert content["meta"]["side_effect"]["explicit_token_required"] is True
