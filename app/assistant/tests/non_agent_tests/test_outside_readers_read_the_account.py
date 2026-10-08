"""Readers outside dayflow read what happened, and an unanswered question is weighed by the concern (2026-10-04).

The 2026-10-03 workshop case: one ticket expired unanswered; the finalizer's account said so plainly, but its
verdict and its recommendation to the architect ("do not send a reminder or recreate the prompt") reached the
brain through the past-work view, were copied into the concern's journal, its brief and its plan, and the
concern held until its date had passed.

Pins: the brain's past-work view and the concern records show each task's account in the finalizer's prose,
never its verdict, recommendation or next step; the work report names the task without a verdict; the concern
decider weighs an unanswered question against how much the concern matters and its date, and sees the
concern's severity.
"""
from app.assistant.dayflow_orchestrator.work_context import _ENV, render_view

RES = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"},
       "resource_assistant_capabilities": "", "resource_owner_standing_directives": ""}
FIN = {"verdict": "unrecoverable", "next_step": "stop", "at": "2026-03-10T10:30:00+00:00",
       "outcome": "The ticket expired unanswered; it does not establish that Sam saw it.",
       "recommendation": "Do not send a reminder or recreate the prompt."}


def test_the_brains_past_work_shows_what_each_task_found_and_nothing_addressed_to_the_architect():
    w = {"work_id": "w1", "title": "Ask about the fair", "status": "abandoned", "created": "Mon", "updated": "Tue",
         "objective": "", "ended_because": "", "link": "cites a concern of this matter", "score": None,
         "outcomes": [{"title": "Ask about the fair", "outcome": FIN["outcome"], "at": FIN["at"]}]}
    text = _ENV.from_string("{% include 'shared/brain/past_work.j2' %}").render(w=w)
    assert '- task "Ask about the fair" (' in text and FIN["outcome"] in text
    assert "unrecoverable" not in text and "Do not send" not in text and "NEXT" not in text


def test_the_concern_record_shows_the_account_without_the_verdict():
    view = {"concern_id": "c1234567", "title": "School fair", "status": "active", "severity": "low",
            "done_when": None, "owner_words": None, "brief": None,
            "attempts": [{"work_id": "w1", "objective": "Ask about the fair", "attached_at": FIN["at"],
                          "progress": {"work_id": "w1", "state": "abandoned", "ended_at": FIN["at"], "reason": "r",
                                       "replanning": False, "done": [], "dropped": [], "unfinished": [], "to_come": []},
                          "judgments": [{**FIN, "title": "Ask", "replies": []}]}]}
    text = render_view("concern_brief", concerns=[view])
    assert "- concern c1234567: School fair [active]\n  severity: low (how much it matters" in text
    assert '- task "Ask" (' in text and FIN["outcome"] in text
    assert "unrecoverable" not in text and "Do not send" not in text


def test_the_decider_weighs_an_unanswered_question_against_what_matters_and_the_date():
    text = _ENV.get_template("subconscious/concern_tasks/prompts/system.j2").render(**RES)
    assert "When an earlier question to Sam went unanswered, decide from how much the concern matters" in text
    assert "a minor one is let go" in text
