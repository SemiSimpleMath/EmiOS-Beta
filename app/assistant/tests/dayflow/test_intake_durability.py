"""Intake must land before cursors advance, with unique references and retryable acknowledgement."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.assistant.utils.pydantic_classes import Message
from app.assistant.dayflow_orchestrator import ingestion, orchestrator_status
from app.assistant.ServiceLocator.service_locator import DI


def test_status_field_update_preserves_other_writers(monkeypatch):
    state = {"chat_ingested_up_to_utc": "new-cursor", "blocked_until_utc": "new-block"}
    def update(rid, value, **kwargs):
        state.clear()
        state.update(value)
    monkeypatch.setattr(DI, "resource_manager", SimpleNamespace(get_resource=lambda **kwargs: dict(state), update_resource=update))
    orchestrator_status.persist_orchestrator_status({"last_run_at_utc": "completed-run"})
    assert state["chat_ingested_up_to_utc"] == "new-cursor"
    assert state["blocked_until_utc"] == "new-block"


@pytest.fixture
def sources(monkeypatch):
    now = datetime.now(timezone.utc)
    message = Message(id="new-chat", content="Please investigate", metadata={"item_id": "new-chat", "source_type": "chat", "state": "artifact"})
    monkeypatch.setattr(ingestion, "load_ingestion_identity_index", lambda **kwargs: {})
    monkeypatch.setattr(ingestion, "_load_chat_entitled_rooms", lambda: ["master_room"])
    monkeypatch.setattr(ingestion, "load_orchestrator_status", lambda: {})
    monkeypatch.setattr(ingestion, "ingest_cross_room_chat", lambda **kwargs: ([message], now))
    monkeypatch.setattr(ingestion, "_ingest_emails", lambda *args: [])
    monkeypatch.setattr(ingestion, "_load_dayflow_requests", lambda **kwargs: [])
    monkeypatch.setattr(ingestion, "_load_dayflow_pod_kinds_filter", lambda: [])
    persisted = Mock()
    monkeypatch.setattr(ingestion, "persist_orchestrator_status", persisted)
    return now, persisted


def test_destination_failure_does_not_advance_source_cursor(sources, monkeypatch):
    from app.assistant.dayflow_orchestrator import dayflow_item_writer
    now, persisted = sources
    def fail_write(*args, **kwargs):
        raise OSError("synthetic intake write failure")
    monkeypatch.setattr(dayflow_item_writer, "write_dayflow_items_batch", fail_write)
    with pytest.raises(OSError):
        ingestion.run_dayflow_ingestion(now_utc=now)
    persisted.assert_not_called()


def test_short_ids_do_not_wrap_onto_existing_ids(sources, monkeypatch):
    from app.assistant.dayflow_orchestrator import dayflow_item_writer
    now, _ = sources
    monkeypatch.setattr(ingestion, "load_ingestion_identity_index", lambda **kwargs: {"old": {"metadata": {"short_id": 10000}}})
    write = Mock()
    monkeypatch.setattr(dayflow_item_writer, "write_dayflow_items_batch", write)
    ingestion.run_dayflow_ingestion(now_utc=now)
    assert write.call_args.args[0][0]["short_id"] == "10001"


def test_existing_destination_still_acknowledges_delegation(sources, monkeypatch):
    now, _ = sources
    request = {"id": "request"}
    monkeypatch.setattr(ingestion, "ingest_cross_room_chat", lambda **kwargs: ([], None))
    monkeypatch.setattr(ingestion, "load_ingestion_identity_index", lambda **kwargs: {"delegation": {"metadata": {"short_id": 1}}})
    monkeypatch.setattr(ingestion, "_load_dayflow_requests", lambda **kwargs: [request])
    monkeypatch.setattr(ingestion, "_build_delegation_message", lambda **kwargs: Message(id="delegation"))
    acknowledge = Mock()
    monkeypatch.setattr(ingestion, "mark_dayflow_requests_ingested", acknowledge)
    ingestion.run_dayflow_ingestion(now_utc=now)
    acknowledge.assert_called_once_with([request])


def test_chat_scan_has_no_truncation_and_includes_cursor_boundary(monkeypatch):
    from contextlib import contextmanager
    from app.assistant.dayflow_orchestrator import chat_ingestion as chat
    statements = []
    class Session:
        def execute(self, stmt):
            statements.append(stmt)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    @contextmanager
    def read_session():
        yield Session()
    monkeypatch.setattr(chat, "get_db_manager", lambda: SimpleNamespace(read_session=read_session))
    chat._load_chat_since(room_ids=["master_room"], since_utc=datetime.now(timezone.utc))
    sql = str(statements[0])
    assert "LIMIT" not in sql
    assert "timestamp >=" in sql


def test_filtered_chat_still_advances_observed_cursor(monkeypatch):
    from app.assistant.dayflow_orchestrator import chat_ingestion as chat
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(id="noise", timestamp=now, content_type="ticket", metadata_json={})
    monkeypatch.setattr(chat, "_load_chat_since", lambda **kwargs: [row])
    assert chat.ingest_cross_room_chat(since_utc=now, entitled_room_ids=["master_room"]) == ([], now)


def test_conversion_failure_prevents_skipping_chat(monkeypatch):
    from app.assistant.dayflow_orchestrator import chat_ingestion as chat
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(id="bad", timestamp=now, content_type="text", metadata_json={})
    monkeypatch.setattr(chat, "_load_chat_since", lambda **kwargs: [row])
    monkeypatch.setattr(chat, "_row_to_dayflow_message", Mock(side_effect=ValueError("invalid source")))
    with pytest.raises(ValueError, match="invalid source"):
        chat.ingest_cross_room_chat(since_utc=now, entitled_room_ids=["master_room"])


def test_pod_scan_does_not_cut_off_older_unseen_rows(sources, monkeypatch):
    from app.assistant.pod_store.pod_store import PodStore
    now, _ = sources
    query = Mock(return_value=[])
    monkeypatch.setattr(PodStore, "query", query)
    monkeypatch.setattr(ingestion, "_load_dayflow_pod_kinds_filter", lambda: [{"kind": "camera"}])
    ingestion._ingest_pods(set(), now)
    assert query.call_args.kwargs["limit"] is None


def test_ingestion_identity_index_keeps_old_closed_ids():
    from datetime import timedelta
    from app.assistant.dayflow_orchestrator.state_store import load_ingestion_identity_index
    from app.assistant.tests.dayflow.conftest import make_dayflow_message, seed_items
    old = datetime.now(timezone.utc) - timedelta(days=30)
    seed_items([make_dayflow_message(item_id="old:source", short_id="17000", state="closed", created_at=old, last_reviewed_at=old)])
    index = load_ingestion_identity_index()
    assert index["old:source"]["metadata"]["short_id"] == "17000"
