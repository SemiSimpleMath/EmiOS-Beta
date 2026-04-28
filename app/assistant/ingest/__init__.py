"""The gut — unified inbound intake.

Scans all inbound message sources (global blackboard, email repo, future:
SMS, calendar, etc.) on a schedule, normalizes each item into an
IngestEnvelope, and fans them out to registered subscribers.

Two downstream consumers are planned:
- SignalRouterService (reactive watch matching, already exists)
- PodClassifierService (declarative pod minting, to be built)

In prose we call this "the gut." In code: ``app/assistant/ingest/``,
``IngestService``, ``IngestEnvelope``, ``IngestSource``, ``IngestSubscriber``.
"""
from app.assistant.ingest.contracts import (
    IngestEnvelope,
    IngestSource,
    IngestSubscriber,
)
from app.assistant.ingest.ingest_service import IngestService

__all__ = [
    "IngestEnvelope",
    "IngestService",
    "IngestSource",
    "IngestSubscriber",
]
