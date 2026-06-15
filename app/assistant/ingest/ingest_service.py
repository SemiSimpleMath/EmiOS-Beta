"""IngestService — the gut.

Polls every registered ingest source on a schedule and fans the resulting
IngestEnvelopes to every registered subscriber. Subscribers may be
signal_router (reactive watches), the pod classifier, or anything else
that needs to see inbound in near-real-time.

## Design choices

- **Direct subscriber callbacks**, not EventHub pub/sub. Strict ordering,
  tighter debugging, one fewer moving part. If we ever need async fan-out
  we can swap later.
- **Subscribers are sequential per envelope** — if subscriber A raises, B
  still fires. Exceptions are logged and contained so a broken subscriber
  never stalls the loop.
- **No internal dedupe or garbage filter** — sources own their cursor
  (so nothing is re-delivered) and any noise filtering is a subscriber
  concern, or lives in a future IngestNoiseFilter between the source and
  the fan-out. Keeping the service itself minimal means fewer hidden
  behaviors to reason about.
"""
from __future__ import annotations

import os
import threading
import time
from typing import List

from app.assistant.ingest.contracts import IngestEnvelope, IngestSource, IngestSubscriber
from app.assistant.runtime import start_monitored_thread
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class IngestService:
    def __init__(
        self,
        sources: List[IngestSource],
        *,
        poll_interval_seconds: int = 120,
        startup_delay_seconds: int = 0,
    ) -> None:
        if not sources:
            raise ValueError("IngestService requires at least one source")
        self._sources: List[IngestSource] = list(sources)
        self._poll_interval_seconds = max(5, int(poll_interval_seconds))
        # Optional quiet window before the FIRST poll. Default 0 (immediate, as
        # before — keeps tests deterministic); production sets it so the boot poll
        # doesn't contend with a user's first interaction right after restart.
        self._startup_delay_seconds = max(0, int(startup_delay_seconds))
        self._subscribers: List[IngestSubscriber] = []
        self._subscribers_lock = threading.RLock()

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    def register_subscriber(self, fn: IngestSubscriber) -> None:
        """Register a callback to receive every IngestEnvelope."""
        if not callable(fn):
            raise ValueError("subscriber must be callable")
        with self._subscribers_lock:
            self._subscribers.append(fn)
        logger.info("IngestService subscriber registered: %r", getattr(fn, "__name__", fn))

    def tick_once(self) -> int:
        """Run one poll cycle across all sources. Returns total envelopes dispatched.

        Exposed for tests and for callers that want to drive the loop
        manually without a background thread.
        """
        total = 0
        for source in self._sources:
            try:
                envelopes = source.pull()
            except Exception as e:
                logger.error(
                    "IngestService: source %r raised during pull: %s",
                    getattr(source, "name", source), e,
                )
                logger.debug("IngestService source.pull exception details", exc_info=True)
                continue

            if not envelopes:
                continue

            for envelope in envelopes:
                self._dispatch(envelope)
                total += 1

        return total

    def _dispatch(self, envelope: IngestEnvelope) -> None:
        """Call every subscriber with this envelope. A raising subscriber
        does not prevent others from firing."""
        with self._subscribers_lock:
            subscribers = list(self._subscribers)

        for fn in subscribers:
            try:
                fn(envelope)
            except Exception as e:
                logger.error(
                    "IngestService: subscriber %r raised on envelope %s: %s",
                    getattr(fn, "__name__", fn),
                    envelope.signal_id,
                    e,
                )
                logger.debug("IngestService subscriber exception details", exc_info=True)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                logger.warning("IngestService already running")
                return
            self._stop_event.clear()
            self._thread = start_monitored_thread(
                owner="ingest",
                name="ingest-service",
                target=self._run_loop,
                daemon=True,
                kind="service_loop",
                metadata={
                    "component": "ingest",
                    "interval_seconds": self._poll_interval_seconds,
                    "sources": [getattr(s, "name", "?") for s in self._sources],
                },
            )
        logger.info(
            "IngestService started pid=%s interval=%ss sources=%s",
            os.getpid(),
            self._poll_interval_seconds,
            [getattr(s, "name", "?") for s in self._sources],
        )

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread:
            thread.join(timeout=2.0)
        logger.info(
            "IngestService stopped pid=%s joined=%s",
            os.getpid(),
            bool(thread and not thread.is_alive()),
        )

    def _run_loop(self) -> None:
        if self._startup_delay_seconds:
            # The loop polls at the top, so without this the first poll fires
            # immediately on start; hold it until the post-boot storm settles.
            self._stop_event.wait(self._startup_delay_seconds)
        while not self._stop_event.is_set():
            tick_started = time.monotonic()
            try:
                dispatched = self.tick_once()
                elapsed_ms = int((time.monotonic() - tick_started) * 1000)
                if dispatched:
                    logger.info(
                        "IngestService tick dispatched=%d elapsed_ms=%d",
                        dispatched, elapsed_ms,
                    )
                else:
                    logger.debug(
                        "IngestService tick dispatched=0 elapsed_ms=%d", elapsed_ms
                    )
            except Exception as e:
                logger.error("IngestService loop iteration failed: %s", e)
                logger.debug("IngestService loop iteration details", exc_info=True)
            self._stop_event.wait(self._poll_interval_seconds)
