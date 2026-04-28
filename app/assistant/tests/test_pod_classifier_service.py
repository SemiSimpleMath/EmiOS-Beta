"""Tests for PodClassifierService.

Uses the test_setup harness so we can construct real agents and the real
PodStore. The pod_classifier agent is not stubbed because the test asserts
end-to-end: an envelope goes in, a pod comes out of the store with correct
tags/one_liner/source_refs.
"""
import app.assistant.tests.test_setup  # noqa: F401

import uuid
from datetime import datetime, timezone

import pytest

from app.assistant.ingest.contracts import IngestEnvelope
from app.assistant.pod_store import PodStore
from app.assistant.pod_store.pod_classifier_service import PodClassifierService
from app.models.base import get_session
from sqlalchemy import text


_TEST_ROOM_ID = "test_room/pod_classifier_service"


def _envelope(speaker: str, content: str, *, source_id: str = "") -> IngestEnvelope:
    signal_id = source_id or f"test-{uuid.uuid4()}"
    return IngestEnvelope(
        signal_id=signal_id,
        source_type="unified_log",
        source_id=speaker,
        occurred_at_utc=datetime.now(timezone.utc).isoformat(),
        signal_type="chat",
        content=content,
        data={},
        metadata={"room_id": _TEST_ROOM_ID, "speaker_name": speaker},
    )


def _clear_test_pods() -> None:
    session = get_session()
    try:
        session.execute(text("DELETE FROM pod_store WHERE scope_id = :rid"), {"rid": _TEST_ROOM_ID})
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_test_pods()
    yield
    _clear_test_pods()


def test_service_ignores_non_chat_envelopes():
    svc = PodClassifierService(quiet_threshold_seconds=1, tick_interval_seconds=60)
    email_env = IngestEnvelope(
        signal_id="email-xyz",
        source_type="external",  # email repo source uses source_type="external"
        source_id="event_repository:email",
        occurred_at_utc=datetime.now(timezone.utc).isoformat(),
        signal_type="email",
        content="Hello from Whole Foods",
        data={},
        metadata={"subject": "sale"},
    )
    svc.handle_envelope(email_env)
    assert svc._buffers == {} or all(len(b) == 0 for b in svc._buffers.values())


def test_service_buffers_chat_envelope_by_room():
    svc = PodClassifierService(quiet_threshold_seconds=1, tick_interval_seconds=60)
    svc.handle_envelope(_envelope("Jukka", "I had açai for breakfast"))
    svc.handle_envelope(_envelope("Emi", "nice"))
    with svc._buffers_lock:
        assert _TEST_ROOM_ID in svc._buffers
        assert len(svc._buffers[_TEST_ROOM_ID]) == 2


def test_service_skips_envelopes_without_room_id():
    svc = PodClassifierService(quiet_threshold_seconds=1, tick_interval_seconds=60)
    bad_env = IngestEnvelope(
        signal_id="no-room",
        source_type="unified_log",
        source_id="speaker",
        occurred_at_utc=datetime.now(timezone.utc).isoformat(),
        signal_type="chat",
        content="orphan",
        data={},
        metadata={},  # no room_id
    )
    svc.handle_envelope(bad_env)
    with svc._buffers_lock:
        assert all(len(b) == 0 for b in svc._buffers.values())


def test_tick_once_flushes_idle_rooms_and_mints_pod():
    """End-to-end: envelopes in → tick_once fires flush → real
    pod_classifier agent runs → pod appears in pod_store."""
    pod_store = PodStore()
    svc = PodClassifierService(
        pod_store=pod_store,
        quiet_threshold_seconds=0,  # flush immediately on next tick
        tick_interval_seconds=60,
    )

    # A clearly food-tagged burst.
    envs = [
        _envelope("Jukka", "going to try that new Thai place tonight", source_id="test-food-1"),
        _envelope("Emi", "the one off Culver?", source_id="test-food-2"),
        _envelope("Jukka", "yeah. hungry for pad see ew.", source_id="test-food-3"),
    ]
    for e in envs:
        svc.handle_envelope(e)

    # Threshold is 0 → every buffered room flushes on next sweep.
    flushed = svc.tick_once()
    assert flushed == 1, f"expected 1 room flushed, got {flushed}"

    pods = pod_store.query(tags=["food"], limit=10)
    test_pods = [p for p in pods if p.scope_id == _TEST_ROOM_ID]
    assert len(test_pods) == 1, f"expected one food pod, got {[p.pod_id for p in test_pods]}"
    p = test_pods[0]
    assert "food" in p.tags
    assert p.scope_id == _TEST_ROOM_ID
    assert p.kind == "chat_cluster"
    assert p.one_liner, "one_liner should be populated"
    assert len(p.source_refs) == 3
    assert {sr.id for sr in p.source_refs} == {"test-food-1", "test-food-2", "test-food-3"}
    assert all(sr.kind == "unified_log" for sr in p.source_refs)


def test_tick_once_discards_chitchat_burst():
    """Classifier returns empty tags for pure banter; no pod minted."""
    pod_store = PodStore()
    svc = PodClassifierService(
        pod_store=pod_store,
        quiet_threshold_seconds=0,
        tick_interval_seconds=60,
    )

    envs = [
        _envelope("Justin", "hey", source_id="test-chit-1"),
        _envelope("Jukka", "lol", source_id="test-chit-2"),
        _envelope("Jukka", "haha fair", source_id="test-chit-3"),
    ]
    for e in envs:
        svc.handle_envelope(e)

    flushed = svc.tick_once()
    # flush still counts — we did process the buffer, just minted nothing.
    assert flushed == 1

    pods = pod_store.query(limit=10)
    test_pods = [p for p in pods if p.scope_id == _TEST_ROOM_ID]
    assert test_pods == [], f"expected no pods for chitchat, got {[p.pod_id for p in test_pods]}"


def test_deterministic_pod_id_for_same_envelope_set():
    """Same signal_ids → same pod_id. Idempotent mint."""
    svc = PodClassifierService()
    envs = [
        _envelope("a", "one", source_id="x"),
        _envelope("b", "two", source_id="y"),
        _envelope("c", "three", source_id="z"),
    ]
    id_a = svc._make_cluster_pod_id(envs)
    id_b = svc._make_cluster_pod_id(list(reversed(envs)))  # order shouldn't matter
    assert id_a == id_b
    assert id_a.startswith("datapod:chat_cluster:")


def test_buffer_force_flushes_at_max_burst_size():
    """Safety cap: if a single room accumulates MAX_BURST_SIZE envelopes
    without quieting, the buffer force-flushes inside handle_envelope."""
    from app.assistant.pod_store.pod_classifier_service import _MAX_BURST_SIZE

    pod_store = PodStore()
    svc = PodClassifierService(
        pod_store=pod_store,
        quiet_threshold_seconds=99999,  # never trigger quiet flush
        tick_interval_seconds=99999,
    )
    for i in range(_MAX_BURST_SIZE):
        svc.handle_envelope(
            _envelope("user", f"msg {i}", source_id=f"test-force-{i}")
        )
    # Buffer should be empty post-force-flush.
    with svc._buffers_lock:
        assert len(svc._buffers.get(_TEST_ROOM_ID, [])) == 0
