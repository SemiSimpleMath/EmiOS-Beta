"""Tests for IngestService.

Uses fake sources to exercise the fan-out logic in isolation. The real
sources (UnifiedLogSource, EmailRepoSource) have their own dedicated
test files.
"""
import app.assistant.tests.test_setup  # noqa: F401

from typing import List

import pytest

from app.assistant.ingest.contracts import IngestEnvelope
from app.assistant.ingest.ingest_service import IngestService


def _env(signal_id: str, content: str = "x") -> IngestEnvelope:
    return IngestEnvelope(
        signal_id=signal_id,
        source_type="global_blackboard",
        source_id="test",
        occurred_at_utc="2026-04-18T00:00:00+00:00",
        signal_type="test",
        content=content,
        data={},
        metadata={},
    )


class _FakeSource:
    def __init__(self, name: str, batches: List[List[IngestEnvelope]]) -> None:
        self.name = name
        self._batches = list(batches)

    def pull(self) -> List[IngestEnvelope]:
        if not self._batches:
            return []
        return self._batches.pop(0)


class _RaisingSource:
    name = "raising_source"

    def pull(self) -> List[IngestEnvelope]:
        raise RuntimeError("simulated source failure")


def test_register_subscriber_requires_callable():
    svc = IngestService([_FakeSource("s1", [])])
    with pytest.raises(ValueError):
        svc.register_subscriber("not_a_function")  # type: ignore[arg-type]


def test_construction_requires_at_least_one_source():
    with pytest.raises(ValueError):
        IngestService([])


def test_tick_dispatches_envelopes_to_single_subscriber():
    src = _FakeSource("s", [[_env("a"), _env("b")]])
    received: List[IngestEnvelope] = []

    svc = IngestService([src])
    svc.register_subscriber(lambda e: received.append(e))

    dispatched = svc.tick_once()

    assert dispatched == 2
    assert [e.signal_id for e in received] == ["a", "b"]


def test_tick_dispatches_to_multiple_subscribers_in_registration_order():
    src = _FakeSource("s", [[_env("a")]])
    received_x: List[str] = []
    received_y: List[str] = []

    svc = IngestService([src])
    svc.register_subscriber(lambda e: received_x.append(e.signal_id))
    svc.register_subscriber(lambda e: received_y.append(e.signal_id))

    svc.tick_once()

    assert received_x == ["a"]
    assert received_y == ["a"]


def test_subscriber_raising_does_not_block_others():
    src = _FakeSource("s", [[_env("a")]])
    received: List[str] = []

    def boom(_):
        raise RuntimeError("subscriber failed")

    svc = IngestService([src])
    svc.register_subscriber(boom)
    svc.register_subscriber(lambda e: received.append(e.signal_id))

    dispatched = svc.tick_once()

    assert dispatched == 1
    assert received == ["a"]


def test_source_raising_does_not_stop_other_sources():
    s1 = _RaisingSource()
    s2 = _FakeSource("s2", [[_env("a")]])
    received: List[str] = []

    svc = IngestService([s1, s2])
    svc.register_subscriber(lambda e: received.append(e.signal_id))

    dispatched = svc.tick_once()

    assert dispatched == 1
    assert received == ["a"]


def test_tick_aggregates_envelopes_across_sources():
    s1 = _FakeSource("s1", [[_env("a"), _env("b")]])
    s2 = _FakeSource("s2", [[_env("c")]])
    received: List[str] = []

    svc = IngestService([s1, s2])
    svc.register_subscriber(lambda e: received.append(e.signal_id))

    dispatched = svc.tick_once()

    assert dispatched == 3
    assert set(received) == {"a", "b", "c"}


def test_tick_with_no_new_envelopes_returns_zero():
    src = _FakeSource("s", [])
    called = []

    svc = IngestService([src])
    svc.register_subscriber(lambda e: called.append(e))

    assert svc.tick_once() == 0
    assert called == []


def test_subsequent_ticks_pick_up_next_batch():
    src = _FakeSource("s", [[_env("a")], [_env("b"), _env("c")]])
    received: List[str] = []

    svc = IngestService([src])
    svc.register_subscriber(lambda e: received.append(e.signal_id))

    assert svc.tick_once() == 1
    assert svc.tick_once() == 2
    assert received == ["a", "b", "c"]


def test_start_stop_lifecycle_runs_loop_without_crashing():
    src = _FakeSource("s", [])
    svc = IngestService([src], poll_interval_seconds=5)

    svc.start()
    try:
        # Loop should be alive; no easy way to introspect without sleeping,
        # so just verify thread state flips.
        with svc._lock:
            assert svc._thread is not None
            assert svc._thread.is_alive()
    finally:
        svc.stop()

    with svc._lock:
        assert svc._thread is not None
        assert not svc._thread.is_alive()


def test_start_is_idempotent_warns_if_already_running():
    src = _FakeSource("s", [])
    svc = IngestService([src], poll_interval_seconds=5)

    svc.start()
    try:
        first_thread = svc._thread
        svc.start()  # should warn, not spawn a second thread
        assert svc._thread is first_thread
    finally:
        svc.stop()
