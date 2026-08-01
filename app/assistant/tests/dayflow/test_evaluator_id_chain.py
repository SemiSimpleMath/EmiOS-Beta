"""The evaluator's id chain (2026-08-01): provenance is resolved deterministically
before the prompt, never reconstructed from wording by the model.

Edges recorded at write time: ticket.trigger_context.work_node (node_dispatch),
schedule-entry source `ticket:<ticket_id>` (daily_context_tracker, verbatim copy).
The prep node chases them — ticket -> work node -> work object -> status — and
renders the resolved facts on the evaluator's TICKET REPLIES and TODAY'S SCHEDULE
lines. Unresolvable ids render visibly unresolved (sink, not drop). The 2026-07-29
trash re-mint is the motivating incident: the tracker's echo of an accepted ticket
read as a bare unowned ongoing activity and was re-minted as new work.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.control_nodes.strategic_planner_wo_prep_node import (
    _annotate_work_refs,
    _resolve_ticket_provenance,
)
from app.assistant.pipelines.dayflow.utils.context_sources import (
    _format_ticket_for_context,
)

_STATUS = {"work_a860eab761d8": "done", "work_af114744b5eb": "active"}


class FakeTicketManager:
    def __init__(self, tickets):
        self._tickets = {t.ticket_id: t for t in tickets}

    def get_ticket_by_id(self, ticket_id):
        return self._tickets.get(ticket_id)


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


class TestWorkRefAnnotation:

    def test_reply_row_resolves_to_work_object_and_status(self):
        rows = [{"work_node": "work_a860eab761d8::trash_bins_reminder--a860ea"}]
        _annotate_work_refs(rows, _STATUS)
        assert rows[0]["work_ref"] == "work_a860eab761d8 — done"

    def test_unknown_work_id_renders_unresolved(self):
        rows = [{"work_node": "work_gone::node--x"}]
        _annotate_work_refs(rows, _STATUS)
        assert rows[0]["work_ref"] == "work_gone — unresolved"

    def test_row_without_edge_stays_unannotated(self):
        rows = [{"work_node": ""}, {"title": "no field at all"}]
        _annotate_work_refs(rows, _STATUS)
        assert "work_ref" not in rows[0] and "work_ref" not in rows[1]


class TestTicketProvenance:

    def test_full_chain_resolves(self):
        tm = FakeTicketManager([_ticket()])
        out = _resolve_ticket_provenance("t1", tm, _STATUS)
        assert out == "outcome of work_a860eab761d8 — done; user willdo"

    def test_missing_ticket_is_visibly_unresolved(self):
        tm = FakeTicketManager([])
        out = _resolve_ticket_provenance("t404", tm, _STATUS)
        assert "unresolved" in out and "t404" in out

    def test_ticket_without_work_edge_still_names_the_ticket(self):
        tm = FakeTicketManager([_ticket(ticket_id="t2", work_node=None)])
        out = _resolve_ticket_provenance("t2", tm, _STATUS)
        assert out == "from ticket t2; user willdo"

    def test_empty_id_is_visibly_unresolved(self):
        out = _resolve_ticket_provenance("", FakeTicketManager([]), _STATUS)
        assert "unresolved" in out


class TestTicketContextRow:

    def test_formatted_row_carries_ticket_id_and_work_node(self):
        row = _format_ticket_for_context(_ticket())
        assert row["ticket_id"] == "t1"
        assert row["work_node"] == "work_a860eab761d8::trash_bins_reminder--a860ea"
