"""Market Event Ingest public contracts owned by Strategy Factory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class NormalizedEventDecision:
    """Normalization decision for a raw market event."""

    event_id: str
    should_normalize: bool
    normalized_title: str | None = None
    normalized_category: str | None = None
    normalized_sentiment: str | None = None
    skip_reason: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class FactoryEventBridgePayload:
    """Payload for bridging normalized events to Strategy Factory."""

    event_id: str
    source_type: str  # 'news', 'notice', 'research'
    title: str
    category: str
    sentiment: str | None
    codes: list[str]  # Affected stock codes
    published_at: datetime
    bridge_decision: str  # 'cluster', 'signal', 'skip'
    cluster_id: str | None = None
    signal_strength: float | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class MarketEventIngestSummary:
    """Summary of one complete market event ingest run."""

    run_id: str
    trigger: str
    start_time: datetime
    end_time: datetime
    duration_sec: float
    status: str  # 'completed', 'partial', 'failed'
    events_fetched: int
    events_normalized: int
    events_bridged: int
    clusters_created: int
    signals_created: int
    error: str | None = None


__all__ = [
    "FactoryEventBridgePayload",
    "MarketEventIngestSummary",
    "NormalizedEventDecision",
]
