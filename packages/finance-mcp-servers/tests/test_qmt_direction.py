"""QMT must refuse an order direction it does not recognise.

Regression
----------
``QmtClient.place_order`` read::

    order_type = STOCK_BUY if direction == "buy" else STOCK_SELL

so *every* string that was not exactly ``"buy"`` became a **sell** order:
``"buy "`` with a trailing space, a misspelling such as ``"byu"``, or a synonym
like ``"long"``. The handler made it worse by omitting ``.strip()`` before the
comparison. Tonghuashun already rejected unknown directions
(``if direction not in ("buy", "sell")``); QMT did not.

These tests drive the real handler with a recording client and assert that **no
broker call is made** for an invalid direction — returning an error while still
placing the order would satisfy a weaker assertion and miss the point.

``xtquant`` is the broker SDK and is not installed here, so the client-level
tests stub only its constants module.
"""

from __future__ import annotations

import sys
import types

import pytest

from aiask_finance_mcp.qmt import server as qmt_server


class _RecordingTrader:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def order_stock(self, account, code, order_type, volume, price_type, price):
        self.calls.append((code, order_type, volume, price))
        return 9001


@pytest.fixture
def xtconstant(monkeypatch):
    """Stub the broker SDK's constants so the client guard is reachable."""

    package = types.ModuleType("xtquant")
    constants = types.ModuleType("xtquant.xtconstant")
    constants.STOCK_BUY = 23
    constants.STOCK_SELL = 24
    monkeypatch.setitem(sys.modules, "xtquant", package)
    monkeypatch.setitem(sys.modules, "xtquant.xtconstant", constants)
    return constants


def _recording_client(xtconstant) -> tuple[qmt_server.QmtClient, _RecordingTrader]:
    client = qmt_server.QmtClient()
    client._connected = True
    trader = _RecordingTrader()
    client._trader = trader
    client._account = object()
    return client, trader


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("AIASK_FINANCE_ENABLE_LIVE_TOOLS", "1")
    monkeypatch.setenv("AIASK_FINANCE_QMT_BROKER_TOKEN", "secret")
    return {"broker_token": "secret", "code": "600519", "price": 10.0, "volume": 100}


def _handler_call(monkeypatch, arguments: dict) -> tuple[dict, list]:
    """Run the real handler; record whether the client was ever reached."""

    reached: list = []

    def _explode():
        reached.append(True)
        raise AssertionError("client must not be reached for an invalid request")

    monkeypatch.setattr(qmt_server, "_get_client", _explode)
    return qmt_server.handle_place_order(arguments), reached


@pytest.mark.parametrize(
    "direction",
    ["byu", "long", "SELL!", "buy;", "买"],
)
def test_invalid_direction_is_rejected_before_the_client(
    monkeypatch, live_env, direction: str
) -> None:
    """Directions that are not whitespace/case variants of buy or sell.

    ``"buy "`` deliberately does not appear here: stripping whitespace is
    normalisation, so it must be *accepted* as buy — see the normalisation test
    below. Putting it here was wrong and produced three failing assertions even
    with the fix in place, which would have contaminated the injection results.
    """

    payload, reached = _handler_call(monkeypatch, {**live_env, "direction": direction})

    assert payload["success"] is False, (
        f"direction {direction!r} must be rejected; got {payload}"
    )
    assert payload["error"] == "direction must be 'buy' or 'sell'"
    assert reached == [], (
        "an invalid direction previously became a SELL order; the client must "
        "never be reached"
    )


def test_case_and_whitespace_are_normalised_for_valid_values(monkeypatch, live_env) -> None:
    recorded: list = []

    class _Client:
        def place_order(self, *, code, price, volume, direction):
            recorded.append(direction)
            return {"order_id": 1}

    monkeypatch.setattr(qmt_server, "_get_client", lambda: _Client())

    for raw in ("BUY", " buy ", "Sell", "sell "):
        payload = qmt_server.handle_place_order({**live_env, "direction": raw})
        assert payload["success"] is True, raw

    assert recorded == ["buy", "buy", "sell", "sell"]


def test_missing_direction_still_defaults_to_buy(monkeypatch, live_env) -> None:
    recorded: list = []

    class _Client:
        def place_order(self, *, code, price, volume, direction):
            recorded.append(direction)
            return {"order_id": 1}

    monkeypatch.setattr(qmt_server, "_get_client", lambda: _Client())
    payload = qmt_server.handle_place_order(dict(live_env))

    assert payload["success"] is True
    assert recorded == ["buy"]


def test_client_maps_direction_to_the_right_order_type(xtconstant) -> None:
    client, trader = _recording_client(xtconstant)

    client.place_order(code="600519", price=10.0, volume=100, direction="buy")
    client.place_order(code="600519", price=10.0, volume=100, direction="sell")
    # Whitespace and case are normalised, so these are the same two sides —
    # not invalid input. Expecting them to raise was my own misclassification,
    # made twice, and it produced failing assertions even with the fix in place.
    client.place_order(code="600519", price=10.0, volume=100, direction="buy ")
    client.place_order(code="600519", price=10.0, volume=100, direction=" SELL ")

    assert [call[1] for call in trader.calls] == [
        xtconstant.STOCK_BUY,
        xtconstant.STOCK_SELL,
        xtconstant.STOCK_BUY,
        xtconstant.STOCK_SELL,
    ]


@pytest.mark.parametrize("direction", ["byu", "long", "", "买"])
def test_client_refuses_to_guess_a_side(xtconstant, direction: str) -> None:
    """Only genuinely unrecognised directions — not normalisable variants."""

    client, trader = _recording_client(xtconstant)

    with pytest.raises(ValueError, match="Invalid direction"):
        client.place_order(
            code="600519", price=10.0, volume=100, direction=direction
        )

    assert trader.calls == [], (
        "the client used to pick STOCK_SELL for anything that was not 'buy'; "
        f"{direction!r} must place no order at all"
    )
