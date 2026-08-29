"""Market Event Ingest orchestration logic owned by Strategy Factory.

This orchestrator coordinates the event ingest cycle:
1. Fetch raw events from external sources
2. Normalize events (decision logic)
3. Bridge events to Strategy Factory (cluster/signal creation)

The actual data source integration is delegated to MarketEventIngestSupport,
allowing Strategy Factory to own the bridge contract and normalization logic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .contracts import (
    FactoryEventBridgePayload,
    MarketEventIngestSummary,
    NormalizedEventDecision,
)
from .provider import MarketEventIngestSupport

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SEC = 300
BRIDGE_TIMEOUT_SEC = 180


class MarketEventIngestOrchestrator:
    """Orchestrator for market event ingest cycles.

    Owns the bridge contract and normalization decision logic while delegating
    data source integration to the support provider.
    """

    def __init__(self, support: MarketEventIngestSupport):
        self._support = support

    async def run_cycle(
        self,
        *,
        trigger: str = "scheduled",
        lookback_days: int = 7,
    ) -> dict[str, Any]:
        """Execute one complete market event ingest cycle.

        Args:
            trigger: What triggered this run
            lookback_days: How many days to look back for events

        Returns:
            MarketEventIngestSummary as dict
        """
        run_id = uuid4().hex[:12]
        start_time = datetime.now(timezone.utc)
        logger.info(
            "MarketEventIngestOrchestrator: starting run %s (trigger=%s, lookback=%d)",
            run_id,
            trigger,
            lookback_days,
        )

        db = await self._get_db()

        try:
            # Phase 1: Fetch raw events
            logger.info("MarketEventIngestOrchestrator [%s] Phase 1: Fetch", run_id)
            raw_events = await asyncio.wait_for(
                self._support.fetch_official_market_events(
                    db, lookback_days=lookback_days
                ),
                timeout=FETCH_TIMEOUT_SEC,
            )
            events_fetched = len(raw_events)
            logger.info(
                "MarketEventIngestOrchestrator [%s]: fetched %d raw events",
                run_id,
                events_fetched,
            )

            # Phase 2: Normalize and bridge
            logger.info("MarketEventIngestOrchestrator [%s] Phase 2: Normalize & Bridge", run_id)
            events_normalized = 0
            events_bridged = 0
            clusters_created = 0
            signals_created = 0

            for raw_event in raw_events:
                try:
                    # Normalization decision
                    decision = self._make_normalization_decision(raw_event)

                    if decision.should_normalize:
                        # Save normalized event
                        normalized = {
                            "event_id": decision.event_id,
                            "title": decision.normalized_title,
                            "category": decision.normalized_category,
                            "sentiment": decision.normalized_sentiment,
                            "source_type": raw_event.get("source_type"),
                            "published_at": raw_event.get("published_at"),
                            "metadata": decision.metadata,
                        }
                        await self._support.save_normalized_event(db, normalized)
                        events_normalized += 1

                        # Bridge decision
                        bridge_payload = self._make_bridge_decision(normalized, raw_event)
                        if bridge_payload.bridge_decision == "cluster":
                            cluster_result = await self._support.save_factory_event_cluster(
                                db,
                                {
                                    "cluster_id": bridge_payload.cluster_id,
                                    "title": bridge_payload.title,
                                    "category": bridge_payload.category,
                                    "codes": bridge_payload.codes,
                                    "published_at": bridge_payload.published_at.isoformat(),
                                    "metadata": bridge_payload.metadata,
                                },
                            )
                            if cluster_result.get("saved"):
                                clusters_created += 1
                                events_bridged += 1

                        elif bridge_payload.bridge_decision == "signal":
                            signal_result = await self._support.save_factory_event_signal(
                                db,
                                {
                                    "event_id": bridge_payload.event_id,
                                    "title": bridge_payload.title,
                                    "codes": bridge_payload.codes,
                                    "signal_strength": bridge_payload.signal_strength,
                                    "sentiment": bridge_payload.sentiment,
                                    "published_at": bridge_payload.published_at.isoformat(),
                                    "metadata": bridge_payload.metadata,
                                },
                            )
                            if signal_result.get("saved"):
                                signals_created += 1
                                events_bridged += 1

                except Exception as exc:
                    logger.warning(
                        "MarketEventIngestOrchestrator [%s]: failed processing event %s: %s",
                        run_id,
                        raw_event.get("event_id", "unknown"),
                        exc,
                    )

            end_time = datetime.now(timezone.utc)
            duration_sec = (end_time - start_time).total_seconds()

            summary = MarketEventIngestSummary(
                run_id=run_id,
                trigger=trigger,
                start_time=start_time,
                end_time=end_time,
                duration_sec=duration_sec,
                status="completed",
                events_fetched=events_fetched,
                events_normalized=events_normalized,
                events_bridged=events_bridged,
                clusters_created=clusters_created,
                signals_created=signals_created,
                error=None,
            )

            logger.info(
                "MarketEventIngestOrchestrator [%s]: completed in %.1fs (fetched=%d, normalized=%d, bridged=%d)",
                run_id,
                duration_sec,
                events_fetched,
                events_normalized,
                events_bridged,
            )

            return self._summary_to_dict(summary)

        except asyncio.TimeoutError:
            end_time = datetime.now(timezone.utc)
            duration_sec = (end_time - start_time).total_seconds()
            logger.error(
                "MarketEventIngestOrchestrator [%s]: timeout after %.1fs",
                run_id,
                duration_sec,
            )

            summary = MarketEventIngestSummary(
                run_id=run_id,
                trigger=trigger,
                start_time=start_time,
                end_time=end_time,
                duration_sec=duration_sec,
                status="failed",
                events_fetched=0,
                events_normalized=0,
                events_bridged=0,
                clusters_created=0,
                signals_created=0,
                error="timeout",
            )

            return self._summary_to_dict(summary)

        except Exception as exc:
            end_time = datetime.now(timezone.utc)
            duration_sec = (end_time - start_time).total_seconds()
            logger.exception(
                "MarketEventIngestOrchestrator [%s]: cycle failed: %s", run_id, exc
            )

            summary = MarketEventIngestSummary(
                run_id=run_id,
                trigger=trigger,
                start_time=start_time,
                end_time=end_time,
                duration_sec=duration_sec,
                status="failed",
                events_fetched=0,
                events_normalized=0,
                events_bridged=0,
                clusters_created=0,
                signals_created=0,
                error=str(exc),
            )

            return self._summary_to_dict(summary)

    def _make_normalization_decision(
        self, raw_event: dict[str, Any]
    ) -> NormalizedEventDecision:
        """Make normalization decision for a raw event.

        This is the core normalization logic owned by Strategy Factory.
        """
        event_id = str(raw_event.get("event_id", ""))
        title = str(raw_event.get("title", "")).strip()
        source_type = str(raw_event.get("source_type", "")).lower()

        # Skip empty titles
        if not title:
            return NormalizedEventDecision(
                event_id=event_id,
                should_normalize=False,
                skip_reason="empty_title",
            )

        # Basic normalization
        normalized_title = title
        normalized_category = self._infer_category(title, source_type)
        normalized_sentiment = self._infer_sentiment(title, source_type)

        return NormalizedEventDecision(
            event_id=event_id,
            should_normalize=True,
            normalized_title=normalized_title,
            normalized_category=normalized_category,
            normalized_sentiment=normalized_sentiment,
            metadata={"source_type": source_type},
        )

    def _make_bridge_decision(
        self, normalized: dict[str, Any], raw_event: dict[str, Any]
    ) -> FactoryEventBridgePayload:
        """Make bridge decision for a normalized event.

        This is the core bridge logic owned by Strategy Factory.
        """
        event_id = str(normalized.get("event_id", ""))
        title = str(normalized.get("title", ""))
        category = str(normalized.get("category", "general"))
        sentiment = normalized.get("sentiment")
        codes = self._extract_codes(raw_event)
        published_at = self._parse_datetime(raw_event.get("published_at"))

        # Decision: cluster if multi-stock, signal if single-stock
        if len(codes) > 1:
            bridge_decision = "cluster"
            cluster_id = f"evt_{uuid4().hex[:8]}"
            signal_strength = None
        elif len(codes) == 1:
            bridge_decision = "signal"
            cluster_id = None
            signal_strength = self._compute_signal_strength(sentiment, category)
        else:
            bridge_decision = "skip"
            cluster_id = None
            signal_strength = None

        return FactoryEventBridgePayload(
            event_id=event_id,
            source_type=str(normalized.get("source_type", "unknown")),
            title=title,
            category=category,
            sentiment=sentiment,
            codes=codes,
            published_at=published_at,
            bridge_decision=bridge_decision,
            cluster_id=cluster_id,
            signal_strength=signal_strength,
            metadata=normalized.get("metadata"),
        )

    def _infer_category(self, title: str, source_type: str) -> str:
        """Infer event category from title and source."""
        title_lower = title.lower()
        if "业绩" in title_lower or "财报" in title_lower:
            return "earnings"
        if "重组" in title_lower or "并购" in title_lower:
            return "ma"
        if "监管" in title_lower or "处罚" in title_lower:
            return "regulatory"
        if source_type == "research":
            return "research"
        return "general"

    def _infer_sentiment(self, title: str, source_type: str) -> str | None:
        """Infer sentiment from title."""
        title_lower = title.lower()
        positive_keywords = ["利好", "增长", "突破", "上涨", "创新高"]
        negative_keywords = ["利空", "下跌", "风险", "亏损", "处罚"]

        positive_count = sum(1 for kw in positive_keywords if kw in title_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in title_lower)

        if positive_count > negative_count:
            return "positive"
        if negative_count > positive_count:
            return "negative"
        return "neutral"

    def _extract_codes(self, raw_event: dict[str, Any]) -> list[str]:
        """Extract stock codes from raw event."""
        codes = raw_event.get("codes", [])
        if isinstance(codes, list):
            return [str(c).strip() for c in codes if str(c).strip()]
        return []

    def _parse_datetime(self, value: Any) -> datetime:
        """Parse datetime from various formats."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                pass
        return datetime.now(timezone.utc)

    def _compute_signal_strength(
        self, sentiment: str | None, category: str
    ) -> float:
        """Compute signal strength based on sentiment and category."""
        base_strength = 0.5

        if sentiment == "positive":
            base_strength += 0.2
        elif sentiment == "negative":
            base_strength -= 0.2

        if category in ("earnings", "ma"):
            base_strength += 0.1

        return max(0.0, min(1.0, base_strength))

    async def _get_db(self) -> Any:
        """Resolve the database connection through the provider protocol.

        Prefers the public ``get_db()``; the underscored name is kept as a
        fallback for hosts that predate it. Same resolution order as the
        incubation orchestrator, so both paths behave identically.
        """
        public = getattr(self._support, "get_db", None)
        if callable(public):
            return await public()
        legacy = getattr(self._support, "_get_db", None)
        if callable(legacy):
            return await legacy()
        raise RuntimeError(
            "MarketEventIngestSupport must expose get_db() (or legacy _get_db())"
        )

    def _summary_to_dict(self, summary: MarketEventIngestSummary) -> dict[str, Any]:
        """Convert summary to dict for compatibility."""
        return {
            "run_id": summary.run_id,
            "trigger": summary.trigger,
            "start_time": summary.start_time.isoformat(),
            "end_time": summary.end_time.isoformat(),
            "duration_sec": summary.duration_sec,
            "status": summary.status,
            "events_fetched": summary.events_fetched,
            "events_normalized": summary.events_normalized,
            "events_bridged": summary.events_bridged,
            "clusters_created": summary.clusters_created,
            "signals_created": summary.signals_created,
            "error": summary.error,
        }


__all__ = ["MarketEventIngestOrchestrator"]
