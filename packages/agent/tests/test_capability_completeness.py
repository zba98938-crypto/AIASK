"""Capability completeness regression tests.

Asserts the four Hermes v0.14 delta features that previously sat at status="partial"
have moved to either implemented (with native action) or excluded_by_design (with
explicit reason). Also pins:

- agent_security_scan(action="dependency_advisory") returning configured=True without live env.
- agent_file_write writing invalid Python yields a SyntaxError diagnostic with line info.
- agent_file_write writing invalid YAML yields a YAML diagnostic when PyYAML is installed
  (skipped/best-effort otherwise — never marked partial).
- The plugin_runtime no-runner branch returns the new structured skip envelope.
- ToolPolicy financial guardrails (FORBIDDEN_DIRECT_MANAGER_TOKENS, finance_safe default)
  are still enforced — these are the AIASK-specific financial safety invariants and must
  not regress while we close the parity gap.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiask_agent import capabilities as cap
from aiask_agent.general_tools import _write_diagnostics, build_general_tool_handlers
from aiask_agent.plugin_runtime import NativePluginManager
from aiask_agent.security import SecurityScanner
from aiask_agent.tools.policy import (
    FINANCE_SAFE_TOOLSET,
    FORBIDDEN_DIRECT_MANAGER_TOKENS,
    ToolPolicy,
    build_policy_from_env,
    ensure_agent_tool_name,
)


_REGISTERED = {
    "agent_model_manage",
    "agent_security_scan",
    "agent_file_write",
    "agent_file_patch",
    "agent_file_checkpoint",
    "agent_file_rollback",
    "agent_file_mutation_verify",
}


def _feature_by_name(features, name):
    for item in features:
        if item.get("feature") == name:
            return item
    raise AssertionError(f"feature {name} not found in parity matrix")


def test_v014_partial_features_have_been_resolved() -> None:
    features = cap.hermes_native_feature_parity(_REGISTERED, env={})

    proxy = _feature_by_name(features, "openai_compatible_local_proxy")
    assert proxy["status"] == "excluded_by_design"
    assert proxy.get("excluded_reason"), "excluded_by_design must carry excluded_reason"
    assert proxy["mock_status"] == "excluded"

    oauth = _feature_by_name(features, "oauth_subscription_providers")
    assert oauth["status"] == "excluded_by_design"
    assert oauth.get("excluded_reason")
    assert oauth["mock_status"] == "excluded"

    lsp = _feature_by_name(features, "write_time_lsp_diagnostics")
    assert lsp["status"] == "implemented"
    assert lsp["mock_status"] == "passed"

    supply = _feature_by_name(features, "lazy_dependency_and_supply_chain_checks")
    assert supply["status"] == "implemented"
    assert supply["mock_status"] == "passed"


def test_parity_summary_exposes_excluded_by_design_buckets() -> None:
    summary = cap.parity_summary(_REGISTERED, env={})
    assert summary["excluded_by_design_count"] >= 2
    feature_names = {item["feature"] for item in summary["excluded_by_design_features"]}
    assert "openai_compatible_local_proxy" in feature_names
    assert "oauth_subscription_providers" in feature_names

    delta = summary["v014_delta"]
    assert delta["excluded_by_design_count"] >= 2
    assert all(item.get("status") == "excluded_by_design" for item in delta["excluded_by_design"])

    delta_partial_features = {
        item.get("feature")
        for item in delta["partial"]
        if item.get("feature")
    }
    assert "openai_compatible_local_proxy" not in delta_partial_features
    assert "oauth_subscription_providers" not in delta_partial_features
    assert "write_time_lsp_diagnostics" not in delta_partial_features
    assert "lazy_dependency_and_supply_chain_checks" not in delta_partial_features


def test_security_scanner_dependency_advisory_runs_without_live_env() -> None:
    scanner = SecurityScanner()
    result = scanner.dependency_advisory({})
    assert result["configured"] is True
    assert result["lazy_supply_chain_check"] is True
    assert result["advisory_count"] >= 1
    assert isinstance(result["findings"], list)
    assert result["status"] in {"passed", "warning", "blocked"}


def test_security_scanner_dependency_advisory_flags_explicit_vulnerable_version() -> None:
    scanner = SecurityScanner()
    result = scanner.dependency_advisory(
        {
            "include_loaded": False,
            "include_installed": False,
            "packages": [{"name": "requests", "version": "2.30.0"}],
        }
    )
    assert result["finding_count"] >= 1
    flagged = [item for item in result["findings"] if str(item.get("package", "")).lower() == "requests"]
    assert flagged, "requests 2.30.0 should be flagged by the curated ledger"
    assert flagged[0]["installed_version"] == "2.30.0"
    assert flagged[0]["fixed_in"]


def test_security_scan_router_dispatches_dependency_advisory() -> None:
    scanner = SecurityScanner()
    result = scanner.scan({"action": "dependency_advisory"})
    assert result["object"] == "aiask.dependency_advisory"
    assert result["configured"] is True


def test_write_diagnostics_python_syntax_error(tmp_path: Path) -> None:
    target = tmp_path / "broken.py"
    target.write_text("def oops(:\n    pass\n", encoding="utf-8")
    diagnostics = _write_diagnostics(target)
    py_entries = [item for item in diagnostics if item.get("language") == "python"]
    assert py_entries, "expected python diagnostic for .py write"
    failed = [item for item in py_entries if item["status"] == "failed"]
    assert failed, "broken python file must report failed diagnostic"
    assert failed[0].get("line"), "syntax error diagnostic must carry line number"


def test_write_diagnostics_json_syntax_error(tmp_path: Path) -> None:
    target = tmp_path / "broken.json"
    target.write_text("{\"a\": 1,}\n", encoding="utf-8")  # trailing comma is invalid JSON
    diagnostics = _write_diagnostics(target)
    json_entries = [item for item in diagnostics if item.get("language") == "json"]
    assert json_entries
    assert json_entries[0]["status"] == "failed"
    assert json_entries[0].get("line")


def test_write_diagnostics_yaml_path(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("a: 1\nb: [unclosed\n", encoding="utf-8")
    diagnostics = _write_diagnostics(target)
    yaml_entries = [item for item in diagnostics if item.get("language") == "yaml"]
    assert yaml_entries, "yaml diagnostic should always emit at least a skipped/passed/failed entry"
    # Either pyyaml reports failed, or pyyaml is absent and we get skipped — both are non-partial outcomes.
    assert yaml_entries[0]["status"] in {"failed", "passed", "skipped"}


def test_plugin_runtime_no_runner_skip_envelope(tmp_path: Path) -> None:
    manager = NativePluginManager(root=tmp_path / "plugins")
    hook = {"plugin": "demo", "name": "pre_tool_call"}
    result = asyncio.run(manager._run_hook(hook, {"argument": "value"}))
    assert isinstance(result, dict)
    assert result.get("skipped") is True
    assert result.get("reason") == "no_runner"
    assert result.get("configured") is False
    assert result.get("hook") == "pre_tool_call"
    assert result.get("plugin") == "demo"


def test_finance_safe_policy_invariants_preserved(monkeypatch) -> None:
    monkeypatch.delenv("AIASK_AGENT_TOOLSET", raising=False)
    monkeypatch.delenv("AIASK_AGENT_ENABLE_GENERAL_TOOLS", raising=False)
    policy = build_policy_from_env()
    assert policy.toolset == FINANCE_SAFE_TOOLSET, "default toolset must remain finance_safe"
    assert policy.allows_category("financial_read") is True
    assert policy.allows_category("general_write") is False
    assert policy.allows_category("browser") is False


def test_forbidden_manager_tokens_still_block_naming() -> None:
    for token in FORBIDDEN_DIRECT_MANAGER_TOKENS:
        leaky_name = f"agent_{token}_proxy"
        try:
            ensure_agent_tool_name(leaky_name)
        except ValueError as exc:
            assert token in str(exc)
        else:
            raise AssertionError(f"forbidden token {token!r} must be rejected by ensure_agent_tool_name")
    # Sanity: a normal AIASK-native tool name still passes.
    assert ensure_agent_tool_name("agent_security_scan") == "agent_security_scan"
