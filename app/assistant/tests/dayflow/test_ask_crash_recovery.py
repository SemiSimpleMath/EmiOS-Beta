"""An ask outlives the process that made it.

The thread waiting on the user dies at shutdown; the QUESTION does not — it is a row in the ticket
database and may already carry the answer. Without recovery the node sits `dispatched` with a dead
session, the orphan sweep fails it, and repair re-asks: an answer the user gave minutes before the
restart is discarded, and the same question goes back on screen.

So recovery is driven from the TICKET side — the durable record, which carries the node ref that
says which call it belongs to.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant.dayflow_orchestrator import work_session as ws


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _inflight_ask(store, title="Ask WO"):
    """A node mid-call: claimed by the gate, its session gone with the process."""
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "ask1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Give the user the plan",
                             "content": "Tell the user the plan."})
    store.apply("set_status", {"work_id": wo.id, "node_id": "ask1", "status": "dispatched"},
                actor="dispatch_gate")
    return wo.id


def _ticket(ref, *, state, user_text="", user_action="", valid_minutes=45, ticket_id="t1"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        ticket_id=ticket_id, state=state, title="the picture-day plan",
        user_text=user_text, user_action=user_action,
        trigger_context={"work_node": ref, "dispatch_epoch": 1}, created_at=now,
        valid_until=now + timedelta(minutes=valid_minutes),
    )


class _TM:
    def __init__(self, tickets):
        self._t = tickets

    def get_tickets(self, **kw):
        return self._t

    def get_ticket_by_id(self, ticket_id):
        return next((t for t in self._t if t.ticket_id == ticket_id), None)


def _patch(monkeypatch, tickets):
    import app.assistant.ticket_manager as tm_pkg
    monkeypatch.setattr(tm_pkg, "get_ticket_manager", lambda: _TM(tickets))
    from app.assistant.ServiceLocator.service_locator import DI

    judged = []

    class FinalizerAgent:
        def action_handler(self, message):
            judged.append(message)
            missed = "user not reached" in message.information or "TOOL STATUS: the tool reported" in message.information
            return SimpleNamespace(data={
                "verdict": "unrecoverable" if missed else "achieved",
                "next_step": "stop" if missed else "",
                "outcome": "The user was not reached." if missed else "The user received the plan and answered.",
                "recommendation": "Stop this branch." if missed else "",
            })

    def create_agent(name):
        assert name == "dayflow_orchestrator::work_finalizer"
        return FinalizerAgent()

    monkeypatch.setattr(DI, "agent_factory", SimpleNamespace(create_agent=create_agent))
    return judged


class TestReArmInflightAsks:

    def test_an_answer_given_while_we_were_down_is_landed(self, monkeypatch):
        """The call DID return — nobody was listening. The answer is the node's result, exactly as
        if the thread had survived. This is the case that used to be thrown away."""
        store = _store()
        wid = _inflight_ask(store)
        _patch(monkeypatch, [_ticket(
            f"{wid}::ask1", state="accepted", user_action="acknowledge",
            user_text="Acknowledge — the picture was already taken so this is all moot.")])

        assert ws.re_arm_inflight_asks() == 1

        wo = store.load(wid)
        assert wo.nodes["ask1"].status == "closed"
        assert wo.nodes["ask1"].payload["finalizer"]["verdict"] == "achieved"
        assert wo.status == "done"
        evidence = [n for n in wo.nodes.values() if n.parent_id == "ask1" and n.type == "evidence"]
        assert "this is all moot" in evidence[0].content

    def test_a_question_that_lapsed_while_we_were_down_lands_as_not_reached(self, monkeypatch):
        store = _store()
        wid = _inflight_ask(store)
        _patch(monkeypatch, [_ticket(f"{wid}::ask1", state="expired")])

        assert ws.re_arm_inflight_asks() == 1

        wo = store.load(wid)
        assert wo.nodes["ask1"].status == "failed"
        assert wo.nodes["ask1"].payload["finalizer"]["verdict"] == "unrecoverable"
        assert wo.status == "active"
        evidence = [n for n in wo.nodes.values() if n.parent_id == "ask1" and n.type == "evidence"]
        assert "user not reached" in evidence[0].content

    def test_a_still_live_question_is_waited_on_again_without_re_asking(self, monkeypatch):
        """The question is still on screen with time left. We wait out the REMAINDER on the
        existing ticket — creating a second one would ask the user twice for the same thing."""
        store = _store()
        wid = _inflight_ask(store)
        _patch(monkeypatch, [_ticket(f"{wid}::ask1", state="proposed", valid_minutes=30)])

        waits = []
        monkeypatch.setattr(ws, "_run_resume_ask_session",
                            lambda *a: waits.append(a))

        assert ws.re_arm_inflight_asks() == 1
        sid = ws.session_id_for(wid, "ask1")
        with ws._sessions_lock:
            entry = ws._live_sessions.pop(sid, None)
        if entry is not None:
            entry["thread"].join(timeout=5)

        assert len(waits) == 1
        _, _, _, _, ticket_id, remaining, epoch = waits[0]
        assert epoch == 1
        assert ticket_id == "t1"
        assert 0 < remaining <= 30 * 60          # what is LEFT, not a fresh window
        # Untouched: still in flight, no second question minted.
        assert store.load(wid).nodes["ask1"].status == "dispatched"

    def test_a_node_that_already_ended_is_left_alone(self, monkeypatch):
        store = _store()
        wid = _inflight_ask(store)
        store.apply("set_status", {"work_id": wid, "node_id": "ask1", "status": "done"})
        _patch(monkeypatch, [_ticket(f"{wid}::ask1", state="accepted", user_text="yes")])

        assert ws.re_arm_inflight_asks() == 0

    def test_a_live_session_is_not_disturbed(self, monkeypatch):
        """Re-arming runs at boot, but must be harmless if a thread really is alive — never two
        waiters on one question."""
        store = _store()
        wid = _inflight_ask(store)
        sid = ws.session_id_for(wid, "ask1")
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True),
                                      "started_at": datetime.now(timezone.utc), "epoch": 1}
        try:
            _patch(monkeypatch, [_ticket(f"{wid}::ask1", state="accepted", user_text="yes")])
            assert ws.re_arm_inflight_asks() == 0
            assert store.load(wid).nodes["ask1"].status == "dispatched"
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_the_newest_ticket_wins(self, monkeypatch):
        """A superseded question is not the live one — answering the stale ticket must not settle
        the node on the wrong answer."""
        store = _store()
        wid = _inflight_ask(store)
        old = _ticket(f"{wid}::ask1", state="expired", ticket_id="t_old")
        old.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        new = _ticket(f"{wid}::ask1", state="accepted", ticket_id="t_new",
                      user_text="Yes, go ahead.")
        _patch(monkeypatch, [old, new])

        assert ws.re_arm_inflight_asks() == 1
        evidence = [n for n in store.load(wid).nodes.values()
                    if n.parent_id == "ask1" and n.type == "evidence"]
        assert "Yes, go ahead" in evidence[0].content


class TestItIsActuallyCalled:
    """Recovery that nothing invokes is the same as no recovery.

    This file's six assertions all passed while `re_arm_inflight_asks` had NO production caller —
    it was written, documented "Call once at boot", tested, and never wired. The consequence was
    visible on 2026-09-17: a 9:00 AM ask was mid-question when the process restarted, and the node
    sat `dispatched` with a dead session waiting to be failed by the 80-minute orphan sweep, with
    the answer (if any) unread in the ticket store.
    """

    def test_the_scheduler_re_arms_asks_at_boot(self, monkeypatch):
        from app.assistant.dayflow_orchestrator import dayflow_scheduler as ds

        calls = []
        monkeypatch.setattr(ws, "re_arm_inflight_asks", lambda: calls.append("armed") or 1)

        sched = ds.DayflowScheduler.__new__(ds.DayflowScheduler)
        sched._started = False
        sched._subscribe_events = lambda: None
        scheduled = []
        sched._schedule_tick = lambda **kw: scheduled.append(kw.get("reason"))

        sched.start()

        assert calls == ["armed"], "boot did not reconnect in-flight asks"
        assert scheduled == ["startup"], "the startup tick was not scheduled"

    def test_recovery_runs_before_the_first_tick(self, monkeypatch):
        """Order matters: a tick that plans before recovery sees a dead ask as merely stuck, and
        can abandon or re-ask the question the user has already answered."""
        from app.assistant.dayflow_orchestrator import dayflow_scheduler as ds

        order = []
        monkeypatch.setattr(ws, "re_arm_inflight_asks", lambda: order.append("re_arm") or 0)

        sched = ds.DayflowScheduler.__new__(ds.DayflowScheduler)
        sched._started = False
        sched._subscribe_events = lambda: None
        sched._schedule_tick = lambda **kw: order.append("tick")

        sched.start()

        assert order == ["re_arm", "tick"]


@pytest.mark.parametrize("result_type,content,action,expected", [
    ("ticket_response", "User responded: yes", "acknowledge", "closed"),
    ("ticket_response", "Notify expired, user not reached", "timeout", "failed"),
    ("error", "Ticket lookup failed", "error", "failed"),
    ("success", "", "empty", "failed"),
])
def test_resumed_wait_records_then_finalizes(monkeypatch, result_type, content, action, expected):
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
    from app.assistant.utils.pydantic_classes import ToolResult
    from app.assistant.dayflow_orchestrator import node_dispatch

    store = _store()
    wid = _inflight_ask(store)
    ticket = _ticket(f"{wid}::ask1", state="proposed")
    judged = _patch(monkeypatch, [ticket])
    monkeypatch.setattr(CreateDayflowTicketTool, "_wait_for_ticket_response", staticmethod(
        lambda *args: ToolResult(result_type=result_type, content=content, data={"action": action})))
    progress = []
    monkeypatch.setattr(node_dispatch, "signal_work_progress",
                        lambda ref: progress.append(store.load(wid).nodes["ask1"].status))

    ws._run_resume_ask_session(store, wid, "ask1", ws.session_id_for(wid, "ask1"), "t1", 60, 1)

    wo = store.load(wid)
    assert wo.nodes["ask1"].status == expected
    assert wo.nodes["ask1"].payload["finalizer"]["outcome"]
    assert len(judged) == 1
    assert judged[0].scope_context.room_id == "dayflow_orchestrator"
    assert progress == [expected], "planning was signaled before the recovered result was judged"
    assert any(n.parent_id == "ask1" and n.type == "evidence" for n in wo.nodes.values())


def test_resumed_wait_uses_answer_saved_before_listener_registered(monkeypatch):
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
    from app.assistant.utils.pydantic_classes import ToolResult

    store = _store()
    wid = _inflight_ask(store)
    judged = _patch(monkeypatch, [_ticket(f"{wid}::ask1", state="accepted", user_text="Yes, received it.")])
    monkeypatch.setattr(CreateDayflowTicketTool, "_wait_for_ticket_response", staticmethod(
        lambda *args: ToolResult(result_type="ticket_response", content="Notify expired, user not reached",
                                 data={"action": "timeout"})))

    ws._run_resume_ask_session(store, wid, "ask1", ws.session_id_for(wid, "ask1"), "t1", 60, 1)

    assert store.load(wid).nodes["ask1"].status == "closed"
    assert len(judged) == 1
    assert "Yes, received it." in judged[0].information


def test_rejected_recovery_result_does_not_finalize_successor(monkeypatch):
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
    from app.assistant.utils.pydantic_classes import ToolResult

    store = _store()
    wid = _inflight_ask(store)
    judged = _patch(monkeypatch, [])

    def successor_returns(*args):
        for status in ("failed", "dispatched", "done"):
            store.apply("set_status", {"work_id": wid, "node_id": "ask1", "status": status})
        return ToolResult(result_type="ticket_response", content="Old answer", data={"action": "acknowledge"})

    monkeypatch.setattr(CreateDayflowTicketTool, "_wait_for_ticket_response", staticmethod(successor_returns))
    ws._run_resume_ask_session(store, wid, "ask1", ws.session_id_for(wid, "ask1"), "t1", 60, 1)

    assert store.load(wid).nodes["ask1"].status == "done"
    assert judged == []


def test_recovery_does_not_apply_an_older_attempts_ticket(monkeypatch):
    store = _store()
    wid = _inflight_ask(store)
    ticket = _ticket(f"{wid}::ask1", state="accepted", user_text="Old answer")
    ticket.trigger_context["dispatch_epoch"] = 0
    judged = _patch(monkeypatch, [ticket])
    assert ws.re_arm_inflight_asks() == 0
    assert store.load(wid).nodes["ask1"].status == "dispatched"
    assert not judged
