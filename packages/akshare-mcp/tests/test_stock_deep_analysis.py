from __future__ import annotations

import asyncio
import importlib
import os
import sys
from uuid import uuid4

from akshare_mcp.resources.analysis import (
    build_analysis_run_report_payload,
    build_analysis_run_summary_payload,
    build_stock_deep_analysis_payload,
)
from akshare_mcp.services.analysis_integrity_validator import validate_analysis_integrity
from akshare_mcp.services.artifact_registry import register_artifact_async
from akshare_mcp.services.stock_deep_analysis import (
    ANALYSIS_STRATEGY,
    ANALYSIS_VERSION,
    _build_evidence_pack,
    _build_synthesis,
    _resolve_target,
    run_stock_deep_analysis,
)
from akshare_mcp.tools.skills import _available_skill_handlers
from akshare_mcp.tools.skills_registry import _SKILL_CONTRACTS


def _assembled_payload() -> dict:
    return {
        "run_id": "stock-analysis-run-demo",
        "task": "deep_analysis",
        "target": {"code": "600519", "name": "贵州茅台", "resolved": True},
        "profile": {
            "stock": {"code": "600519", "name": "贵州茅台", "pe_ratio": 25.5},
            "realtime_quote": {"price": 1688.0},
        },
        "financials": {
            "reportDate": "2025-12-31",
            "revenue": 1000000000,
            "netProfit": 520000000,
            "roe": 28.3,
            "grossProfitMargin": 46.8,
        },
        "decision": {
            "action": "buy",
            "confidence": 0.72,
            "reasons": ["现金流稳定", "量价结构偏强"],
            "risks": ["估值不便宜"],
        },
        "contexts": {
            "stock": {
                "score": 68,
                "market_snapshot": {"change_pct": 1.2},
                "fund_flow_snapshot": {"main_net_inflow": 12800000},
                "warnings": [],
                "fallback_reason": [],
            },
            "quant": {"score": 64, "warnings": [], "fallback_reason": []},
            "event": {
                "score": 58,
                "sentiment": "bullish",
                "candidate_actions": ["业绩催化"],
                "warnings": [],
                "fallback_reason": [],
            },
            "user": {"risk_bucket": "moderate", "warnings": [], "fallback_reason": []},
        },
    }


def test_integrity_validator_blocks_missing_critical_fields():
    payload = {
        "target": {"code": "600519", "name": "贵州茅台"},
        "profile": {"realtime_quote": {"price": 1688.0}},
        "financials": {},
        "decision": {"action": "buy"},
        "contexts": {"stock": {}, "quant": {}, "event": {}, "user": {}},
    }

    report = validate_analysis_integrity(payload, task="deep_analysis")

    assert report["blocked"] is True
    assert report["status"] == "blocked"
    assert any(item["field"] == "financials.reportDate" for item in report["critical_missing"])


def test_synthesis_differs_between_quick_scan_and_deep_analysis():
    assembled = _assembled_payload()
    evidence = _build_evidence_pack(assembled, task="deep_analysis")
    gap_report = {
        "status": "passed",
        "blocked": False,
        "critical_missing": [],
        "non_critical_missing": [],
        "fallback_flags": [],
    }

    quick = _build_synthesis(assembled, evidence, gap_report, task="quick_scan")
    deep = _build_synthesis(assembled, evidence, gap_report, task="deep_analysis")

    assert len(quick["sections"]) < len(deep["sections"])
    assert {section["key"] for section in quick["sections"]} <= {section["key"] for section in deep["sections"]}


def test_skill_handlers_cover_new_stock_deep_analysis_surfaces():
    handlers = _available_skill_handlers()

    assert "akshare-stock-deep-analysis" in handlers
    assert "akshare-trading-decision" in handlers
    assert "deep_analysis" in _SKILL_CONTRACTS["akshare-stock-deep-analysis"]["supported_tasks"]
    assert "trade_plan" in _SKILL_CONTRACTS["akshare-trading-decision"]["supported_tasks"]


