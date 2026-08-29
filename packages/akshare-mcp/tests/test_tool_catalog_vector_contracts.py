from __future__ import annotations

from akshare_mcp.tools.tool_catalog import get_tool_contract, list_tool_contracts


def test_vector_search_contracts_are_explicitly_registered_in_tool_catalog():
    catalog = {item["name"]: item for item in list_tool_contracts()}

    semantic = catalog.get("semantic_stock_search")
    similar = catalog.get("search_similar_stocks")
    kline = catalog.get("search_by_kline")

    assert semantic is not None
    assert similar is not None
    assert kline is not None

    assert semantic["category"] == "search"
    assert semantic["required_params"] == ["query"]
    assert semantic["side_effect"]["level"] == "read_only"
    assert semantic["input_schema"]["properties"]["limit"]["maximum"] == 100

    assert similar["category"] == "search"
    assert similar["required_params"] == ["code"]
    assert "similarity_type" in similar["input_schema"]["properties"]
    assert similar["side_effect"]["level"] == "read_only"

    assert kline["category"] == "quant"
    assert kline["required_params"] == ["code"]
    assert kline["input_schema"]["properties"]["days"]["minimum"] == 5
    assert kline["side_effect"]["level"] == "read_only"


def test_get_tool_contract_returns_vector_contract_without_runtime_inference_flags():
    semantic = get_tool_contract("semantic_stock_search")

    assert semantic is not None
    assert semantic["name"] == "semantic_stock_search"
    assert semantic["contract_version"] == "ai_tool_contract_v1"
    assert semantic.get("inferred_from_runtime") is None
    assert semantic["examples"][0]["arguments"]["query"] == "高股息银行股"
