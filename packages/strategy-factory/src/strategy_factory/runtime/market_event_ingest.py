"""Canonical market event ingest runtime owned by Strategy Factory.

Phase 2D complete: Orchestration logic now lives in application layer.
"""

from __future__ import annotations

import inspect
from typing import Any

from ..application.market_event_ingest import MarketEventIngestOrchestrator
from ..infrastructure.runtime_services import (
    get_db_provider,
    get_market_event_ingest_support_factory,
)


# Default lookback window when neither ``lookback_days`` nor its ``notice_days``
# alias is supplied by the caller.
DEFAULT_LOOKBACK_DAYS = 7


class MarketEventIngestRuntime:
    """Host-neutral market event ingest runtime with Strategy Factory-owned orchestration.

    Phase 2D: Delegates to application-layer orchestrator for bridge and
    normalization decision logic.
    """

    def __init__(self, support: Any, db_provider: Any | None = None):
        self._support = support
        self._db_provider = db_provider
        self._orchestrator = MarketEventIngestOrchestrator(support) if support else None

    def preflight(self) -> dict[str, Any]:
        return {
            "available": self._support is not None,
            "runtime_type": type(self._support).__name__ if self._support is not None else None,
            "orchestrator_ready": self._orchestrator is not None,
            "db_provider_available": self._db_provider is not None,
        }

    def status(self) -> dict[str, Any]:
        status = getattr(self._support, "status", None)
        if callable(status):
            return dict(status() or {})
        return self.preflight()

    async def run_once(
        self,
        *,
        trigger: str = "scheduled",
        lookback_days: int | None = None,
        db: Any | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute one complete market event ingest cycle.

        Phase 2D: Delegates to application-layer orchestrator.

        ``notice_days`` is the caller-facing name for the same lookback window
        used by the legacy ingest surface. Accept it as an alias so callers that
        only pass ``notice_days`` are not silently downgraded to the default
        window on the modern orchestrator path.
        """
        notice_days = kwargs.pop("notice_days", None)
        if lookback_days is None:
            lookback_days = notice_days if notice_days is not None else DEFAULT_LOOKBACK_DAYS
        lookback_days = max(1, int(lookback_days))
        # Keep the legacy support surface fed with the name it expects.
        kwargs["notice_days"] = lookback_days

        if self._support is None:
            return {
                "success": False,
                "error": "market_event_ingest_support_missing",
                "trigger": trigger,
                "strategy_factory_bridge": {},
                "quality_flags": ["support_missing"],
                "totals": {"errors": 1},
            }

        # Hosts predating the public application protocol expose the complete
        # text-ingest support surface through underscored methods. Keep that
        # compatibility at the runtime boundary instead of leaking private
        # fallbacks into the application orchestrator.
        if hasattr(self._support, "_resolve_runtime_args"):
            return await self._run_legacy_ingest(db=db, lookback_days=lookback_days, **kwargs)

        return await self._orchestrator.run_cycle(trigger=trigger, lookback_days=lookback_days)

    async def _resolve_db(self, explicit_db: Any | None) -> Any:
        if explicit_db is not None:
            return explicit_db
        if self._db_provider is None:
            raise RuntimeError("market_event_ingest_db_provider_missing")
        resolved = self._db_provider()
        if inspect.isawaitable(resolved):
            resolved = await resolved
        initialize = getattr(resolved, "initialize", None)
        if callable(initialize):
            initialized = initialize()
            if inspect.isawaitable(initialized):
                await initialized
        return resolved

    async def _run_legacy_ingest(
        self,
        *,
        db: Any | None,
        lookback_days: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        support = self._support
        resolved_db = await self._resolve_db(db)
        # ``lookback_days`` is authoritative: run_once already folded any
        # ``notice_days`` alias into it before dispatching here.
        runtime_args = support._resolve_runtime_args({**kwargs, "notice_days": lookback_days})
        requested_doc_types = list(runtime_args["requested_doc_types"])
        requested_codes = list(runtime_args["requested_codes"])
        dry_run = bool(runtime_args["dry_run"])
        result: dict[str, Any] = {
            "args": dict(runtime_args["args_payload"]),
            "source_status": support._event_source_status(),
            "fetched": {},
            "saved": {},
            "normalized_events": {},
            "strategy_factory_bridge": {},
            "snapshots": [],
            "final_counts": {},
            "errors": [],
            "quality_flags": [],
        }

        if "news" in requested_doc_types:
            await support._ingest_news(
                resolved_db,
                result,
                news_limit=runtime_args["news_limit"],
                embed=runtime_args["embed"],
                chunk_size=runtime_args["chunk_size"],
                overlap=runtime_args["overlap"],
                version=runtime_args["version"],
                dry_run=dry_run,
            )

        notice_codes: list[str] = []
        if "notice" in requested_doc_types and runtime_args["allow_network"]:
            official = support._fetch_official_market_event_documents(
                runtime_args["start_date"].isoformat(),
                runtime_args["end_date"].isoformat(),
                limit=runtime_args["official_notice_limit"],
                stock_codes=requested_codes or None,
            )
            official_items = list((official or {}).get("items") or [])
            result["fetched"]["official_notice"] = len(official_items)
            result["source_status"].update(dict((official or {}).get("sources") or {}))
            if official_items:
                saved_total = {"documents": 0, "chunks": 0, "embedded_chunks": 0, "headline_labels": 0}
                event_total = {"total": 0, "verified": 0, "provisional": 0, "degraded": 0, "rejected": 0, "latest": []}
                by_code: dict[str, list[dict[str, Any]]] = {}
                for item in official_items:
                    code = str(item.get("stock_code") or item.get("code") or "MARKET")
                    by_code.setdefault(code, []).append(item)
                    if code != "MARKET" and code not in notice_codes:
                        notice_codes.append(code)
                if not dry_run:
                    for code, items in by_code.items():
                        saved = await resolved_db.save_market_documents(
                            code,
                            "notice",
                            items,
                            embed=runtime_args["embed"],
                            chunk_size=runtime_args["chunk_size"],
                            overlap=runtime_args["overlap"],
                            version=runtime_args["version"],
                        )
                        support._merge_saved_totals(saved_total, saved)
                        event_summary = await support._persist_normalized_events(
                            resolved_db, code, "notice", items
                        )
                        support._merge_event_summary(event_total, event_summary)
                else:
                    saved_total = {"candidate_docs": len(official_items), "dry_run": True}
                result["saved"]["official_notice"] = saved_total
                result["normalized_events"]["official_notice"] = event_total

            raw_notices = support._fetch_notice_head(
                runtime_args["start_date"],
                runtime_args["end_date"],
                runtime_args["notice_limit"],
                result,
            )
            mapped_notices = [support._map_notice_item(item) for item in raw_notices]
            mapped_notices = [item for item in mapped_notices if item.get("title")]
            result["fetched"]["notice_head"] = len(mapped_notices)
            if mapped_notices and not dry_run:
                saved = await resolved_db.save_market_documents(
                    "MARKET", "notice", mapped_notices,
                    embed=runtime_args["embed"], chunk_size=runtime_args["chunk_size"],
                    overlap=runtime_args["overlap"], version=runtime_args["version"],
                )
                result["saved"]["notice_head"] = saved
                result["normalized_events"]["notice_head"] = await support._persist_normalized_events(
                    resolved_db, "MARKET", "notice", mapped_notices
                )

            code_universe = await support._select_stock_universe(
                resolved_db,
                limit=runtime_args["code_notice_code_limit"],
                extra_codes=requested_codes or notice_codes,
            )
            code_saved = {"documents": 0, "chunks": 0, "embedded_chunks": 0, "headline_labels": 0}
            for code in code_universe:
                items = await support._fetch_code_notice_items(
                    start_date=runtime_args["start_date"], end_date=runtime_args["end_date"],
                    code=code, limit=runtime_args["code_notice_limit"],
                )
                if items and not dry_run:
                    saved = await resolved_db.save_market_documents(
                        code, "notice", items,
                        embed=runtime_args["embed"], chunk_size=runtime_args["chunk_size"],
                        overlap=runtime_args["overlap"], version=runtime_args["version"],
                    )
                    support._merge_saved_totals(code_saved, saved)
                    await support._persist_normalized_events(resolved_db, code, "notice", items)
            result["saved"]["code_notices"] = code_saved

        if "research" in requested_doc_types:
            await support._ingest_research(
                resolved_db,
                result,
                requested_codes=requested_codes,
                notice_codes=notice_codes,
                research_code_limit=runtime_args["research_code_limit"],
                research_per_code=runtime_args["research_per_code"],
                embed=runtime_args["embed"],
                chunk_size=runtime_args["chunk_size"],
                overlap=runtime_args["overlap"],
                version=runtime_args["version"],
                dry_run=dry_run,
            )

        if not dry_run:
            result["strategy_factory_bridge"] = await support._bridge_normalized_events_to_strategy_factory(
                resolved_db, limit=50
            )
            if runtime_args["build_snapshot"]:
                result["snapshots"] = await support._build_market_doc_snapshots(
                    resolved_db,
                    doc_types=requested_doc_types,
                    activate=runtime_args["activate_snapshot"],
                    dry_run=False,
                )
            result["final_counts"] = await support._load_final_counts(resolved_db)
        result["totals"] = {"errors": len(result["errors"])}
        return result


def build_market_event_ingest_runtime(
    *, support: Any | None = None, db_provider: Any | None = None
) -> MarketEventIngestRuntime:
    resolved_support = support if support is not None else get_market_event_ingest_support_factory()()
    if db_provider is None:
        db_provider = get_db_provider()
    return MarketEventIngestRuntime(resolved_support, db_provider=db_provider)


def get_market_event_ingest_runtime() -> MarketEventIngestRuntime:
    return build_market_event_ingest_runtime()


def get_market_event_ingest_factory() -> MarketEventIngestRuntime:
    return get_market_event_ingest_runtime()


__all__ = [
    "MarketEventIngestRuntime",
    "build_market_event_ingest_runtime",
    "get_market_event_ingest_factory",
    "get_market_event_ingest_runtime",
]
