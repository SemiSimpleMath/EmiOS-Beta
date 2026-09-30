"""The concern brief: written when a concern's record changes, stored against that record, read by
every dayflow agent working on work that serves the concern.

Pins: a concern gets a brief when it has none or its record changed; an unchanged concern is not
rewritten; a brief that fails is recorded and not retried until the record changes; every cited
source must have been shown to the writer; a brief written from an older record is not stored;
work citing a concern (full id or short form) carries its brief, and an unresolvable ref is shown,
not raised. Scratch register; the writer is a fake. Invented data only.
"""
from __future__ import annotations

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import concern_brief, persist, work_links
from app.assistant.tests.concern_store_helpers import ScratchRegister

BRIEF = {"what": "The dogs' monthly flea dose.", "why_it_matters": "Fleas if missed.",
         "known": [{"fact": "Given on Sep 28.", "source": "message:m1"}], "tried": "Nothing yet.",
         "owner_wishes": "Nothing said.", "depends_on_it": ["Calendar: Oct 2 dose"], "open_questions": [],
         "recommendation": "No action until the next dose."}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(work_links, "linked_work", lambda concern_ids, threads: [])
    monkeypatch.setattr(work_links, "similar_work", lambda texts, exclude=(): [])
    monkeypatch.setattr(work_links, "active_work", lambda: [])
    monkeypatch.setattr(work_links, "live_reminders", lambda now=None: [])
    from app.assistant.subconscious import kg_links
    monkeypatch.setattr(kg_links, "find_entities", lambda texts: [])
    monkeypatch.setattr(kg_links, "shared", lambda ids: {"links": [], "entities": []})
    from app.assistant.subconscious import brain_step
    monkeypatch.setattr(brain_step, "_message_text", lambda mid: f"(message {mid})")


def _concern(cid="c-flea", **kw):
    return {"concern_id": cid, "title": "Flea medication due", "subject": "household", "done_when": "dose given",
            "notes": "n", "evidence": [{"kind": "chat_msg", "ref": "message:m1", "snippet": "given"}],
            "reinforcement_notes": "", **kw}


def _writer(*answers):
    seen = []

    def call(payload):
        seen.append(payload)
        return answers[min(len(seen), len(answers)) - 1]
    call.seen = seen
    return call


def test_a_brief_is_written_once_per_version_of_the_record(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [_concern()], "addressing": [], "resolved": [], "dormant": []})
    writer = _writer(BRIEF)
    assert concern_brief.run_briefs(call=writer, register_connect=reg.connect, calendar="(cal)")["briefs_written"] == 1
    [c] = reg.read()["active"]
    assert c["brief"]["recommendation"] == "No action until the next dose."
    assert c["brief"]["basis"] == concern_brief.basis(c, "active")
    assert concern_brief.run_briefs(call=writer, register_connect=reg.connect, calendar="(cal)")["briefs_written"] == 0
    assert len(writer.seen) == 1, "an unchanged concern is not rewritten"
    shown = writer.seen[0]
    assert shown["concern"]["evidence"][0]["snippet"] == "(message m1)" and shown["calendar"] == "(cal)"


def test_a_changed_record_gets_a_new_brief(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [_concern()], "addressing": [], "resolved": [], "dormant": []})
    concern_brief.run_briefs(call=_writer(BRIEF), register_connect=reg.connect, calendar="")
    register = reg.read()
    register["active"][0]["reinforcement_notes"] = "\n[t] [brain] The dose moved to Oct 3."
    reg.write(register)
    assert [c["concern_id"] for _, c in concern_brief.stale(reg.read())] == ["c-flea"]


def test_an_invented_source_gets_one_correction_then_the_failure_is_recorded_once(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [_concern()], "addressing": [], "resolved": [], "dormant": []})
    bad = {**BRIEF, "known": [{"fact": "The vet confirmed.", "source": "message:never-shown"}]}
    writer = _writer(bad, bad)
    out = concern_brief.run_briefs(call=writer, register_connect=reg.connect, calendar="")
    assert out == {"briefs_written": 0, "briefs_failed": 1}
    assert "message:never-shown" in writer.seen[1]["correction"]
    [c] = reg.read()["active"]
    assert "brief" not in c and "still invalid" in c["brief_error"]["error"]
    concern_brief.run_briefs(call=writer, register_connect=reg.connect, calendar="")
    assert len(writer.seen) == 2, "not retried until the record changes"


