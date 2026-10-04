"""A concern's journal reaches agents in local time (2026-10-04).

Stored entries are "[<UTC ISO>] text"; prompts show every time local. The concern planner read UTC stamps beside a
local Current Time and called a 9 AM notice "now-past" at 3:31 AM. Pins: each stamped entry is shown through the
shared local_time_text; a line without a stamp (the archive marker) or with a bracket that is not a time is shown
as stored; the brain's concern view, the concern door, the planner and the noticer all read it that way.
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
    from app.assistant.subconscious import brain_step, concern_door, concern_planner, context_builder
    concern = {"concern_id": "c1", "title": "Ring battery", "_status": "active", "evidence": [], "attached_work": {},
               "reinforcement_notes": NOTES}
    local = local_time_text(STAMP)
    assert local in brain_step._concern_view("C1", concern)["journal"]
    assert local in concern_planner.concern_text(concern, "active")
    assert local in context_builder._render_handling_history(concern)
    assert local in concern_door.build_payload([], [concern])["existing"][0]["journal"]
