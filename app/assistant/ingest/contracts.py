"""Types consumed by the gut (IngestService).

IngestEnvelope is an alias of signal_router's SignalEnvelope — the two live
at the same abstraction level (a single normalized inbound item). Keeping
them as one type avoids conversion at the signal_router subscriber boundary
and keeps existing signal_router code working unchanged while the gut is
extracted.

IngestSource is the contract every inbound puller must satisfy.
IngestSubscriber is the callable signature for registered consumers.
"""
from __future__ import annotations

from typing import Callable, List, Protocol, runtime_checkable

from app.assistant.signal_router.contracts import SignalEnvelope


IngestEnvelope = SignalEnvelope


IngestSubscriber = Callable[[IngestEnvelope], None]


@runtime_checkable
class IngestSource(Protocol):
    """A source the gut pulls from on each tick.

    Implementations own their own cursor. Calling ``pull()`` returns every
    new envelope since the previous call and atomically advances the cursor.
    An empty list means "nothing new since last pull."
    """

    name: str

    def pull(self) -> List[IngestEnvelope]: ...
