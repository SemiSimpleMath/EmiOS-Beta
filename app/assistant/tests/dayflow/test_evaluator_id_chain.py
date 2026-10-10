"""A ticket's context row carries its ticket id and the work node it belongs to (2026-08-01).

Edges recorded at write time: ticket.trigger_context.work_node (node_dispatch). Until 2026-10-10 the steward's prep
node also chased schedule entries `ticket:<ticket_id>` to the work object's status (the 2026-07-29 trash re-mint);
it was retired with the steward.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.pipelines.dayflow.utils.context_sources import (
    _format_ticket_for_context,
)

def _ticket(ticket_id="t1", work_node="work_a860eab761d8::trash_bins_reminder--a860ea",
            user_action="willdo"):
    return SimpleNamespace(
        ticket_id=ticket_id,
        title="Trash bins",
        message="Time to take the bins to the curb.",
        suggestion_type="work_notify",
        responded_at=None,
        snooze_until=None,
        user_text="yes thank you",
        user_action=user_action,
        trigger_context={"work_node": work_node} if work_node else {},
        execution_result="",
    )


class TestTicketContextRow:

    def test_formatted_row_carries_ticket_id_and_work_node(self):
        row = _format_ticket_for_context(_ticket())
        assert row["ticket_id"] == "t1"
        assert row["work_node"] == "work_a860eab761d8::trash_bins_reminder--a860ea"
