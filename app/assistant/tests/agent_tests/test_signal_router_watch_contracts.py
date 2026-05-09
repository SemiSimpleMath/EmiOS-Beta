import app.assistant.tests.test_setup  # noqa: F401
import pytest

from app.assistant.signal_router.contracts import WatchRegistrationRequest
from app.assistant.signal_router.signal_router_service import SignalRouterService


def test_email_semantic_match_registration_contract_accepts_valid_payload():
    req = WatchRegistrationRequest(
        watch_key="watch_email_jukka_cabin",
        event_name="signal_router.watch.email_from_jukka_cabin",
        watch_type="email_semantic_match",
        predicate={
            "semantic_query": "email from jukka that talks about directions to the cabin",
            "sender_contains": "jukka",
        },
    )
    req.validate()


def test_email_semantic_match_registration_contract_rejects_missing_semantic_query():
    req = WatchRegistrationRequest(
        watch_key="watch_email_jukka_cabin",
        event_name="signal_router.watch.email_from_jukka_cabin",
        watch_type="email_semantic_match",
        predicate={"sender_contains": "jukka"},
    )
    with pytest.raises(ValueError, match="semantic_query"):
        req.validate()


def test_email_semantic_match_registration_contract_rejects_bad_event_prefix():
    req = WatchRegistrationRequest(
        watch_key="watch_email_jukka_cabin",
        event_name="signal_router.watch.generic_event",
        watch_type="email_semantic_match",
        predicate={"semantic_query": "email from jukka about cabin directions"},
    )
    with pytest.raises(ValueError, match="signal_router.watch.email_"):
        req.validate()


def test_signal_router_register_email_semantic_watch_persists_contract():
    service = SignalRouterService(emit_to_event_hub=False)
    registration = service.register_email_semantic_watch(
        watch_key="watch_email_jukka_cabin",
        event_name="signal_router.watch.email_from_jukka_cabin",
        semantic_query="email from jukka that talks about directions to the cabin",
        sender_contains="jukka",
    )
    assert registration.watch_type == "email_semantic_match"
    assert registration.event_name.startswith("signal_router.watch.email_")
    assert registration.predicate.get("semantic_query")


def test_email_semantic_match_registration_contract_accepts_account_scope():
    req = WatchRegistrationRequest(
        watch_key="watch_email_jouko_emi",
        event_name="signal_router.watch.email_jouko_reply",
        watch_type="email_semantic_match",
        predicate={
            "semantic_query": "reply email from jouko",
            "account_id_equals": "google_emi",
        },
    )
    req.validate()


def test_email_semantic_match_registration_contract_rejects_empty_account_scope():
    req = WatchRegistrationRequest(
        watch_key="watch_email_jouko_emi",
        event_name="signal_router.watch.email_jouko_reply",
        watch_type="email_semantic_match",
        predicate={
            "semantic_query": "reply email from jouko",
            "account_id_equals": "   ",
        },
    )
    with pytest.raises(ValueError, match="account_id_equals"):
        req.validate()
