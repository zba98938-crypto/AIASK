from __future__ import annotations

import math

import pytest

from akshare_mcp.tools.managers import compliance_manager


@pytest.mark.parametrize("price_raw", ["nan", float("inf"), "-inf"])
def test_order_compliance_rejects_non_finite_price(monkeypatch, price_raw) -> None:
    monkeypatch.setattr(
        compliance_manager,
        "get_quote_snapshot_sync",
        lambda *_args, **_kwargs: {"success": False, "data": None},
    )
    monkeypatch.setattr(
        compliance_manager,
        "get_order_book",
        lambda *_args, **_kwargs: {"success": False, "data": None},
    )

    result = compliance_manager.evaluate_order_compliance(
        code="600519",
        direction="buy",
        quantity_raw=100,
        price_raw=price_raw,
    )

    assert result["passed"] is False
    assert result["blocked"] is True
    assert result["price"] is None
    assert result["order_amount"] is None
    assert any("price" in item for item in result["violations"])


def test_order_compliance_rejects_non_finite_quantity(monkeypatch) -> None:
    monkeypatch.setattr(
        compliance_manager,
        "get_quote_snapshot_sync",
        lambda *_args, **_kwargs: {"success": False, "data": None},
    )
    monkeypatch.setattr(
        compliance_manager,
        "get_order_book",
        lambda *_args, **_kwargs: {"success": False, "data": None},
    )

    result = compliance_manager.evaluate_order_compliance(
        code="600519",
        direction="buy",
        quantity_raw=math.inf,
        price_raw=100,
    )

    assert result["passed"] is False
    assert result["blocked"] is True
    assert result["quantity"] is None
    assert result["order_amount"] is None
    assert any("quantity" in item for item in result["violations"])
