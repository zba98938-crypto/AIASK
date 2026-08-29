"""The minimal reference host (Phase 5).

Its job is to prove the Host Adapter Contract is implementable without Hermes.
These tests check conformance and the honesty properties — that gaps are reported
rather than hidden, and that a degraded result is never rendered as clean.

Deliberately not tested here: conversation loops, gateways, model routing. The
reference host has none, and the contract does not require them.
"""

from __future__ import annotations

import json

import pytest

from strategy_factory.api.runtime import (
    HOST_ADAPTER_PROTOCOL_VERSION,
    HostAdapter,
    McpManifest,
    SkillBundle,
    verify_host_adapter,
)

from aiask_agent.reference_host import REFERENCE_CAPABILITIES, ReferenceHost


@pytest.fixture()
def skills_dir(tmp_path):
    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8"
        )
    return root


@pytest.fixture()
def host(tmp_path, skills_dir):
    return ReferenceHost(
        project_root=tmp_path / "project",
        state_dir=tmp_path / "state",
        skills_dir=skills_dir,
    )


# --- conformance ---------------------------------------------------------------


def test_the_reference_host_conforms_to_the_contract(host) -> None:
    report = verify_host_adapter(host)

    assert report["conforms"] is True, f"missing: {report['missing_methods']}"
    assert isinstance(host, HostAdapter)


def test_it_declares_the_contract_version(host) -> None:
    assert host.discover_capabilities().protocol_version == HOST_ADAPTER_PROTOCOL_VERSION


def test_it_has_no_hermes_dependency() -> None:
    """The point of a reference host is that it needs no particular host."""
    import ast
    import inspect

    import aiask_agent.reference_host as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"her" + "mes_cli", "her" + "mes_constants", "run_agent"}
    assert not (imported & forbidden), f"reference host imports {imported & forbidden}"


# --- skill bundles -------------------------------------------------------------


def test_installing_a_bundle_verifies_skills_exist(host) -> None:
    outcome = host.install_skill_bundle(
        SkillBundle(name="b", version="1.0.0", skills=("alpha", "beta"))
    )

    assert outcome["ok"] is True
    assert outcome["installed"] == 2
    assert outcome["missing"] == []


def test_a_bundle_with_a_missing_skill_is_not_ok(host) -> None:
    """A partial install must not be reported as success."""
    outcome = host.install_skill_bundle(
        SkillBundle(name="b", version="1.0.0", skills=("alpha", "nope"))
    )

    assert outcome["ok"] is False
    assert outcome["missing"] == ["nope"]


def test_an_empty_bundle_is_not_ok(host) -> None:
    outcome = host.install_skill_bundle(SkillBundle(name="b", version="1.0.0"))
    assert outcome["ok"] is False


def test_installation_is_idempotent(host) -> None:
    bundle = SkillBundle(name="b", version="1.0.0", skills=("alpha",))
    first = host.install_skill_bundle(bundle)
    second = host.install_skill_bundle(bundle)

    assert first == second
    state = json.loads((host.state_dir / "skill_bundles.json").read_text(encoding="utf-8"))
    assert list(state) == ["b"]


def test_the_default_bundle_is_read_from_disk(host) -> None:
    bundle = host.default_skill_bundle()

    assert bundle.skills == ("alpha", "beta")
    assert "aiask-factories" in bundle.required_mcp


# --- MCP registration ----------------------------------------------------------


def test_registering_an_mcp_is_idempotent(host) -> None:
    manifest = McpManifest(name="m", command=("python", "x.py"))

    assert host.register_mcp(manifest)["ok"] is True
    second = host.register_mcp(manifest)

    assert second["registered"] == 1, "re-registering the same name must not duplicate"


def test_a_non_stdio_transport_is_refused(host) -> None:
    outcome = host.register_mcp(
        McpManifest(name="m", command=("x",), transport="http")
    )

    assert outcome["ok"] is False
    assert "unsupported_transport" in outcome["error"]


def test_the_default_manifest_is_read_only(host) -> None:
    manifest = host.default_mcp_manifest()

    assert manifest.read_only is True
    assert manifest.transport == "stdio"
    assert "AIASK_PROJECT_ROOT" in manifest.env


