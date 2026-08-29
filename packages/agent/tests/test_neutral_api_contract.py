"""Neutral domain API contract.

The Desktop frontend was removed from this tree, so the ``/v1/desktop/*`` facade
it existed to serve is gone and its still-useful capabilities moved to
domain-scoped paths. These tests pin three things:

1. No ``/v1/desktop/*`` path survives, and each answers a structured 404 that
   names its replacement (so "this moved" is distinguishable from "you typo'd").
2. The neutral routes exist and are reachable.
3. ``/v1/finance/broker/{accounts,positions,orders,deals}`` return *different*
   resources. All three used to call the same full-snapshot payload, so three
   differently-named URLs returned byte-identical bodies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from aiask_agent.api_envelope import REMOVED_ROUTE_REPLACEMENTS, envelope, replacement_for
from aiask_agent.model_client import MockModelClient
from aiask_agent.runtime import AgentRuntime
from aiask_agent.server import create_app
from aiask_agent.session_store import AgentSessionStore
from aiask_agent.tool_registry import AgentToolRegistry


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    runtime = AgentRuntime(
        model_client=MockModelClient(),
        tool_registry=AgentToolRegistry(),
        session_store=AgentSessionStore(tmp_path / "state.sqlite3"),
        max_iterations=2,
    )
    return TestClient(create_app(runtime=runtime))


REMOVED_PATHS = [
    "/v1/desktop/settings/status",
    "/v1/desktop/data/status",
    "/v1/desktop/data/sync-plan",
    "/v1/desktop/stock-data-sources",
    "/v1/desktop/users/local-profile",
    "/v1/desktop/users/local/activity",
    "/v1/desktop/analytics/summary",
    "/v1/desktop/events",
    "/v1/desktop/feedback",
    "/v1/desktop/retention/sweep",
    "/v1/desktop/quant/presets",
    "/v1/desktop/financial-manager/catalog",
    "/v1/desktop/broker-readiness",
    "/v1/desktop/broker/accounts",
    "/v1/desktop/broker/positions",
    "/v1/desktop/broker/orders",
    "/v1/desktop/factor-factory/status",
    "/v1/desktop/stock-radar/status",
    "/v1/desktop/trade-predictions/status",
    "/v1/desktop/workbench/summary",
    "/v1/desktop/runs",
    "/v1/desktop/capabilities",
    "/v1/desktop/files",
    "/v1/desktop/users/local/strategies",
    "/v1/desktop/users/local/stock-pools",
    # Never existed; must still be refused rather than reaching a handler.
    "/v1/desktop",
    "/v1/desktop/",
    "/v1/desktop/made/up/path",
]


@pytest.mark.parametrize("path", REMOVED_PATHS)
def test_removed_desktop_paths_return_404(client: TestClient, path: str) -> None:
    for method in ("get", "post", "patch", "delete"):
        response = getattr(client, method)(path)
        assert response.status_code == 404, f"{method.upper()} {path}"
        body = response.json()
        assert body["error_code"] == "ROUTE_REMOVED"
        assert body["success"] is False
        assert body["meta"]["requested_path"].startswith("/v1/desktop")


def test_removed_route_404_names_its_replacement(client: TestClient) -> None:
    body = client.get("/v1/desktop/broker/positions").json()
    assert body["meta"]["replacement"] == "/v1/finance/broker"
    assert "/v1/finance/broker" in body["error"]


def test_unknown_removed_subpath_is_honest_about_having_no_replacement(client: TestClient) -> None:
    body = client.get("/v1/desktop/made/up/path").json()
    assert body["error_code"] == "ROUTE_REMOVED"
    assert body["meta"]["replacement"] is None
    assert "use" not in body["error"], "must not invent a replacement it cannot name"


def test_no_desktop_route_is_registered_except_the_catch_all(client: TestClient) -> None:
    registered = {route.path for route in client.app.routes if isinstance(route, APIRoute)}
    desktop = {path for path in registered if "/v1/desktop" in path}
    assert desktop == {"/v1/desktop/{removed_path:path}"}, f"unexpected desktop routes: {desktop}"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/settings/status",
        "/v1/data/status",
        "/v1/data/stock-data-sources",
        "/v1/users/local-profile",
        "/v1/workbench/summary",
        "/v1/capabilities",
        "/v1/capabilities/parity",
        "/v1/runs",
        "/v1/finance/quant/presets",
        "/v1/finance/manager/catalog",
        "/v1/finance/broker/readiness",
        "/v1/finance/broker/snapshot",
        "/v1/finance/broker/accounts",
        "/v1/finance/broker/positions",
        "/v1/finance/broker/orders",
        "/v1/finance/broker/deals",
        "/v1/research/factor-factory/status",
    ],
)
def test_neutral_routes_are_reachable(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_every_replacement_target_actually_exists(client: TestClient) -> None:
    """A 404 that points at a nonexistent path would be worse than a bare 404."""
    registered = {route.path for route in client.app.routes if isinstance(route, APIRoute)}
    missing = [
        new
        for _old, new in REMOVED_ROUTE_REPLACEMENTS
        if not any(path == new or path.startswith(f"{new}/") for path in registered)
    ]
    assert not missing, f"replacement families with no registered route: {missing}"


BROKER_RESOURCES = ["accounts", "positions", "orders", "deals"]


@pytest.mark.parametrize("resource", BROKER_RESOURCES)
def test_broker_resource_returns_only_its_own_collection(client: TestClient, resource: str) -> None:
    body = client.get(f"/v1/finance/broker/{resource}").json()
    assert body["success"] is True
    assert body["object"] == f"aiask.broker_readonly.{resource}"
    assert sorted(body["data"]) == [resource], "endpoint must not leak other resources"
    assert body["meta"]["resource"] == resource
    assert body["read_only"] is True
    assert body["live_trading_enabled"] is False


def test_broker_resources_are_not_interchangeable(client: TestClient) -> None:
    """The original defect: three URLs, one identical body."""
    bodies = {
        resource: client.get(f"/v1/finance/broker/{resource}").text
        for resource in BROKER_RESOURCES
    }
    assert len(set(bodies.values())) == len(BROKER_RESOURCES), (
        "broker resources returned identical payloads: " + ", ".join(sorted(bodies))
    )


def test_broker_snapshot_still_exposes_every_resource(client: TestClient) -> None:
    """Callers that genuinely want everything keep a single endpoint for it."""
    data = client.get("/v1/finance/broker/snapshot").json()["data"]
    for key in ("profiles", "accounts", "positions", "orders", "deals", "analytics"):
        assert key in data


def test_broker_resource_limit_is_bounded(client: TestClient) -> None:
    assert client.get("/v1/finance/broker/positions?limit=0").status_code == 422
    assert client.get("/v1/finance/broker/positions?limit=99999").status_code == 422
    ok = client.get("/v1/finance/broker/positions?limit=5")
    assert ok.status_code == 200
    assert ok.json()["meta"]["limit"] == 5


def test_cross_user_read_requires_a_control_token(client: TestClient) -> None:
    """require_user_scope must gate one user's id reaching another's data."""
    same_user = client.get("/v1/users/local/activity", headers={"X-AIASK-User-Id": "local"})
    assert same_user.status_code == 200

    cross_user = client.get("/v1/users/someone-else/activity", headers={"X-AIASK-User-Id": "local"})
    assert cross_user.status_code in {401, 503}, "cross-user read must not succeed unauthenticated"


def test_cross_user_export_and_data_policy_are_scoped(client: TestClient) -> None:
    for path in ("/v1/users/other/export", "/v1/users/other/data-policy", "/v1/users/other/recommendations"):
        response = client.get(path, headers={"X-AIASK-User-Id": "local"})
        assert response.status_code in {401, 503}, path


def test_unscoped_analytics_aggregate_requires_control(client: TestClient) -> None:
    """Without a user_id the aggregate spans users, so plain API access is not enough."""
    response = client.get("/v1/users/analytics/summary")
    assert response.status_code in {401, 503}


def test_replacement_lookup_ignores_unrelated_paths() -> None:
    assert replacement_for("/v1/runs") is None
    assert replacement_for("/health") is None
    assert replacement_for("/v1/desktop/broker-readiness") == "/v1/finance/broker/readiness"
    # Longest prefix wins: /broker must not shadow /broker-readiness.
    assert replacement_for("/v1/desktop/broker/sync") == "/v1/finance/broker"


def test_removed_asset_proxy_routes_have_no_replacement() -> None:
    """These proxied a service that is gone, so there is nothing to point at."""
    for path in (
        "/v1/desktop/files",
        "/v1/desktop/files/file_1",
        "/v1/desktop/users/local/strategies",
        "/v1/desktop/users/local/strategies/strat_1",
        "/v1/desktop/users/local/stock-pools/pool_1/stocks",
    ):
        assert replacement_for(path) is None, path
    # Sibling user routes that *do* have a home must not be caught by this.
    assert replacement_for("/v1/desktop/users/local/activity") == "/v1/users"
    assert replacement_for("/v1/desktop/users/local-profile") == "/v1/users"


def test_envelope_shape_is_complete() -> None:
    body = envelope(
        True,
        object_name="aiask.probe",
        data={"x": 1},
        source="unit-test",
        freshness=123,
        meta={"resource": "probe"},
    )
    for key in ("object", "success", "data", "error", "error_code", "source", "freshness", "degraded", "meta"):
        assert key in body, key
    assert body["meta"]["degraded"] is False
    assert json.dumps(body), "envelope must be JSON-serialisable"
