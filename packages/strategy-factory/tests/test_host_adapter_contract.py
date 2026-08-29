"""The Host Adapter Contract (Phase 5).

Hermes, a stock MCP client and the reference host all need the same small set of
capabilities. Writing it down as a protocol is what makes "second host" checkable
rather than aspirational.

Two properties carry most of the weight:

* side effects are **declared, not inferred**, and a host may tighten the policy
  but never loosen it;
* ``render_structured_error`` must preserve degradation, so a host cannot present
  a ``degraded`` result as clean.
"""

from __future__ import annotations

import pytest

from strategy_factory.api.runtime import (
    DEFAULT_SIDE_EFFECT_POLICY,
    HOST_ADAPTER_PROTOCOL_VERSION,
    KNOWN_OPERATIONS,
    CapabilityReport,
    HostAdapter,
    McpManifest,
    SkillBundle,
    assert_policy_not_loosened,
    default_side_effect,
    missing_adapter_methods,
    verify_host_adapter,
)


class _Conforming:
    def install_skill_bundle(self, bundle):
        return {"ok": True}

    def register_mcp(self, manifest):
        return {"ok": True}

    def discover_capabilities(self):
        return CapabilityReport(host="test")

    def invoke_aiask_cli(self, argv, *, timeout=7200.0):
        return {"ok": True}

    def map_side_effect_policy(self, operation):
        return default_side_effect(operation)

    def render_structured_error(self, envelope):
        return dict(envelope)


class _Partial:
    def install_skill_bundle(self, bundle):
        return {}


# --- public surface ------------------------------------------------------------


def test_the_contract_is_on_the_public_api() -> None:
    from strategy_factory.api import runtime

    for name in (
        "HostAdapter",
        "SkillBundle",
        "McpManifest",
        "CapabilityReport",
        "default_side_effect",
        "verify_host_adapter",
    ):
        assert name in runtime.__all__


def test_protocol_version_is_declared() -> None:
    assert HOST_ADAPTER_PROTOCOL_VERSION == "1.0"


# --- conformance ---------------------------------------------------------------


def test_a_conforming_adapter_passes() -> None:
    report = verify_host_adapter(_Conforming())

    assert report["conforms"] is True
    assert report["missing_methods"] == []
    assert report["protocol_version"] == HOST_ADAPTER_PROTOCOL_VERSION


def test_a_partial_adapter_is_named_method_by_method() -> None:
    missing = missing_adapter_methods(_Partial())

    assert "discover_capabilities" in missing
    assert "invoke_aiask_cli" in missing
    assert "install_skill_bundle" not in missing


def test_runtime_checkable_matches_a_conforming_adapter() -> None:
    assert isinstance(_Conforming(), HostAdapter)


def test_verification_does_not_execute_the_adapter() -> None:
    """Conformance must be checkable without side effects."""

    class _Exploding:
        def install_skill_bundle(self, bundle):
            raise AssertionError("must not be called")

        register_mcp = install_skill_bundle
        discover_capabilities = install_skill_bundle
        invoke_aiask_cli = install_skill_bundle
        map_side_effect_policy = install_skill_bundle
        render_structured_error = install_skill_bundle

    assert verify_host_adapter(_Exploding())["conforms"] is True


# --- side-effect policy --------------------------------------------------------


@pytest.mark.parametrize("operation", KNOWN_OPERATIONS)
def test_every_known_operation_is_classified(operation: str) -> None:
    assert operation in DEFAULT_SIDE_EFFECT_POLICY
    assert default_side_effect(operation) in {"read_only", "mutating", "destructive"}


def test_read_only_verbs_are_read_only() -> None:
    for operation in (
        "factory.status",
        "factory.preflight",
        "factory.evidence",
        "factory.schedule_template",
    ):
        assert default_side_effect(operation) == "read_only"


def test_run_verbs_are_mutating() -> None:
    for operation in ("factory.once", "factory.cycle", "factory.ops_start"):
        assert default_side_effect(operation) == "mutating"


def test_enabling_live_trading_is_destructive() -> None:
    """The most consequential operation must carry the strongest classification."""
    assert default_side_effect("live_trading.enable") == "destructive"


def test_unknown_operations_fail_closed() -> None:
    """A new verb must not slip past a confirmation gate by being unclassified."""
    for operation in ("", None, "something.new", "factory.wipe_everything"):
        assert default_side_effect(operation) == "mutating"


def test_a_host_may_tighten_the_policy() -> None:
    assert_policy_not_loosened("factory.status", "mutating")
    assert_policy_not_loosened("factory.once", "destructive")


def test_a_host_may_not_loosen_the_policy() -> None:
    with pytest.raises(ValueError, match="weaker than the required"):
        assert_policy_not_loosened("factory.once", "read_only")

    with pytest.raises(ValueError):
        assert_policy_not_loosened("live_trading.enable", "mutating")


def test_matching_the_baseline_is_allowed() -> None:
    for operation, declared in DEFAULT_SIDE_EFFECT_POLICY.items():
        assert_policy_not_loosened(operation, declared)


# --- data shapes ---------------------------------------------------------------


def test_skill_bundle_serialises() -> None:
    payload = SkillBundle(
        name="aiask-finance", version="1.0.0", skills=("a", "b"), required_mcp=("m",)
    ).to_dict()

    assert payload["skills"] == ["a", "b"]
    assert payload["required_mcp"] == ["m"]
    assert payload["protocol_version"] == HOST_ADAPTER_PROTOCOL_VERSION


def test_mcp_manifest_defaults_to_read_only_stdio() -> None:
    manifest = McpManifest(name="m", command=("python", "x.py"))

    assert manifest.transport == "stdio"
    assert manifest.read_only is True, "registration should default to the safe side"


def test_capability_report_is_not_ready_without_capabilities() -> None:
    assert CapabilityReport(host="h").ready is False


def test_capability_report_is_not_ready_while_degraded() -> None:
    report = CapabilityReport(
        host="h", capabilities=("factory.status",), degraded_reasons=("no_mcp",)
    )

    assert report.ready is False, "a degraded host must not report itself ready"
    assert report.to_dict()["ready"] is False


def test_capability_report_is_ready_when_clean() -> None:
    assert CapabilityReport(host="h", capabilities=("factory.status",)).ready is True


def test_capability_report_serialises_every_field() -> None:
    payload = CapabilityReport(
        host="h",
        capabilities=("c",),
        mcp_servers=("m",),
        skills=("s",),
        degraded_reasons=("d",),
    ).to_dict()

    for key in ("host", "protocol_version", "capabilities", "mcp_servers", "skills", "degraded_reasons", "ready"):
        assert key in payload
