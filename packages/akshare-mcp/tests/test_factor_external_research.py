from __future__ import annotations

import asyncio

from akshare_mcp.services import get_artifact_async, list_artifacts_async
from akshare_mcp.services.factor_candidate_storage import get_factor_candidate_record_async
from akshare_mcp.services.factor_candidate_vector_backfill import backfill_factor_candidate_vectors
from akshare_mcp.services.factor_external_research import (
    FACTOR_EXTERNAL_RESEARCH_STRATEGY,
    ingest_external_factor_research,
)
from akshare_mcp.services.factor_research_memory import get_factor_research_memory_service
from akshare_mcp.services.unified_vector_governance import audit_vector_collection_quality
from akshare_mcp.storage import close_db, get_db
from akshare_mcp.tools.managers._data_sync_manager_support_sync import (
    _sync_factor_external_research_ingest_now,
)
from akshare_mcp.tools.managers.quant_mgr_validation import _resolve_candidate_for_validation


def test_external_factor_research_ingest_review_only_and_vectorized(tmp_path, monkeypatch):
    db_path = str(tmp_path / "external_factor.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            sources = [
                {
                    "source": "Mock Public Research",
                    "url": "https://example.com/mock-factor-momentum",
                    "title": "Mock Momentum Evidence",
                    "published_at": "2026-01-02",
                    "summary": "A public abstract suggests trend and momentum signals, but requires local validation.",
                    "extracted_factor_idea": "momentum candidate should be tested with local RankIC and OOS gates",
                    "factor_family": "momentum",
                    "license": "public_metadata_only",
                },
                {
                    "source": "Mock Public Research",
                    "url": "https://example.com/mock-factor-profitability",
                    "title": "Mock Profitability Evidence",
                    "summary": "A profitability idea has no supported local DSL mapping yet.",
                    "extracted_factor_idea": "profitability accounting factor",
                    "factor_family": "profitability",
                    "license": "public_metadata_only",
                },
            ]
            result = await ingest_external_factor_research(
                db,
                sources=sources,
                allow_network=False,
                codes=["000001", "000002", "000003", "000004"],
                limit=5,
            )
            assert result["saved_evidence_records"] == 2
            assert result["saved_candidate_records"] == 1
            assert result["compile_valid_records"] == 1
            assert result["skipped_candidate_records"] == 1
            assert result["skipped_reasons"]["no_supported_local_dsl_mapping"] == 1

            evidence = await get_artifact_async(result["evidence_ids"][0])
            assert evidence["strategy"] == FACTOR_EXTERNAL_RESEARCH_STRATEGY
            evidence_payload = evidence["payload"]
            assert evidence_payload["provenance"]["body_stored"] is False
            assert evidence_payload["license"] == "public_metadata_only"

            memory = await get_factor_candidate_record_async(result["candidate_artifact_ids"][0])
            assert memory["status"] == "review"
            assert "requires_validation" in memory["tags"]
            assert "external_factor_research" in memory["tags"]
            assert memory["memory_flags"]["active_pool_eligible"] is False
            assert memory["memory_flags"]["active_pool_block_reasons"] == ["requires_local_validation"]
            assert memory["external_evidence"][0]["url"] == sources[0]["url"]

            resolved = await _resolve_candidate_for_validation(
                {"artifact_id": memory["artifact_id"]},
                get_artifact_async_fn=get_artifact_async,
            )
            assert resolved["resolved_from"] == "factor_candidate_memory"
            assert resolved["candidate"]["expression_dsl"] == "zscore(momentum_20d, 20) + zscore(momentum_60d, 20)"

            validation_rows = await list_artifacts_async(
                limit=10,
                strategy="quant_factor_candidate_validation",
            )
            assert validation_rows == []

            backfill = await backfill_factor_candidate_vectors(
                db,
                limit=5,
                status="review",
                version="v1",
            )
            assert backfill["saved_profiles"] >= 1
            profiles = await db.list_vector_profiles(
                collection_name="factor_candidate_embeddings",
                version="v1",
                limit=5,
            )
            target_profiles = [
                item
                for item in profiles
                if item["entity_id"] == memory["artifact_id"]
            ]
            assert target_profiles
            assert target_profiles[0]["vector_dim"] == 128
            assert target_profiles[0]["metadata"]["external_evidence_count"] == 1

            stats = await get_factor_research_memory_service().summarize_memory_records(limit=50)
            assert stats["external_evidence_records"] >= 1
            assert stats["unvalidated_external_records"] >= 1
            assert stats["quality_flags"]["unvalidated_external_evidence"] is True
            qa = await audit_vector_collection_quality(
                db,
                collection_name="factor_candidate_embeddings",
                profile_version="v1",
            )
            assert qa["factor_quality_governance"]["external_evidence_count"] >= 1
        finally:
            await close_db()

    asyncio.run(_run())


def test_data_sync_external_factor_research_task(tmp_path, monkeypatch):
    db_path = str(tmp_path / "external_factor_sync.sqlite3")
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            result = await _sync_factor_external_research_ingest_now(
                {
                    "sources": [
                        {
                            "source": "Mock Public Research",
                            "url": "https://example.com/mock-factor-liquidity",
                            "title": "Mock Liquidity Evidence",
                            "summary": "Liquidity and turnover ideas should be validated locally.",
                            "extracted_factor_idea": "liquidity candidate based on volume pressure",
                            "factor_family": "liquidity",
                            "license": "public_metadata_only",
                        }
                    ],
                    "allow_network": False,
                    "limit": 1,
                    "codes": ["000001", "000002", "000003", "000004"],
                    "backfill_vectors": True,
                }
            )
            assert result["success"] == 1
            assert result["ingest"]["saved_evidence_records"] == 1
            assert result["ingest"]["saved_candidate_records"] == 1
            assert result["backfill"]["saved_profiles"] >= 1
            assert "factor_external_research" in result["message"]
        finally:
            await close_db()

    asyncio.run(_run())