def test_quick_scan_direct_code_backfills_target_name_and_renders_report():
    import akshare_mcp.services.stock_deep_analysis as stock_deep_analysis

    assembled = _assembled_payload() | {"task": "quick_scan"}

    async def _fake_contexts(_code: str, _user_id: str | None):
        contexts = assembled["contexts"]
        return contexts["stock"], contexts["quant"], contexts["event"], contexts["user"]

    async def _fake_persist_artifact(**_kwargs):
        return {}

    original_resolve_target = stock_deep_analysis._resolve_target
    original_profile = stock_deep_analysis._safe_profile_payload
    original_financials = stock_deep_analysis._safe_financial_payload
    original_contexts = stock_deep_analysis._assemble_contexts
    original_decision = stock_deep_analysis._safe_decision_summary
    original_persist_artifact = stock_deep_analysis._persist_artifact
    stock_deep_analysis._resolve_target = lambda _query: asyncio.sleep(
        0,
        result={
            "query": "600519",
            "resolved": True,
            "code": "600519",
            "name": "",
            "resolution_mode": "direct_code",
            "candidates": [],
        },
    )
    stock_deep_analysis._safe_profile_payload = lambda _code: asyncio.sleep(0, result=assembled["profile"] | {"found": True})
    stock_deep_analysis._safe_financial_payload = lambda _code: asyncio.sleep(
        0,
        result={"success": True, "data": assembled["financials"], "source": "unit-test"},
    )
    stock_deep_analysis._assemble_contexts = _fake_contexts
    stock_deep_analysis._safe_decision_summary = lambda _code, _style, _user_id: asyncio.sleep(
        0,
        result=assembled["decision"] | {"name": "贵州茅台"},
    )
    stock_deep_analysis._persist_artifact = _fake_persist_artifact
    try:
        payload = asyncio.run(run_stock_deep_analysis(code="600519", task="quick_scan"))
    finally:
        stock_deep_analysis._resolve_target = original_resolve_target
        stock_deep_analysis._safe_profile_payload = original_profile
        stock_deep_analysis._safe_financial_payload = original_financials
        stock_deep_analysis._assemble_contexts = original_contexts
        stock_deep_analysis._safe_decision_summary = original_decision
        stock_deep_analysis._persist_artifact = original_persist_artifact

    assert payload["status"] == "completed"
    assert payload["analysis_input"]["target"]["name"] == "贵州茅台"
    assert payload["analysis_gap_report"]["blocked"] is False
    assert payload["summary"]["report_ready"] is True
    assert "<html" in payload["analysis_report_bundle"]["standalone_html"].lower()


def test_trade_plan_task_generates_trade_plan_bundle():
    import akshare_mcp.services.stock_deep_analysis as stock_deep_analysis

    assembled = _assembled_payload() | {"task": "trade_plan"}

    async def _fake_contexts(_code: str, _user_id: str | None):
        contexts = assembled["contexts"]
        return contexts["stock"], contexts["quant"], contexts["event"], contexts["user"]

    async def _fake_persist_artifact(**_kwargs):
        return {}

    original_resolve_target = stock_deep_analysis._resolve_target
    original_profile = stock_deep_analysis._safe_profile_payload
    original_financials = stock_deep_analysis._safe_financial_payload
    original_contexts = stock_deep_analysis._assemble_contexts
    original_decision = stock_deep_analysis._safe_decision_summary
    original_persist_artifact = stock_deep_analysis._persist_artifact
    original_generate_plan = stock_deep_analysis.generate_plan
    stock_deep_analysis._resolve_target = lambda _query: asyncio.sleep(
        0,
        result={
            "query": "600519",
            "resolved": True,
            "code": "600519",
            "name": "",
            "resolution_mode": "direct_code",
            "candidates": [],
        },
    )
    stock_deep_analysis._safe_profile_payload = lambda _code: asyncio.sleep(0, result=assembled["profile"] | {"found": True})
    stock_deep_analysis._safe_financial_payload = lambda _code: asyncio.sleep(
        0,
        result={"success": True, "data": assembled["financials"], "source": "unit-test"},
    )
    stock_deep_analysis._assemble_contexts = _fake_contexts
    stock_deep_analysis._safe_decision_summary = lambda _code, _style, _user_id: asyncio.sleep(
        0,
        result=assembled["decision"] | {"name": "贵州茅台"},
    )
    stock_deep_analysis._persist_artifact = _fake_persist_artifact
    stock_deep_analysis.generate_plan = lambda _code, **_kwargs: asyncio.sleep(
        0,
        result={"action": "watch", "plan_id": "trade-plan-demo"},
    )
    try:
        payload = asyncio.run(run_stock_deep_analysis(code="600519", task="trade_plan"))
    finally:
        stock_deep_analysis._resolve_target = original_resolve_target
        stock_deep_analysis._safe_profile_payload = original_profile
        stock_deep_analysis._safe_financial_payload = original_financials
        stock_deep_analysis._assemble_contexts = original_contexts
        stock_deep_analysis._safe_decision_summary = original_decision
        stock_deep_analysis._persist_artifact = original_persist_artifact
        stock_deep_analysis.generate_plan = original_generate_plan

    assert payload["status"] == "completed"
    assert payload["analysis_input"]["target"]["name"] == "贵州茅台"
    assert payload["summary"]["report_ready"] is True
    assert payload["trade_plan"]["action"] == "watch"
    assert payload["trade_plan"]["plan_id"] == "trade-plan-demo"


