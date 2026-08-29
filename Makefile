.PHONY: bootstrap test test-agent test-finance smoke code-graph

bootstrap:
	cd packages/agent && uv sync

test: test-agent test-finance

test-agent:
	cd packages/agent && pytest -q tests

test-finance:
	cd packages/akshare-mcp && pytest -q tests/test_strategy_mgr_capabilities_health.py tests/test_tool_argument_contract.py tests/test_strategy_market_incubation_surface.py

smoke:
	curl -fsS http://127.0.0.1:$${AIASK_AGENT_PORT:-8765}/health

code-graph:
	python scripts/code_graph/build_aiask_code_graph.py
