"""A concern's summary and how every reader sees a concern (2026-10-09).

Owner, 2026-10-09: "we cant have something with 100 notes on it. we also don't want to show truncated record"; "It
does not have to run on every concern every time. only when the record gets long."

Pins: a concern whose notes are short is not summarized and is read whole; once the notes no summary covers pass
SUMMARY_AFTER_CHARS they are folded into a summary, once; every reader then gets the summary and the notes written
since, whole; a note written while the summary was being written stays after it; the writer sees the previous summary
and the notes since; a summary that fails is recorded and not tried again until a note is written; the record's
fingerprint ignores the summary and the plan; work citing a concern carries its record, and an unresolvable ref is
shown, not raised. Scratch register; the writer is a fake. Invented data only.
"""
from __future__ import annotations

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import concern_summary, concern_view, persist
from app.assistant.tests.concern_store_helpers import ScratchRegister

EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}


def _note(i: int) -> str:
    return f"[2026-10-0{1 + i // 10}T1{i % 10}:00:00+00:00] [brain] Observation {i}: " + "detail " * 30


def _concern(cid="c-flea", notes=0, **kw):
    return {"concern_id": cid, "title": "Flea medication due", "subject": "household", "done_when": "dose given",
            "notes": "Monthly dose for the dogs.", "first_observed": "2026-09-28T08:00:00+00:00", "evidence": [],
            "reinforcement_notes": "".join("\n" + _note(i) for i in range(notes)), **kw}


def _writer(*answers):
    seen = []

    def call(payload):
        seen.append(payload)
        return answers[min(len(seen), len(answers)) - 1]
    call.seen = seen
    return call


def test_short_notes_are_read_whole_and_never_summarized(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_concern(notes=3)]})
    writer = _writer({"summary": "s"})
    assert concern_summary.run_summaries(call=writer, register_connect=reg.connect) == \
        {"summaries_written": 0, "summaries_failed": 0}
    assert writer.seen == []
    history = persist.history_for_prompt(reg.read()["active"][0]).splitlines()
    assert len(history) == 4 and history[0].endswith("] Monthly dose for the dogs.")


def test_long_notes_are_folded_once_and_readers_get_the_summary_and_the_notes_since(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_concern(notes=20)]})
    writer = _writer({"summary": "The dose is monthly; given Oct 1."})
    assert concern_summary.run_summaries(call=writer, register_connect=reg.connect)["summaries_written"] == 1
    shown = writer.seen[0]
    assert shown["summary"] is None and len(shown["notes"]) == 21, "the raise note and every journal entry"
    assert shown["notes"][0].endswith("] Monthly dose for the dogs.")
    [c] = reg.read()["active"]
    assert c["summary"]["lines"] == 20 and c["summary"]["text"] == "The dose is monthly; given Oct 1."
    assert c["summary"]["through"] == "2026-10-02T19:00:00+00:00", "the last folded note's time"
    assert persist.history_for_prompt(c).startswith("Summary of the notes up to ")
    assert "Observation" not in persist.history_for_prompt(c), "the folded notes are not shown again"
    assert concern_summary.run_summaries(call=writer, register_connect=reg.connect)["summaries_written"] == 0

    register = reg.read()
    register["active"][0]["reinforcement_notes"] += "\n" + _note(25)
    reg.write(register)
    history = persist.history_for_prompt(reg.read()["active"][0]).splitlines()
    assert len(history) == 2 and "Observation 25" in history[1], "a new note is shown whole after the summary"


def test_the_next_summary_reads_the_previous_one_and_only_the_notes_since(tmp_path):
    c = _concern(notes=40, summary={"text": "Earlier: monthly.", "lines": 20, "through": "2026-10-02T19:00:00+00:00"})
    payload = concern_summary.build_payload(c, "active")
    assert payload["summary"] == "Earlier: monthly." and len(payload["notes"]) == 20
    assert "Observation 20" in payload["notes"][0], "the raise note is already folded"
    from app.assistant.dayflow_orchestrator import work_context
    user = work_context._ENV.get_template("subconscious/concern_summary/prompts/user.j2").render(agent_input=payload)
    assert "Covering the notes up to" in user and "Earlier: monthly." in user and "Observation 39" in user


def test_a_note_written_while_summarizing_stays_after_the_summary(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_concern(notes=21)]})
    persist.set_concern_summary("c-flea", 20, text="s", connect=reg.connect)
    [c] = reg.read()["active"]
    assert persist.unsummarized(c) == [persist._entry_for_prompt(_note(20).strip())]
    with pytest.raises(ValueError):
        persist.set_concern_summary("c-flea", 30, text="s", connect=reg.connect)


def test_a_failed_summary_is_recorded_and_waits_for_a_new_note(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_concern(notes=20)]})
    writer = _writer({"summary": ""}, {"summary": " "})
    assert concern_summary.run_summaries(call=writer, register_connect=reg.connect) == \
        {"summaries_written": 0, "summaries_failed": 1}
    assert writer.seen[1]["correction"] == "the answer had no summary"
    [c] = reg.read()["active"]
    assert "summary" not in c and c["summary_error"]["lines"] == 20
    concern_summary.run_summaries(call=writer, register_connect=reg.connect)
    assert len(writer.seen) == 2, "not tried again until a note is written"
    register = reg.read()
    register["active"][0]["reinforcement_notes"] += "\n" + _note(21)
    reg.write(register)
    assert len(concern_summary.due(reg.read())) == 1


def test_the_fingerprint_ignores_the_summary_and_the_plan():
    b = concern_view.basis(_concern(), "active")
    assert concern_view.basis({**_concern(), "summary": {"text": "s"}, "plan": {"decision": "hold"},
                               "summary_error": {}, "plan_error": {}}, "active") == b
    assert concern_view.basis(_concern(notes=1), "active") != b
    assert concern_view.basis(_concern(), "addressing") != b


def test_work_carries_the_record_of_the_concerns_it_serves(tmp_path, monkeypatch):
    reg = ScratchRegister(tmp_path).write({**EMPTY, "active": [_concern("3f0c2b9a-1111-2222-3333-444455556666",
                                                                         notes=1)]})
    monkeypatch.setattr("app.assistant.subconscious.concern_store._connect", reg.connect)
    [served, missing] = concern_view.concerns_for_refs(["concern:3f0c2b9a", "concern:deadbeef"])
    assert "Monthly dose for the dogs." in served["history"] and "Observation 0" in served["history"]
    assert missing == {"ref": "concern:deadbeef", "unresolved": "matches 0 concerns in the register"}
    from app.assistant.dayflow_orchestrator.work_context import render_view
    text = render_view("concern_view", concerns=[served, missing])
    assert "- concern 3f0c2b9a: Flea medication due [active]" in text
    assert "  its record, oldest first" in text and "    [" in text and "Monthly dose for the dogs." in text
    assert "- concern concern:deadbeef: not found (matches 0 concerns in the register)" in text