def test_analysis_resources_read_persisted_artifacts():
    async def _scenario():
        run_id = f"stock-analysis-run-600519-{uuid4().hex[:8]}"
        code = "600519"
        summary_payload = {
            "run_id": run_id,
            "code": code,
            "task": "deep_analysis",
            "status": "completed",
            "summary": {
                "run_id": run_id,
                "code": code,
                "report_ready": True,
            },
            "analysis_report_bundle": {
                "run_id": run_id,
                "code": code,
                "one_paragraph_digest": "demo digest",
            },
        }
        report_payload = {
            "run_id": run_id,
            "code": code,
            "one_paragraph_digest": "demo digest",
            "standalone_html": "<html></html>",
        }
        await register_artifact_async(
            {
                "artifact_id": run_id,
                "artifact_type": "analysis_run_summary",
                "strategy": ANALYSIS_STRATEGY,
                "strategy_version": ANALYSIS_VERSION,
                "code": code,
                "payload": summary_payload,
            }
        )
        await register_artifact_async(
            {
                "artifact_id": f"{run_id}:report",
                "artifact_type": "analysis_report_bundle",
                "strategy": ANALYSIS_STRATEGY,
                "strategy_version": ANALYSIS_VERSION,
                "code": code,
                "payload": report_payload,
            }
        )

        summary_resource = await build_analysis_run_summary_payload(run_id)
        report_resource = await build_analysis_run_report_payload(run_id)
        latest_resource = await build_stock_deep_analysis_payload(code)
        return summary_resource, report_resource, latest_resource

    summary_resource, report_resource, latest_resource = asyncio.run(_scenario())

    assert summary_resource["summary"]["report_ready"] is True
    assert report_resource["standalone_html"] == "<html></html>"
    assert latest_resource["latest_run"]["run_id"].startswith("stock-analysis-run-600519-")


def test_resolve_target_marks_ambiguous_name_when_multiple_candidates():
    import akshare_mcp.services.stock_deep_analysis as stock_deep_analysis

    class _FakeConn:
        async def fetch(self, *_args, **_kwargs):
            return [
                {"code": "000001", "stock_name": "平安银行", "industry": "银行", "market_cap": 1},
                {"code": "601318", "stock_name": "中国平安", "industry": "保险", "market_cap": 2},
            ]

    class _Acquire:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeDb:
        def acquire(self):
            return _Acquire()

    original_get_db = stock_deep_analysis.get_db
    stock_deep_analysis.get_db = lambda: _FakeDb()
    try:
        resolved = asyncio.run(_resolve_target("平安"))
    finally:
        stock_deep_analysis.get_db = original_get_db

    assert resolved["resolved"] is False
    assert resolved["resolution_mode"] == "ambiguous_name"
    assert len(resolved["candidates"]) == 2


def test_rebuild_report_returns_structured_failure_when_prereqs_missing():
    async def _scenario():
        run_id = f"stock-analysis-run-600519-{uuid4().hex[:8]}"
        code = "600519"
        assembled = _assembled_payload() | {"run_id": run_id}
        summary_payload = {
            "run_id": run_id,
            "code": code,
            "task": "quick_scan",
            "status": "partial_failed",
            "steps": [{"stage": "integrity_gate", "status": "blocked", "success": False}],
            "summary": {"run_id": run_id, "code": code, "report_ready": False},
            "analysis_input": assembled,
        }
        await register_artifact_async(
            {
                "artifact_id": run_id,
                "artifact_type": "analysis_run_summary",
                "strategy": ANALYSIS_STRATEGY,
                "strategy_version": ANALYSIS_VERSION,
                "code": code,
                "payload": summary_payload,
            }
        )
        await register_artifact_async(
            {
                "artifact_id": f"{run_id}:input",
                "artifact_type": "analysis_input",
                "strategy": ANALYSIS_STRATEGY,
                "strategy_version": ANALYSIS_VERSION,
                "code": code,
                "payload": assembled,
            }
        )
        return await run_stock_deep_analysis(task="rebuild_report", run_id=run_id)

    payload = asyncio.run(_scenario())

    assert payload["status"] == "partial_failed"
    assert payload["task"] == "rebuild_report"
    assert payload["summary"]["report_ready"] is False
    assert "analysis_synthesis" in payload["summary"]["missing_prerequisites"]
    assert "missing rebuild prerequisites" in str(payload["error"])


