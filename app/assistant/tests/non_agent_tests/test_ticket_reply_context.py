"""Ticket-reply context guards (2026-07-27 timesheet double-reminder).

The evaluator re-minted an abandoned work object 11 minutes after the user
settled it, because its context was incomplete: the template rendered only
accepted/declined/snoozed replies (the user's acknowledge-with-correction
never reached it), and the RECENTLY DROPPED section showed a bare title with
no reason. These tests pin the fixes:

- the recently-dropped log (dayflow_orchestrator/recent_work.py): dropped entries carry the full objective +
  when (local) + the user's last word;
- the editorializing rubrics ("supplementary", "can nudge again") are gone
  from every consumer.
The evaluator (the steward) read that log and its template was pinned here; it was retired on 2026-10-10.
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_ticket_reply_context")

from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.dayflow_orchestrator.recent_work import ended_item, render_recent_ended
from app.assistant.utils.path_utils import get_repo_root


def _abandoned_line(wo) -> str:
    """One work object's entry in the recently-dropped log, as its readers see it."""
    return render_recent_ended([], [ended_item(wo, finalizers=True)])[1]


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


# ---------------------------------------------------------------------------
# RECENTLY DROPPED annotation
# ---------------------------------------------------------------------------


def _fake_wo():
    from work_objects.model import WorkObject, WorkNode
    wo = WorkObject(id="work_bdc2b9abfd47", goal_node_id="node_goal", status="abandoned")
    wo.add_node(WorkNode(id="node_goal", work_id=wo.id, type="goal", status="abandoned",
        content="Complete and submit July monthly Seyfarth timesheets on 2026-08-01.",
        payload={"terminal": {"status": "abandoned", "reason": "settled by the user",
                              "at": "2026-07-27T19:31:45.824283+00:00"}}))
    wo.add_node(WorkNode(id="ask1", work_id=wo.id, type="subtask", parent_id="node_goal", status="closed",
        content="Finish July monthly timesheets", payload={"finalizer": {
            "verdict": "achieved", "outcome": "Those are done first of next month always."}}))
    wo.add_node(WorkNode(id="reply1", work_id=wo.id, type="evidence", parent_id="ask1", status="assumed",
        content="RAW_WORKER_HISTORY_NOT_FOR_STEWARD"))
    return wo


def test_abandoned_line_carries_objective_when_and_reason():
    from app.assistant.utils.time_utils import local_time_text
    line = _abandoned_line(_fake_wo())
    # The goal node's content, whole — not the stored (cut) title.
    assert line.startswith("- Complete and submit July monthly Seyfarth timesheets on 2026-08-01.")
    assert f"(dropped {local_time_text('2026-07-27T19:31:45.824283+00:00')})" in line
    assert "Those are done first of next month always." in line
    assert "settled by the user" in line


def test_abandoned_line_refuses_an_ending_without_a_time():
    import pytest
    wo = _fake_wo()
    del wo.nodes["node_goal"].payload["terminal"]["at"]
    with pytest.raises(ValueError, match="no end time"):
        _abandoned_line(wo)


# ---------------------------------------------------------------------------
# The editorializing rubrics are gone everywhere
# ---------------------------------------------------------------------------


def test_no_consumer_discounts_the_users_words():
    root = get_repo_root()
    consumers = [
        "app/assistant/agents/daily_context_tracker/prompts/user.j2",
        "app/assistant/agents/dayflow_cron_tickets/prompts/user.j2",
        "app/assistant/agents/health_status_inference/prompts/user.j2",
        "app/assistant/pipelines/dayflow/utils/situation_snapshot.py",
    ]
    for rel in consumers:
        text = (root / rel).read_text(encoding="utf-8")
        assert "supplementary" not in text.lower(), rel
        assert "can nudge again" not in text.lower(), rel