def test_a_brief_written_from_an_older_record_is_not_stored(tmp_path):
    reg = ScratchRegister(tmp_path).write({"active": [_concern()], "addressing": [], "resolved": [], "dormant": []})
    old = concern_brief.basis(_concern(), "active")
    register = reg.read()
    register["active"][0]["notes"] = "changed meanwhile"
    reg.write(register)
    assert persist.set_concern_brief("c-flea", old, brief=BRIEF, connect=reg.connect) is False
    assert "brief" not in reg.read()["active"][0]


def test_work_carries_the_briefs_of_the_concerns_it_serves(tmp_path, monkeypatch):
    reg = ScratchRegister(tmp_path).write({"active": [_concern("3f0c2b9a-1111-2222-3333-444455556666")],
                                           "addressing": [], "resolved": [], "dormant": []})
    monkeypatch.setattr("app.assistant.subconscious.concern_store._connect", reg.connect)
    concern_brief.run_briefs(call=_writer(BRIEF), register_connect=reg.connect, calendar="")
    [served, missing] = concern_brief.briefs_for_refs(["concern:3f0c2b9a", "concern:deadbeef"])
    assert served["brief"]["what"] == "The dogs' monthly flea dose." and served["brief_current"]
    assert missing == {"ref": "concern:deadbeef", "unresolved": "matches 0 concerns in the register"}
    from app.assistant.dayflow_orchestrator.work_context import render_view
    text = render_view("concern_brief", concerns=[served, missing])
    assert "- concern 3f0c2b9a: Flea medication due [active]" in text
    assert "    - Given on Sep 28. (message:m1)" in text and "recommendation: No action until the next dose." in text
    assert "- concern concern:deadbeef: not found (matches 0 concerns in the register)" in text


def test_the_brief_prompt_renders_the_concern_and_what_is_in_motion():
    from app.assistant.dayflow_orchestrator import work_context
    from app.assistant.subconscious import brain_step
    payload = {"now": "Tue", "concern": brain_step._concern_view("C1", {**_concern(), "_status": "active"}),
               "linked_work": [], "similar_work": [], "active_work": [],
               "reminders": [{"ref": "reminder:r9", "title": "Flea dose", "kind": "recurring", "repeats": "every month",
                              "start": "Thu", "end": ""}],
               "calendar": "- [calendar:flea] Flea medication @ Oct 2",
               "entities": [{"label": "Bonnie", "description": "A dog."}], "shared": {"links": [], "entities": []},
               "correction": None}
    user = work_context._ENV.get_template("subconscious/brief/prompts/user.j2").render(agent_input=payload)
    assert "### [C1] Flea medication due (active)" in user and "- done when: dose given" in user
    assert "- [reminder:r9] Flea dose (recurring, every month; from Thu)" in user and "[calendar:flea]" in user
    assert "- Bonnie: A dog." in user


def test_a_source_may_name_what_was_shown_in_the_form_it_was_shown():
    payload = {"concern": {"evidence": [{"kind": "pod", "ref": "datapod:chat_cluster:1f50d3a4eaa414a6"},
                                        {"kind": "chat_msg", "ref": "2c45a0e7-6af9-41d9-8a26"}]},
               "linked_work": [{"work_id": "work_17b43a46f546"}], "similar_work": [], "active_work": [],
               "calendar": "- [calendar:evt_1_20261002T000000Z] Flea dose @ Oct 2",
               "reminders": [{"ref": "reminder:9adcde00", "title": "A friend's birthday"}]}
    refs = concern_brief.allowed_refs(payload)
    good = [{"fact": f, "source": s} for f, s in [
        ("a", "pod datapod:chat_cluster:1f50d3a4eaa414a6"), ("b", "chat_msg 2c45a0e7-6af9-41d9-8a26"),
        ("c", "work_17b43a46f546; calendar:evt_1_20261002T000000Z"), ("d", "reminder:9adcde00"),
        ("e", "knowledge graph"), ("f", "journal")]]
    assert concern_brief._problems({**BRIEF, "known": good}, refs) == []
    bad = [{"fact": "x", "source": "calendar:A friend's birthday"}, {"fact": "y", "source": "work_1; invented"}]
    assert len(concern_brief._problems({**BRIEF, "known": bad}, refs)) == 2


def test_a_new_writer_version_rewrites_every_brief(monkeypatch):
    before = concern_brief.basis(_concern(), "active")
    monkeypatch.setattr(concern_brief, "WRITER_VERSION", concern_brief.WRITER_VERSION + 1)
    assert concern_brief.basis(_concern(), "active") != before
