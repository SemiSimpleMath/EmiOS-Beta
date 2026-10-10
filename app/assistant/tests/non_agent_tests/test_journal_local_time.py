"""A concern's journal reaches agents in local time (2026-10-04).

Stored entries are "[<UTC ISO>] text"; prompts show every time local. The concern planner read UTC stamps beside a
local Current Time and called a 9 AM notice "now-past" at 3:31 AM. Pins: each stamped entry is shown through the
shared local_time_text; a line without a stamp (the archive marker) or with a bracket that is not a time is shown
as stored; the brain's concern view, the concern door and the planner all read it that way (the noticer reads
concerns by title only since 2026-10-09).
"""
from app.assistant.subconscious import persist
from app.assistant.utils.time_utils import local_time_text

STAMP = "2026-10-04T10:25:40.778630+00:00"
NOTES = f"\n(3 earlier notes archived)\n[{STAMP}] [brain] Ring sent a new low-battery notice.\n[not a time] kept as is"


def test_each_stamped_entry_is_shown_in_local_time_and_the_rest_as_stored():
    text = persist.journal_for_prompt(NOTES)
    assert text.splitlines() == ["(3 earlier notes archived)",
                                 f"[{local_time_text(STAMP)}] [brain] Ring sent a new low-battery notice.",
                                 "[not a time] kept as is"]
    assert STAMP not in text


def test_every_agent_view_of_a_concern_reads_the_local_journal():
    from app.assistant.subconscious import brain_step, concern_door, concern_planner
    concern = {"concern_id": "c1", "title": "Ring battery", "_status": "active", "evidence": [], "attached_work": {},
               "reinforcement_notes": NOTES}
    local = local_time_text(STAMP)
    assert local in brain_step._concern_view("C1", concern)["history"]
    assert local in concern_planner.concern_text(concern, "active")
    assert local in concern_door.build_payload([], [concern])["existing"][0]["history"]


def test_the_note_a_concern_was_raised_with_comes_first_with_when_it_was_written():
    """2026-10-08: "She has a doctor appointment at 10:30 today." was written 2026-10-07 and shown undated; on the 8th
    the daily-context tracker put the appointment on the 8th's calendar. Every reader now sees the notes oldest
    first, each with when it was written, whole."""
    from app.assistant.pipelines.dayflow.utils.subconscious_projection import _project_concern
    raised = "2026-10-07T16:09:19+00:00"
    note = "She has a doctor appointment at 10:30 today. " + "x" * 300
    concern = {"concern_id": "c1", "title": "Sick", "_status": "active", "evidence": [], "attached_work": {},
               "first_observed": raised, "notes": note, "reinforcement_notes": NOTES}
    lines = persist.history_for_prompt(concern).splitlines()
    assert lines[0] == f"[{local_time_text(raised)}] {note}"
    assert lines[1:] == persist.journal_for_prompt(NOTES).strip("\n").splitlines()
    assert _project_concern(concern)["history"] == persist.history_for_prompt(concern), "the projection passes it whole"
    assert "notes" not in _project_concern(concern)
