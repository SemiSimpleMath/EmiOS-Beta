"""Ticket-reply context guards (2026-07-27 timesheet double-reminder).

The evaluator re-minted an abandoned work object 11 minutes after the user
settled it, because its context was incomplete: the template rendered only
accepted/declined/snoozed replies (the user's acknowledge-with-correction
never reached it), and the RECENTLY DROPPED section showed a bare title with
no reason. These tests pin the fixes:

- flatten_responded_tickets: every category present, newest first;
- _abandoned_line: dropped entries carry when + the user's last word + the
  goal's final note;
- the evaluator template renders the flat list (acknowledged included) and
  the annotated drop reason;
- the editorializing rubrics ("supplementary", "can nudge again") are gone
  from every consumer.
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_ticket_reply_context")

from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.control_nodes.strategic_planner_wo_prep_node import _abandoned_line
from app.assistant.pipelines.dayflow.utils.context_sources import flatten_responded_tickets
from app.assistant.utils.path_utils import get_repo_root


# ---------------------------------------------------------------------------
# Incident fixture — the two replies from 2026-07-27
# ---------------------------------------------------------------------------

ACCEPT_1101 = {
    "title": "July Seyfarth Timesheets",
    "message": "it's a good time to complete and submit the July monthly timesheets",
    "responded_at_local": "11:01 AM",
    "responded_at_iso": "2026-07-27T18:01:24+00:00",
    "user_comment": "good chance i will get them in by noon",
    "user_action": "accept",
    "snooze_until_local": "",
}
ACK_1219 = {
    "title": "July monthly timesheets",
    "message": "just a reminder to complete and submit the July monthly timesheets",
    "responded_at_local": "12:19 PM",
    "responded_at_iso": "2026-07-27T19:19:49+00:00",
    "user_comment": "Those are done first of next month always.",
    "user_action": "acknowledge",
    "snooze_until_local": "",
}
CATEGORIZED = {
    "accepted": [ACCEPT_1101],
    "acknowledged": [ACK_1219],
    "declined": [],
    "snoozed": [],
}


def test_flatten_includes_acknowledged_newest_first():
    flat = flatten_responded_tickets(CATEGORIZED)
    assert len(flat) == 2
    # The 12:19 correction outranks the 11:01 acceptance.
    assert flat[0]["user_comment"] == "Those are done first of next month always."
    assert flat[0]["action_label"] == "ACKNOWLEDGED"
    assert flat[1]["action_label"] == "ACCEPTED"


def test_flatten_handles_empty_and_malformed():
    assert flatten_responded_tickets({}) == []
    assert flatten_responded_tickets({"accepted": [None, "junk"]}) == []


# ---------------------------------------------------------------------------
# RECENTLY DROPPED annotation
# ---------------------------------------------------------------------------


def _fake_wo():
    goal = SimpleNamespace(
        id="node_goal", type="goal", created_by="steward",
        content="Complete and submit July monthly Seyfarth timesheets on 2026-08-01.",
    )
    ask = SimpleNamespace(id="ask1", type="subtask", created_by="architect",
                          content="Finish July monthly timesheets")
    reply = SimpleNamespace(
        id="reply1", type="evidence", created_by="reply", parent_id="ask1",
        content="User has acknowledged this advice. Additional from user: Those are done first of next month always.",
    )
    return SimpleNamespace(
        id="work_bdc2b9abfd47",
        goal_node_id="node_goal",
        nodes={"node_goal": goal, "ask1": ask, "reply1": reply},
    )


SUMMARY = {
    "id": "work_bdc2b9abfd47",
    "title": "Complete the monthly timesheets by 2026-07-31.",
    "status": "abandoned",
    "updated_at": "2026-07-27T19:31:45.824283+00:00",
}


def test_abandoned_line_carries_reason_and_final_note():
    line = _abandoned_line(SUMMARY, _fake_wo())
    assert "Complete the monthly timesheets by 2026-07-31." in line
    assert "dropped" in line
    assert "Those are done first of next month always." in line
    assert "on 2026-08-01" in line  # the goal node's final content differs from the title


def test_abandoned_line_without_loaded_wo_is_bare_but_valid():
    line = _abandoned_line(SUMMARY, None)
    assert line.startswith("- Complete the monthly timesheets by 2026-07-31.")
    assert "dropped" in line


# ---------------------------------------------------------------------------
# Evaluator template — the incident context must render the settling facts
# ---------------------------------------------------------------------------


def _render_evaluator_template(**ctx):
    from jinja2 import Environment
    src = (
        get_repo_root() / "app" / "assistant" / "agents" / "dayflow_orchestrator"
        / "strategic_planner_wo" / "prompts" / "user.j2"
    ).read_text(encoding="utf-8")
    defaults = {
        "date_time": "2026-07-27 12:41 PDT",
        "day_of_week": "Monday",
        "recent_completed_work": "(none)",
        "work_portfolio": "",
    }
    defaults.update(ctx)
    return Environment().from_string(src).render(**defaults)


def test_evaluator_prompt_shows_acknowledged_correction_first():
    out = _render_evaluator_template(
        recent_ticket_replies=flatten_responded_tickets(CATEGORIZED),
    )
    assert "Those are done first of next month always." in out
    assert "ACKNOWLEDGED" in out
    # Newest first: the correction renders before the stale acceptance.
    assert out.index("Those are done first of next month") < out.index("by noon")
    assert "supersedes" in out


def test_evaluator_prompt_shows_drop_reason():
    out = _render_evaluator_template(
        recent_abandoned_work=_abandoned_line(SUMMARY, _fake_wo()),
    )
    assert "RECENTLY DROPPED" in out
    assert "Those are done first of next month always." in out
    assert "recorded reason" in out


# ---------------------------------------------------------------------------
# The editorializing rubrics are gone everywhere
# ---------------------------------------------------------------------------


def test_no_consumer_discounts_the_users_words():
    root = get_repo_root()
    consumers = [
        "app/assistant/agents/daily_context_tracker/prompts/user.j2",
        "app/assistant/agents/dayflow_cron_tickets/prompts/user.j2",
        "app/assistant/agents/health_status_inference/prompts/user.j2",
        "app/assistant/agents/dayflow_orchestrator/strategic_planner_wo/prompts/user.j2",
        "app/assistant/pipelines/dayflow/utils/situation_snapshot.py",
    ]
    for rel in consumers:
        text = (root / rel).read_text(encoding="utf-8")
        assert "supplementary" not in text.lower(), rel
        assert "can nudge again" not in text.lower(), rel
