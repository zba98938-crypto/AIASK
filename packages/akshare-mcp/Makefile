# AKShare MCP Makefile

.PHONY: help install lint format tool-registry tool-description-audit incubation-factory incubation-factory-daemon incubation-factory-status clean

help:
	@echo "Available commands:"
	@echo "  make install     - Install dependencies"
	@echo "  make lint        - Run linting"
	@echo "  make format      - Format code"
	@echo "  make tool-registry - Export runtime tool registry"
	@echo "  make tool-description-audit - Generate tool description audit report"
	@echo "  make incubation-factory        - Run incubation factory once"
	@echo "  make incubation-factory-daemon - Run incubation factory as daemon"
	@echo "  make incubation-factory-status - Show incubation factory status"
	@echo "  make clean       - Clean cache files"

install:
	pip install -r requirements.txt

lint:
	flake8 src/ --max-line-length=120 --ignore=E501,W503

format:
	black src/ --line-length=120

tool-registry:
	python scripts/export_tool_registry.py

tool-description-audit:
	python scripts/generate_tool_description_audit.py

incubation-factory:
	python scripts/run_incubation_factory.py

incubation-factory-daemon:
	python scripts/run_incubation_factory.py --daemon

incubation-factory-status:
	python scripts/run_incubation_factory.py --status

incubation-factory-dry-run:
	python scripts/run_incubation_factory.py --dry-run --json

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".benchmark" -exec rm -rf {} +
