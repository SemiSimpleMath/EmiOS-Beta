"""One live ask per work object — the 2026-08-17 walk-storm guards, under the
2026-08-18 owner ruling: an ask is a TOOL CALL (dispatched + user_reply, no re-ask
timer) that ends by reply, dismissal, or ticket timeout.

Guards tested here:
  1. store fence      — an in-flight ask refuses terminal writes (replan cannot
                        prune a question that is out); reply (-> done) and
                        timeout (-> failed) stay open, closure cascades it.
  2. dispatch gate    — the gate claims a node before any tool runs, so an in-flight
                        ask is structurally un-pickable.
  3. ticket supersede — a new ask ticket expires prior open asks of the same
                        work object (id join on trigger_context.work_node).
  4. context          — node.content (the planner's full instruction) is the
                        primary message source, not wake_ref.
  5. timeout          — owned by the TOOL now: create_dayflow_ticket waits out its own
                        window and RETURNS a timeout result, so nothing re-derives it from
                        the ticket store afterwards (see test_ask_result_landing.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.assistant.dayflow_orchestrator.node_dispatch as nd


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Ask WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    return wo.id, wo.goal_node_id


def _add_node(store, wid, gid, node_id, *, content="Ask the user the full question.",
              title="Ask the user something"):
    store.apply("add_node", {"work_id": wid, "id": node_id, "type": "subtask", "parent_id": gid,
                             "title": title, "content": content})


def _inflight_ask(store, wid, gid, *, node_id, content="Ask the user the full question.",
                  wake_ref=None):
    _add_node(store, wid, gid, node_id, content=content)
    store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "dispatched"})
    store.apply("defer_node", {"work_id": wid, "node_id": node_id, "wake_kind": "user_reply",
                               "wake_at": None, "wake_ref": wake_ref})


# ── 1. store fence ────────────────────────────────────────────────


class TestInflightAskFence:

    def test_terminal_refused_on_inflight_ask(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _inflight_ask(store, wid, gid, node_id="ask1")
        with pytest.raises(ValueError, match="in-flight ask"):
            store.apply("set_status", {"work_id": wid, "node_id": "ask1", "status": "abandoned",
                                       "verdict": "pruned_by_replan", "reason": "replan says so"},
                        actor="architect")
        assert store.load(wid).nodes["ask1"].status == "dispatched"

    def test_reply_path_done_not_fenced(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _inflight_ask(store, wid, gid, node_id="ask1")
        store.apply("set_status", {"work_id": wid, "node_id": "ask1", "status": "done"})
        assert store.load(wid).nodes["ask1"].status == "done"

    def test_timeout_path_failed_not_fenced(self):
        """The timeout reason rides an append-only payload note — the node's
        content (its immutable directive) must survive the lifecycle write."""
        store = _store()
        wid, gid = _mk_wo(store)
        _inflight_ask(store, wid, gid, node_id="ask1")
        store.apply("set_status", {"work_id": wid, "node_id": "ask1", "status": "failed",
                                   "note": "ask timed out (ticket expired unanswered)"},
                    actor="dispatch_sweeper")
        node = store.load(wid).nodes["ask1"]
        assert node.status == "failed"
        assert node.content == "Ask the user the full question."          # directive untouched
        notes = node.payload.get("status_notes") or []
        assert any("timed out" in n["note"] for n in notes)

    def test_presurface_ask_can_be_pruned(self):
        """A never-surfaced ask (proposed + user_reply) has no question out — replan may prune it."""
        store = _store()
        wid, gid = _mk_wo(store)
        _add_node(store, wid, gid, "ask1")
        store.apply("defer_node", {"work_id": wid, "node_id": "ask1", "wake_kind": "user_reply",
                                   "wake_at": None, "wake_ref": None})
        store.apply("set_status", {"work_id": wid, "node_id": "ask1", "status": "abandoned",
                                   "reason": "replan: question moot before it was ever asked"},
                    actor="architect")
        assert store.load(wid).nodes["ask1"].status == "abandoned"

    def test_work_object_closure_cascade_clears_inflight_ask(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _inflight_ask(store, wid, gid, node_id="ask1")
        store.apply("set_work_status", {"work_id": wid, "status": "abandoned",
                                        "reason": "objective moot"}, actor="steward")
        node = store.load(wid).nodes["ask1"]
        assert node.status == "abandoned"
        assert node.wake_kind is None and node.wake_at is None


# ── 2. dispatch gate: one live ask per work object ────────────────


class TestAskIsAnOrdinaryDispatch:
    """The dispatch gate claims a node before any tool runs, so a `dispatched` ask is already
    invisible to the ready set and cannot be picked twice. The old belt-and-braces rule — a
    second ask node PARKED itself back to `waiting` when a sibling ask was in flight — is
    retired (owner ruling 2026-09-16): a node the gate picked is never un-picked, and two
    nodes asking the same thing is an architect prompt problem, not a dispatch-time guard.
    What still holds the UI to one live question is the tool's own supersede."""

    def test_dispatched_ask_is_not_in_the_ready_set(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _inflight_ask(store, wid, gid, node_id="ask1")
        wo = store.load(wid)
        assert wo.nodes["ask1"].status == "dispatched"
        assert not wo.is_ready(wo.nodes["ask1"]), "an in-flight ask must never be offered again"


# ── 3. ticket supersede ───────────────────────────────────────────


class _FakeOpenTicket:
    def __init__(self, ticket_id, work_node, ticket_type="dayflow_orchestrator"):
        self.ticket_id = ticket_id
        self.ticket_type = ticket_type
        self.trigger_context = {"work_node": work_node}


class _FakeTM:
    def __init__(self, open_tickets):
        self._open = open_tickets
        self.expired = []

    def get_tickets_pending_or_proposed(self):
        return self._open

    def mark_expired(self, ticket_id, reason=""):
        self.expired.append((ticket_id, reason))
        return True


class TestSupersedeOpenAsks:
    """Expiring the previous question is ticket-DATABASE work, so it lives in the tool that owns
    that database — not in the dispatcher. One live question per work object in the UI."""

    def _tool(self):
        from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
            CreateDayflowTicketTool,
        )
        return CreateDayflowTicketTool

    def test_same_work_object_open_ask_is_expired(self):
        tm = _FakeTM([_FakeOpenTicket("t_old", "work_abc::ask1")])
        self._tool()._supersede_open_asks(tm, {"work_node": "work_abc::ask2"})
        assert tm.expired == [("t_old", "superseded by work_abc::ask2")]

    def test_other_work_objects_and_types_untouched(self):
        tm = _FakeTM([
            _FakeOpenTicket("t_other_wo", "work_zzz::ask1"),
            _FakeOpenTicket("t_other_type", "work_abc::ask1", ticket_type="ask_user"),
            _FakeOpenTicket("t_no_ctx", ""),
        ])
        self._tool()._supersede_open_asks(tm, {"work_node": "work_abc::ask2"})
        assert tm.expired == []


