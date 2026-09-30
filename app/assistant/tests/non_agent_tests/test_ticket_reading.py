"""The reading page of a ticket: what it gathers from the ticket's links (ticket_manager/reading.py)."""
from types import SimpleNamespace

import pytest

from app.assistant.subconscious import kg_links, work_links
from app.assistant.ticket_manager import reading


def _ticket(ctx):
    return SimpleNamespace(ticket_id="t1", ticket_type="dayflow_orchestrator", title="School events",
                           message="A long message", state=SimpleNamespace(value="pending"),
                           created_at=None, responded_at=None, user_action=None, user_text=None,
                           trigger_context=ctx)


def _concern(cid, evidence=(), known=()):
    return {"concern_id": cid, "status": "addressing", "title": f"concern {cid}", "subject": None,
            "done_when": "done", "owner_words": None, "brief": {"what": "w", "known": list(known)},
            "earlier": [], "evidence": [e[0] for e in evidence], "evidence_kinds": dict(evidence)}


@pytest.fixture
def links(monkeypatch):
    seen = {"sources": [], "linked": []}
    monkeypatch.setattr(kg_links, "find_entities", lambda texts: [{"label": "Woodbridge"}])

    def linked(refs, threads):
        seen["linked"].append(refs)
        return [{"work_id": "work_now"}, {"work_id": "work_old"}]
    monkeypatch.setattr(work_links, "linked_work", linked)

    def source(ref, kind=None):
        seen["sources"].append((ref, kind))
        if ref == "datapod:email:gone":
            return None
        return {"kind": "email" if "email" in ref else "chat", "ref": ref}
    monkeypatch.setattr(reading, "_source", source)
    return seen


def test_dayflow_ticket_gathers_work_concerns_and_their_sources(monkeypatch, links):
    monkeypatch.setattr(reading, "_ticket", lambda tid: _ticket(
        '{"work_node": "work_now::n1", "response_choices": [{"label": "OK"}]}'))
    monkeypatch.setattr(reading, "_work", lambda wn: {
        "work_id": "work_now", "concern_refs": ["concern:aaaaaaaa", "concern:bbbbbbbb"],
        "sources": [{"pod_id": "datapod:email:e1"}, {"note": "no pod"}]})
    concerns = {"concern:aaaaaaaa": _concern("aaaaaaaa-1", evidence=[("m1", "chat_msg")],
                                             known=[{"fact": "f", "source": "datapod:email:e1; message:m2"}]),
                "concern:bbbbbbbb": _concern("bbbbbbbb-1", known=[{"fact": "f", "source": "datapod:email:gone"}])}
    monkeypatch.setattr(reading, "_concern", concerns.get)

    out = reading.ticket_reading("t1")

    assert [c["concern_id"] for c in out["concerns"]] == ["aaaaaaaa-1", "bbbbbbbb-1"]
    assert all("evidence" not in c and "evidence_kinds" not in c for c in out["concerns"])
    # each cited source is fetched once, evidence keeps its kind; a missing one is left out
    assert links["sources"] == [("datapod:email:e1", None), ("message:m2", None), ("m1", "chat_msg"),
                                ("datapod:email:gone", None)]
    assert [s["ref"] for s in out["sources"]] == ["datapod:email:e1", "message:m2", "m1"]
    # past work excludes the ticket's own work and appears once across concerns
    assert out["related_work"] == [{"work_id": "work_old"}]
    assert out["ticket"]["choices"] == ["OK"] and out["ticket"]["state"] == "pending"
    assert out["entities"] == [{"label": "Woodbridge"}]


def test_question_ticket_reaches_its_concern(monkeypatch, links):
    monkeypatch.setattr(reading, "_ticket", lambda tid: _ticket({"question_id": "q1"}))
    monkeypatch.setattr(reading, "_question", lambda qid: {"question_id": qid, "related_concern_id": "cccccccc-1"})
    monkeypatch.setattr(reading, "_concern", lambda ref: _concern(ref) if ref == "cccccccc-1" else None)

    out = reading.ticket_reading("t1")

    assert out["work"] is None
    assert [c["concern_id"] for c in out["concerns"]] == ["cccccccc-1"]


def test_ticket_without_links_still_reads(monkeypatch, links):
    monkeypatch.setattr(reading, "_ticket", lambda tid: _ticket(None))
    out = reading.ticket_reading("t1")
    assert (out["work"], out["question"], out["concerns"], out["sources"], out["related_work"]) == \
        (None, None, [], [], [])
    assert out["ticket"]["message"] == "A long message"