def test_recover_gaps_reuses_existing_run_input_without_code():
    import akshare_mcp.services.stock_deep_analysis as stock_deep_analysis

    async def _fake_contexts(_code: str, _user_id: str | None):
        return (
            {"score": 68, "warnings": [], "fallback_reason": [], "market_snapshot": {}, "fund_flow_snapshot": {}},
            {"score": 64, "warnings": [], "fallback_reason": []},
            {"score": 58, "sentiment": "bullish", "candidate_actions": ["业绩催化"], "warnings": [], "fallback_reason": []},
            {"risk_bucket": "moderate", "warnings": [], "fallback_reason": []},
        )

    async def _scenario():
        run_id = f"stock-analysis-run-600519-{uuid4().hex[:8]}"
        input_payload = _assembled_payload() | {"run_id": run_id, "task": "quick_scan"}
        await register_artifact_async(
            {
                "artifact_id": f"{run_id}:input",
                "artifact_type": "analysis_input",
                "strategy": ANALYSIS_STRATEGY,
                "strategy_version": ANALYSIS_VERSION,
                "code": "600519",
                "payload": input_payload,
            }
        )

        original_profile = stock_deep_analysis._safe_profile_payload
        original_financials = stock_deep_analysis._safe_financial_payload
        original_contexts = stock_deep_analysis._assemble_contexts
        original_decision = stock_deep_analysis._safe_decision_summary
        stock_deep_analysis._safe_profile_payload = lambda _code: asyncio.sleep(0, result=input_payload["profile"])
        stock_deep_analysis._safe_financial_payload = lambda _code: asyncio.sleep(
            0,
            result={"success": True, "data": input_payload["financials"], "source": "unit-test"},
        )
        stock_deep_analysis._assemble_contexts = _fake_contexts
        stock_deep_analysis._safe_decision_summary = lambda _code, _style, _user_id: asyncio.sleep(
            0,
            result=input_payload["decision"],
        )
        try:
            return await run_stock_deep_analysis(task="recover_gaps", run_id=run_id)
        finally:
            stock_deep_analysis._safe_profile_payload = original_profile
            stock_deep_analysis._safe_financial_payload = original_financials
            stock_deep_analysis._assemble_contexts = original_contexts
            stock_deep_analysis._safe_decision_summary = original_decision

    payload = asyncio.run(_scenario())

    assert payload["status"] == "completed"
    assert payload["code"] == "600519"
    assert payload["analysis_gap_report"]["blocked"] is False
    assert payload["analysis_input"]["recovery_source_run_id"] == payload["run_id"]
    assert payload["analysis_report_bundle"] is None


def test_server_runtime_surface_exposes_stock_deep_analysis_contract():
    original_profile = os.environ.get("AKSHARE_MCP_STARTUP_PROFILE")
    os.environ["AKSHARE_MCP_STARTUP_PROFILE"] = "tool-only"
    try:
        if "akshare_mcp.server" in sys.modules:
            server_module = importlib.reload(sys.modules["akshare_mcp.server"])
        else:
            import akshare_mcp.server as server_module  # type: ignore
        app = server_module.mcp
        tools = getattr(app._tool_manager, "_tools", {})
        prompts = getattr(app._prompt_manager, "_prompts", {})
        templates = getattr(app._resource_manager, "_templates", {})
    finally:
        if original_profile is None:
            os.environ.pop("AKSHARE_MCP_STARTUP_PROFILE", None)
        else:
            os.environ["AKSHARE_MCP_STARTUP_PROFILE"] = original_profile

    assert "analyze_stock_product_workflow" in tools
    assert "stock-analysis-deep" in prompts
    assert "resource://stock/{code}/deep-analysis" in templates
    assert "resource://analysis-run/{run_id}/summary" in templates
    assert "resource://analysis-run/{run_id}/report" in templates