# ── 4. message source: content over wake_ref ──────────────────────


class TestTicketContext:

    def test_brief_carries_node_content_not_wake_ref(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        rich = ("The student should bring her planner and a pencil to music class, "
                "and begin bringing her instrument on August 21, 2026.")
        _inflight_ask(store, wid, gid, node_id="ask1", content=rich,
                      wake_ref="Notify about music class supplies")
        node = store.load(wid).nodes["ask1"]

        calls = []
        from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
            CreateDayflowTicketTool,
        )
        monkeypatch.setattr(CreateDayflowTicketTool, "execute",
                            lambda self, tm: calls.append(tm) or None)

        nd.surface_and_await(store, wid, "ask1", node)

        assert len(calls) == 1
        args = calls[0].tool_data["arguments"]
        assert "instrument on August 21" in args["ticket_brief"]   # content made it through


# ── 4b. dispatch hands the composer manager ids, not pre-chewed content ──


class TestTicketManagerWiring:

    def test_dispatch_passes_the_work_node_ref(self, monkeypatch):
        """Content gathering belongs to ticket_builder_manager's planner: dispatch
        hands over the node ref (ids, not content) so the planner reads the graph
        itself. This replaced the topology-staple that pre-folded upstream
        evidence into the brief (retired 2026-08-19 by owner design ruling)."""
        store = _store()
        wid, gid = _mk_wo(store)
        _add_node(store, wid, gid, "deliver", content="Give the user the assessment.")
        store.apply("set_status", {"work_id": wid, "node_id": "deliver", "status": "actionable"})

        calls = []
        from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
            CreateDayflowTicketTool,
        )
        monkeypatch.setattr(CreateDayflowTicketTool, "execute",
                            lambda self, tm: calls.append(tm) or None)

        nd.surface_and_await(store, wid, "deliver", store.load(wid).nodes["deliver"])

        assert len(calls) == 1
        args = calls[0].tool_data["arguments"]
        assert args["trigger_context"]["work_node"] == f"{wid}::deliver"   # ids in
        assert "Give the user the assessment" in args["ticket_brief"]      # goal text rides the brief
        # The ticket's validity window IS the call's timeout — one number, not two clocks.
        assert args["wait_timeout_seconds"] == args["valid_hours"] * 3600


# ── 4c. repair: ask ceiling + directive preservation ─────────────


class TestRepairAskCeiling:

    def _failed_ask(self, store, wid, gid, *, node_id, timeouts):
        _add_node(store, wid, gid, node_id, content="Deliver the brief.")
        store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "dispatched"})
        store.apply("defer_node", {"work_id": wid, "node_id": node_id, "wake_kind": "user_reply",
                                   "wake_at": None, "wake_ref": None})
        for i in range(timeouts):
            store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "failed",
                                       "note": "ask timed out (ticket expired unanswered)"},
                        actor="dispatch_sweeper")
            if i < timeouts - 1:   # re-open between timeouts, as repair would
                store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "proposed"})
                store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "dispatched"})
                store.apply("defer_node", {"work_id": wid, "node_id": node_id,
                                           "wake_kind": "user_reply", "wake_at": None, "wake_ref": None})

    def test_third_timeout_hits_ceiling(self):
        from app.assistant.dayflow_orchestrator.work_repair_apply import apply_adjudication
        store = _store()
        wid, gid = _mk_wo(store)
        self._failed_ask(store, wid, gid, node_id="ask1", timeouts=3)

        apply_adjudication(store, wid, "escalate", ask="Please answer the brief question.")
        node = store.load(wid).nodes["ask1"]
        assert node.status == "abandoned"
        assert node.payload["terminal"]["verdict"] == "ask_unanswered_ceiling"

    def test_under_ceiling_reasks_without_touching_content(self):
        from app.assistant.dayflow_orchestrator.work_repair_apply import apply_adjudication
        store = _store()
        wid, gid = _mk_wo(store)
        self._failed_ask(store, wid, gid, node_id="ask1", timeouts=1)

        apply_adjudication(store, wid, "escalate", ask="Please answer the brief question.")
        node = store.load(wid).nodes["ask1"]
        assert node.status == "proposed"
        assert node.wake_kind == "user_reply"
        assert node.wake_ref == "Please answer the brief question."   # ask rides wake_ref
        assert node.content == "Deliver the brief."                   # directive untouched
