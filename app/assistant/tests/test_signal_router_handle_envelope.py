"""Tests for SignalRouterService.handle_envelope (Step A1).

handle_envelope is the public entrypoint the gut (IngestService) uses to
dispatch each IngestEnvelope. Internally it mirrors the same match
pipeline the existing poll loop uses, so the tests here focus on:

- The method's external contract (no watches, no crash; validates input)
- The pipeline runs end-to-end (garbage filter → watcher → dedupe → emit)
- Idempotency: calling twice with the same envelope doesn't double-emit

The watcher agent is replaced with a stub so we don't actually hit an LLM.
"""
import app.assistant.tests.test_setup  # noqa: F401

import uuid

import pytest

from app.assistant.signal_router.contracts import (
    SignalEnvelope,
    WatcherAgentOutput,
    WatchRegistrationRequest,
)
from app.assistant.signal_router.signal_router_service import SignalRouterService


_TEST_WATCH_KEY_PREFIXES = ("test_handle_envelope_",)


def _cancel_test_watches() -> None:
    """Mark any test-owned watches cancelled so they don't accumulate in
    the real signal_router_watch table. Runs before AND after each test."""
    from app.models.base import get_session
    session = get_session()
    try:
        from sqlalchemy import text
        for prefix in _TEST_WATCH_KEY_PREFIXES:
            session.execute(
                text(
                    "UPDATE signal_router_watch SET status='cancelled' "
                    "WHERE status='active' AND watch_key LIKE :pfx"
                ),
                {"pfx": f"{prefix}%"},
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clean_test_watches():
    _cancel_test_watches()
    yield
    _cancel_test_watches()


def _new_service(emit_to_event_hub: bool = False) -> SignalRouterService:
    # Every test constructs its own service so registered watches don't
    # leak between tests. The service writes to the real signal_router_*
    # SQLite tables; _clean_test_watches fixture cancels anything created
    # during the test so it can't affect production or subsequent runs.
    return SignalRouterService(emit_to_event_hub=emit_to_event_hub)


class _StubWatcher:
    """Replaces WatcherAgentService.evaluate with deterministic output."""

    def __init__(self, should_emit: bool = True, confidence: float = 0.95):
        self._should_emit = should_emit
        self._confidence = confidence
        self.calls = 0

    def evaluate(self, *, watch_registration, signal, filter_decision):
        self.calls += 1
        out = WatcherAgentOutput(
            should_emit_event=self._should_emit,
            match_reason="stub_match_reason" if self._should_emit else "stub_no_match",
            confidence=self._confidence,
            dedupe_key_hint=f"stub::{signal.get('signal_id', '')}",
            evidence={"stub": True},
            proposed_payload={},
        )
        return out


def _keyword_envelope(signal_id: str, content: str) -> SignalEnvelope:
    env = SignalEnvelope(
        signal_id=signal_id,
        source_type="unified_log",
        source_id="test_sender",
        occurred_at_utc="2026-04-18T00:00:00+00:00",
        signal_type="chat",
        content=content,
        data={},
        metadata={"room_id": "master_room"},
    )
    env.validate()
    return env


def test_handle_envelope_with_no_watches_is_noop():
    svc = _new_service()
    env = _keyword_envelope("sig_1", "hello world")

    # Must not raise even though no watches are registered.
    svc.handle_envelope(env)


def test_handle_envelope_validates_input():
    svc = _new_service()
    # A SignalEnvelope with an empty signal_id fails validate()
    bad_env = SignalEnvelope(
        signal_id="",
        source_type="unified_log",
        source_id="x",
        occurred_at_utc="2026-04-18T00:00:00+00:00",
        signal_type="chat",
        content="hi",
    )
    with pytest.raises(ValueError, match="signal_id"):
        svc.handle_envelope(bad_env)


def test_handle_envelope_fires_pipeline_and_records_dedupe_on_match():
    svc = _new_service()
    svc._watcher_service = _StubWatcher(should_emit=True)

    reg = svc.register_keyword_watch(
        watch_key="test_handle_envelope_match",
        event_name="signal_router.watch.test_keyword",
        keywords=["latte"],
    )

    sig_id = f"sig_match_{uuid.uuid4().hex}"
    env = _keyword_envelope(sig_id, "I want a latte")
    svc.handle_envelope(env)

    # Dedupe key is "{registration_id}:{signal_id}"; persistence is proof
    # the match emitted.
    assert svc._state_store.has_dedupe_key(f"{reg.registration_id}:{sig_id}")
    assert svc._watcher_service.calls == 1


def test_handle_envelope_idempotent_dedupe_suppresses_repeats():
    svc = _new_service()
    stub = _StubWatcher(should_emit=True)
    svc._watcher_service = stub

    reg = svc.register_keyword_watch(
        watch_key="test_handle_envelope_dedupe",
        event_name="signal_router.watch.test_dedupe",
        keywords=["bagel"],
    )

    sig_id = f"sig_dup_{uuid.uuid4().hex}"
    env = _keyword_envelope(sig_id, "bagel for breakfast")

    svc.handle_envelope(env)
    first_calls = stub.calls
    svc.handle_envelope(env)  # same envelope — pre-check should skip watcher entirely
    second_calls = stub.calls

    assert svc._state_store.has_dedupe_key(f"{reg.registration_id}:{sig_id}")
    # First call runs the watcher; second call hits the dedupe pre-check and
    # skips. This is the behavior we explicitly moved dedupe before the
    # watcher to enforce.
    assert first_calls == 1
    assert second_calls == 1


def test_handle_envelope_does_not_record_dedupe_when_watcher_declines():
    svc = _new_service()
    svc._watcher_service = _StubWatcher(should_emit=False)

    reg = svc.register_keyword_watch(
        watch_key="test_handle_envelope_no_match",
        event_name="signal_router.watch.test_no_match",
        keywords=["croissant"],
    )

    sig_id = f"sig_no_match_{uuid.uuid4().hex}"
    env = _keyword_envelope(sig_id, "croissant crumbs everywhere")
    svc.handle_envelope(env)

    assert not svc._state_store.has_dedupe_key(f"{reg.registration_id}:{sig_id}")


def test_handle_envelope_accepts_ingest_envelope_alias():
    """IngestEnvelope is aliased to SignalEnvelope — confirm the type check
    in validate() accepts what the gut hands over."""
    from app.assistant.ingest.contracts import IngestEnvelope

    svc = _new_service()
    env: IngestEnvelope = IngestEnvelope(
        signal_id="sig_alias_1",
        source_type="unified_log",
        source_id="alias_test",
        occurred_at_utc="2026-04-18T00:00:00+00:00",
        signal_type="chat",
        content="ingest passthrough works",
    )
    svc.handle_envelope(env)  # no watches → no-op, but type must accept
