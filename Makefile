.PHONY: bootstrap test smoke code-graph

PY ?= /opt/miniconda3/envs/aiask/bin/python

bootstrap:
	$(PY) -m pip install -e packages/aiask-quant-core
	$(PY) -m pip install -e packages/strategy-factory
	$(PY) -m pip install -e packages/akshare-mcp
	$(PY) -m pip install -e packages/data-mcp
	$(PY) -m pip install -e packages/finance-mcp-servers
	$(PY) -m pip install -e packages/aiask-skills
	$(PY) -m pip install -e packages/reference-host
	$(PY) -m pip install -e packages/agent

test:
	@set -e; for package in agent aiask-quant-core finance-mcp-servers strategy-factory akshare-mcp aiask-skills data-mcp reference-host; do \
		(cd packages/$$package && $(PY) -m pytest -q tests); \
	done

smoke:
	curl -fsS http://127.0.0.1:$${AIASK_AGENT_PORT:-8765}/health

code-graph:
	$(PY) scripts/code_graph/build_aiask_code_graph.py
