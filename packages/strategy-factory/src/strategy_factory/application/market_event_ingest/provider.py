"""Provider abstraction for market event ingest support services."""

from __future__ import annotations

from typing import Any, Protocol


class MarketEventIngestSupport(Protocol):
    """Protocol defining support services required by market event ingest orchestrator.

    This protocol abstracts the concrete implementation (fetch, parse, persist)
    as provider contracts, allowing Strategy Factory to own the bridge and
    normalization decision logic while delegating data source integration to
    host-provided services.
    """

    async def fetch_official_market_events(
        self,
        db: Any,
        *,
        lookback_days: int = 7,
    ) -> list[dict[str, Any]]:
        """Fetch official market event documents from external sources.

        Returns:
            List of raw event dicts with keys: event_id, source_type, title,
            content, published_at, url
        """
        ...

    async def list_normalized_events(
        self,
        db: Any,
        *,
        since_days: int = 7,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List previously normalized events from persistence layer.

        Returns:
            List of normalized event dicts
        """
        ...

    async def save_normalized_event(
        self,
        db: Any,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Save a normalized event to persistence layer.

        Returns:
            dict with keys: saved, event_id
        """
        ...

    async def save_factory_event_cluster(
        self,
        db: Any,
        cluster: dict[str, Any],
    ) -> dict[str, Any]:
        """Save a factory event cluster.

        Returns:
            dict with keys: cluster_id, saved
        """
        ...

    async def save_factory_event_signal(
        self,
        db: Any,
        signal: dict[str, Any],
    ) -> dict[str, Any]:
        """Save a factory event signal.

        Returns:
            dict with keys: signal_id, saved
        """
        ...


__all__ = ["MarketEventIngestSupport"]
