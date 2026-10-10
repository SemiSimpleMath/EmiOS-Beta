"""The brain's situation (subconscious/situation.py; docs/design/brain_delegates_work_2026-10-04.md, section 2).

Pins: a conversation still going is left out until the room goes quiet (the gate's rule), a finished one is read
whole (what wakes a decider is not the situation, day_tasks.fingerprint); work made from the calendar is left out when
asked (shadow), work from elsewhere stays; the template shows every time local and says plainly what is not known.
Invented data; no database.
"""
from datetime import datetime, timedelta, timezone

from app.assistant.subconscious import situation as S
from app.assistant.tests.non_agent_tests.test_day_tasks import NOW, situation

MIN = timedelta(minutes=1)


def _msg(i, at, speaker="Owner"):
    return {"id": f"m{i}", "at": at.isoformat(), "speaker": speaker, "text": f"message {i}"}


def test_a_conversation_still_going_waits_and_a_finished_one_is_read_whole():
    earlier = [_msg(1, NOW - 60 * MIN), _msg(2, NOW - 59 * MIN, "Assistant")]
    going = [_msg(3, NOW - 3 * MIN), _msg(4, NOW - 2 * MIN, "Assistant"), _msg(5, NOW - 1 * MIN)]
    assert S.finished_conversations(earlier + going, NOW) == earlier
    assert S.finished_conversations(earlier + going, NOW + 6 * MIN) == earlier + going
    assert S.finished_conversations(going, NOW) == []
    assert S.finished_conversations([], NOW) == []


def test_the_brain_sees_all_work_in_motion_and_what_ended_in_the_window(monkeypatch):
    """Until 2026-10-10 work made from the calendar was left out on a day the steward still held (retired with it)."""
    from app.assistant.subconscious import work_links
    belief = {"source_intake": [{"source_type": "belief"}]}
    monkeypatch.setattr(work_links, "active_work", lambda: [{"work_id": "w1"}, {"work_id": "w2"}])
    recent, old = (NOW - 60 * MIN).isoformat(), (NOW - 20 * 60 * MIN).isoformat()
    monkeypatch.setattr(work_links, "_rows", lambda: [
        {"id": "w3", "title": "AC", "status": "done", "updated_at": recent, "constraints": belief},
        {"id": "w4", "title": "Ring", "status": "abandoned", "updated_at": recent, "constraints": {}},
        {"id": "w7", "title": "Heads-up", "status": "abandoned", "updated_at": recent,
         "constraints": {"belief_refs": ["B455"], "source_intake": []}},
        {"id": "w5", "title": "Old", "status": "done", "updated_at": old, "constraints": {}},
        {"id": "w6", "title": "Running", "status": "active", "updated_at": recent, "constraints": {}}])
    monkeypatch.setattr(work_links, "work_view", lambda work_id, link: {"work_id": work_id, "link": link})
    work = S.work(NOW)
    assert [w["work_id"] for w in work["in_motion"]] == ["w1", "w2"]
    assert sorted(w["work_id"] for w in work["ended"]) == ["w3", "w4", "w7"], "ended in the window, every source"
    assert all(w["link"] == "ended" for w in work["ended"])


def test_the_template_shows_times_local_and_says_what_is_not_known():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    from app.assistant.utils.time_utils import local_time_text
    t = _ENV.get_template("shared/brain/situation.j2")
    text = t.render(s=situation())
    assert f"Now: {local_time_text(NOW.isoformat())} (Tuesday)" in text
    assert "The owner is away from the keyboard, since 07:18 AM (42 minutes)." in text
    assert "Weather: Springfield, 61 F, Clear" in text and "- 2026-03-12 (2d away): Sam's birthday" in text
    assert NOW.isoformat() not in text, "every time in the prompt is local"
    quiet = situation(presence={"is_afk": False, "status_label": "UNKNOWN", "duration_minutes": 0.0,
                                "start_time_local": "", "most_recent_afk": None},
                      weather={"city": None, "temperature": None, "condition": None}, health="", tickets=[],
                      reminders={"status": "unavailable", "since": NOW.isoformat(), "through": NOW.isoformat(),
                                 "records": []})
    unknown = t.render(s={**quiet, "chat": []})
    assert "Whether the owner is at the keyboard is not known right now." in unknown
    assert "Weather: (not known)" in unknown and "Health today: (no assessment yet)" in unknown
    assert "History unavailable. Do not interpret this as no prior reminders." in unknown
    assert "(no finished conversation yet today)" in unknown
    ended = t.render(s=situation(work={"in_motion": [], "ended": [{
        "work_id": "work_r", "title": "Check the doorbell battery", "status": "abandoned", "objective": "",
        "created": "Sun 2026-10-04", "updated": "Sun 2026-10-04", "link": "ended", "score": None,
        "ended_because": "The battery status could not be read.",
        "outcomes": [{"title": "Check battery", "outcome": "The integration returned no camera details.", "at": None}]}]}))
    assert "- ended because: The battery status could not be read." in ended
    assert '- task "Check battery": The integration returned no camera details.' in ended
    assert "FINALIZER" not in ended and "NEXT" not in ended, "outside dayflow the finalizer's prose only"
