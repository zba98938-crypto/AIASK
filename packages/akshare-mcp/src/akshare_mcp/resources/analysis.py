"""Deep-analysis MCP resources."""

from __future__ import annotations

from typing import Any

from ..services.stock_deep_analysis import (
    get_analysis_report_bundle,
    get_analysis_run_summary,
    get_latest_analysis_summary_for_code,
)


async def build_stock_deep_analysis_payload(code: str) -> dict[str, Any]:
    resolved = await get_latest_analysis_summary_for_code(code)
    if resolved.get("found") is False:
        return {
            "uri": f"resource://stock/{code}/deep-analysis",
            **resolved,
        }
    return {
        "uri": f"resource://stock/{code}/deep-analysis",
        "code": resolved.get("code"),
        "found": True,
        "latest_run": resolved,
    }


async def build_analysis_run_summary_payload(run_id: str) -> dict[str, Any]:
    payload = await get_analysis_run_summary(run_id)
    return {
        "uri": f"resource://analysis-run/{run_id}/summary",
        **payload,
    }


async def build_analysis_run_report_payload(run_id: str) -> dict[str, Any]:
    payload = await get_analysis_report_bundle(run_id)
    return {
        "uri": f"resource://analysis-run/{run_id}/report",
        **payload,
    }


def register(mcp) -> None:
    @mcp.resource(
        "resource://stock/{code}/deep-analysis",
        name="stock_deep_analysis",
        title="Stock Deep Analysis",
        description="Latest persisted deep-analysis run for a stock code",
        mime_type="application/json",
    )
    async def stock_deep_analysis(code: str) -> dict[str, Any]:
        return await build_stock_deep_analysis_payload(code)

    @mcp.resource(
        "resource://analysis-run/{run_id}/summary",
        name="analysis_run_summary",
        title="Analysis Run Summary",
        description="Read-only summary artifact for a stock deep-analysis run",
        mime_type="application/json",
    )
    async def analysis_run_summary(run_id: str) -> dict[str, Any]:
        return await build_analysis_run_summary_payload(run_id)

    @mcp.resource(
        "resource://analysis-run/{run_id}/report",
        name="analysis_run_report",
        title="Analysis Run Report",
        description="Read-only rendered report bundle for a stock deep-analysis run",
        mime_type="application/json",
    )
    async def analysis_run_report(run_id: str) -> dict[str, Any]:
        return await build_analysis_run_report_payload(run_id)
