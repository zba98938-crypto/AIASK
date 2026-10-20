from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _register_factory_schema_provider():
    """A fresh test database must include the factory-domain tables."""
    from strategy_factory.infrastructure.factory_schema import register_factory_schema

    register_factory_schema()
    yield

