from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _register_strategy_schema_provider():
    """Supply the factory-domain schema owner when the package is installed.

    quant-core is a leaf package and does not import strategy-factory.  Its
    tests still create databases that include factory tables, so the owner is
    registered explicitly here, exactly as a host would do at startup.
    """
    try:
        from strategy_factory.infrastructure.factory_schema import (
            register_factory_schema,
        )
    except ImportError:
        yield
        return
    register_factory_schema()
    yield
