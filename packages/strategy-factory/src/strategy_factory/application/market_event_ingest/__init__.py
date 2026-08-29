"""Market Event Ingest application-layer orchestration.

Migrated from akshare-mcp to establish Strategy Factory ownership of the
event bridge contract and normalization decision logic.
"""

from .contracts import (
    MarketEventIngestSummary,
    NormalizedEventDecision,
    FactoryEventBridgePayload,
)
from .orchestrator import MarketEventIngestOrchestrator

__all__ = [
    "FactoryEventBridgePayload",
    "MarketEventIngestOrchestrator",
    "MarketEventIngestSummary",
    "NormalizedEventDecision",
]
