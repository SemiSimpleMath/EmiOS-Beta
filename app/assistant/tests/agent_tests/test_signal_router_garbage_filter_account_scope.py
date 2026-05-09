import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.signal_router.contracts import SignalEnvelope, WatchRegistration, WatchRegistrationRequest
from app.assistant.signal_router.services.garbage_filter_service import GarbageFilterService


def _build_email_watch(*, account_id_equals: str) -> WatchRegistration:
    request = WatchRegistrationRequest(
        watch_key="watch_email_jouko_emi",
        event_name="signal_router.watch.email_jouko_reply",
        watch_type="email_semantic_match",
        predicate={
            "semantic_query": "reply email from jouko",
            "account_id_equals": account_id_equals,
        },
    )
    return WatchRegistration.from_request(request)


def _build_email_signal(*, account_id: str) -> SignalEnvelope:
    signal = SignalEnvelope(
        signal_id="sig_1",
        source_type="external",
        source_id="event_repository:email",
        occurred_at_utc="2026-01-01T00:00:00+00:00",
        signal_type="email",
        content="jouko replied with details",
        data={"account_id": account_id},
        metadata={},
    )
    signal.validate()
    return signal


def test_email_semantic_prefilter_keeps_matching_account_scope():
    service = GarbageFilterService()
    watch = _build_email_watch(account_id_equals="google_emi")
    signal = _build_email_signal(account_id="google_emi")

    decision = service.decide_for_watch(signal=signal, watch=watch)

    assert decision.action == "keep"
    assert decision.reason_code == "email_semantic_prefilter_pass"


def test_email_semantic_prefilter_drops_account_scope_mismatch():
    service = GarbageFilterService()
    watch = _build_email_watch(account_id_equals="google_emi")
    signal = _build_email_signal(account_id="google_user_primary")

    decision = service.decide_for_watch(signal=signal, watch=watch)

    assert decision.action == "drop"
    assert decision.reason_code == "email_watch_account_mismatch"
