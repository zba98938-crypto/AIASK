.PHONY: bootstrap test test-agent test-finance desktop-install typecheck build-desktop package-desktop smoke code-graph

bootstrap:
	cd packages/agent && uv sync
	$(MAKE) desktop-install

test: test-agent test-finance

test-agent:
	cd packages/agent && pytest -q tests

test-finance:
	cd packages/akshare-mcp && pytest -q tests/test_strategy_mgr_capabilities_health.py tests/test_tool_argument_contract.py tests/test_strategy_market_incubation_surface.py

typecheck: desktop-install
	npm --prefix desktop run typecheck

build-desktop: desktop-install
	npm --prefix desktop run build

desktop-install:
	npm --prefix desktop ci

package-desktop: build-desktop
	@echo "AIASK Desktop production artifact: desktop/dist"

smoke:
	curl -fsS http://127.0.0.1:$${AIASK_AGENT_PORT:-8767}/health

code-graph:
	python scripts/code_graph/build_aiask_code_graph.py