# --- capability discovery ------------------------------------------------------


def test_discovery_finds_installed_skills(host) -> None:
    report = host.discover_capabilities()
    assert report.skills == ("alpha", "beta")


def test_discovery_reports_a_missing_mcp_rather_than_hiding_it(host) -> None:
    report = host.discover_capabilities()

    assert "no_mcp_registered" in report.degraded_reasons
    assert report.ready is False


def test_discovery_becomes_ready_once_gaps_close(host) -> None:
    host.register_mcp(McpManifest(name="m", command=("python", "x.py")))
    report = host.discover_capabilities()

    assert report.degraded_reasons == (), f"unexpected gaps: {report.degraded_reasons}"
    assert report.ready is True
    assert set(report.capabilities) == set(REFERENCE_CAPABILITIES)


def test_discovery_reports_an_empty_skills_directory(tmp_path) -> None:
    host = ReferenceHost(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        skills_dir=tmp_path / "absent",
    )

    assert any(
        "no_skills_found" in reason for reason in host.discover_capabilities().degraded_reasons
    )


# --- side-effect policy --------------------------------------------------------


def test_policy_matches_the_shared_baseline(host) -> None:
    assert host.map_side_effect_policy("factory.status") == "read_only"
    assert host.map_side_effect_policy("factory.once") == "mutating"
    assert host.map_side_effect_policy("live_trading.enable") == "destructive"


def test_unknown_operations_are_treated_as_mutating(host) -> None:
    assert host.map_side_effect_policy("brand.new.verb") == "mutating"


# --- error rendering -----------------------------------------------------------


def test_a_degraded_envelope_is_never_rendered_as_clean(host) -> None:
    rendered = host.render_structured_error(
        {"status": "degraded", "degraded_reasons": ["template_only"], "run_id": "r1"}
    )

    assert rendered["clean"] is False
    assert rendered["status"] == "degraded"
    assert "template_only" in rendered["headline"]
    assert rendered["run_id"] == "r1"


def test_errors_take_precedence_in_the_headline(host) -> None:
    rendered = host.render_structured_error(
        {"status": "failed", "errors": ["db unreachable"], "degraded_reasons": ["stale"]}
    )

    assert rendered["headline"] == "db unreachable"
    assert rendered["clean"] is False


def test_a_clean_success_is_marked_clean(host) -> None:
    assert host.render_structured_error({"status": "success"})["clean"] is True


def test_a_success_with_errors_is_not_clean(host) -> None:
    rendered = host.render_structured_error({"status": "success", "errors": ["boom"]})
    assert rendered["clean"] is False


def test_an_empty_envelope_does_not_crash(host) -> None:
    for value in ({}, None):
        rendered = host.render_structured_error(value)
        assert rendered["clean"] is False


# --- CLI invocation ------------------------------------------------------------


def test_cli_invocation_forces_live_trading_off(host, monkeypatch) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    assert host._child_env()["LIVE_TRADING_ENABLED"] == "0"


def test_cli_invocation_reports_a_spawn_failure_structurally(host) -> None:
    host.python = "/nonexistent/python"
    outcome = host.invoke_aiask_cli(["factory", "status"], timeout=10)

    assert outcome["ok"] is False
    assert "spawn_failed" in outcome["error"]


def test_state_survives_a_new_host_instance(host, tmp_path, skills_dir) -> None:
    host.register_mcp(McpManifest(name="m", command=("python", "x.py")))

    reopened = ReferenceHost(
        project_root=tmp_path / "project",
        state_dir=host.state_dir,
        skills_dir=skills_dir,
    )

    assert reopened.discover_capabilities().mcp_servers == ("m",)


def test_corrupt_state_degrades_instead_of_raising(host) -> None:
    host.state_dir.mkdir(parents=True, exist_ok=True)
    (host.state_dir / "mcp_registry.json").write_text("{not json", encoding="utf-8")

    report = host.discover_capabilities()

    assert "no_mcp_registered" in report.degraded_reasons
