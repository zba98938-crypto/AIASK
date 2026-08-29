from __future__ import annotations

import asyncio


def test_factor_candidate_registry_quality_governance_uses_absolute_service_import(monkeypatch):
    import akshare_mcp.services as services_module
    import akshare_mcp.tools.managers.quant_mgr_registry as registry_module

    class FakeMemoryService:
        async def summarize_memory_records(self, *, limit: int) -> dict:
            return {
                "external_evidence_records": 1,
                "unvalidated_external_records": 0,
                "validated_external_records": 1,
                "candidate_source_counts": {"local": 1},
                "quality_flags": {"ok": True},
                "status_counts": {"validated": 1},
                "duplicate_like_count": 0,
                "failure_pattern_count": 0,
                "unstable_count": 0,
            }

    async def fake_list_items(**_kwargs):
        return []

    def ok(data, **metadata):
        return {"success": True, "data": data, **metadata}

    def fail(error, **metadata):
        return {"success": False, "error": error, **metadata}

    monkeypatch.setattr(
        services_module,
        "get_factor_research_memory_service",
        lambda: FakeMemoryService(),
    )
    monkeypatch.setattr(registry_module, "_list_factor_candidate_registry_items", fake_list_items)

    result = asyncio.run(
        registry_module.handle_factor_candidate_registry(
            kw={"op": "active_pool", "limit": 20, "market_codes_only": True},
            ok=ok,
            fail=fail,
            filter_market_codes=lambda values: [str(item) for item in list(values or [])],
        )
    )

    quality_governance = result["data"]["quality_governance"]
    assert result["success"] is True
    assert quality_governance["available"] is True
    assert quality_governance["external_evidence_count"] == 1
