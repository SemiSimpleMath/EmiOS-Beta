"""Ask (user_reply) nodes re-enter dispatch — the re-ask / first-surface guard.

An ask parks `waiting + wake_kind=user_reply + wake_at=<re-ask time>` when its ticket is surfaced
(node_dispatch._ticket), and a repair-escalated ask sits `proposed + user_reply` with no wake_at.
Promotion (state_mover_persist) must pick BOTH up once due, the materializer must list them for the
switchboard to (re-)ticket, and a reply must always win over a re-dispatch via the reply pre-step.
The 2026-07-01 timesheets ask parked forever because every one of these paths excluded user_reply.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.state_mover_persist_node import StateMoverPersistNode
from app.assistant.control_nodes.state_mover_prep_node import StateMoverPrepNode
from app.assistant.control_nodes.work_node_materializer_node import WorkNodeMaterializerNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Ask WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    return wo.id, wo.goal_node_id


def _ask_node(store, wid, gid, *, node_id, status, wake_at, wake_ref="What do you say?"):
    store.apply("add_node", {"work_id": wid, "id": node_id, "type": "subtask", "parent_id": gid,
                             "title": "Ask the user something", "content": "Ask the user something."})
    if status != "proposed":
        if status == "waiting":
            store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "waiting"})
        else:
            store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": status})
    store.apply("defer_node", {"work_id": wid, "node_id": node_id, "wake_kind": "user_reply",
                               "wake_at": wake_at, "wake_ref": wake_ref})


def _node_status(store, wid, nid):
    return _store().load(wid).nodes[nid].status


def _run_promote(bb=None):
    node = StateMoverPersistNode(name="state_mover_persist_node", blackboard=bb or FakeBlackboard(),
                                 agent_registry={}, tool_registry={})
    node.action_handler(message=None)
    return node


class FakeTicket:
    def __init__(self, work_node_ref, user_text):
        self.trigger_context = {"work_node": work_node_ref}
        self.user_text = user_text


class FakeTicketManager:
    def __init__(self, tickets):
        self._tickets = tickets

    def get_tickets(self, **kwargs):
        return self._tickets


class TestAskPromotion:

    def test_due_reask_promotes(self):
        """waiting + user_reply whose re-ask time has passed -> actionable (the timesheets case)."""
        store = _store()
        wid, gid = _mk_wo(store)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        _ask_node(store, wid, gid, node_id="ask1", status="waiting", wake_at=past)

        _run_promote()
        assert _node_status(store, wid, "ask1") == "actionable"

    def test_inflight_ask_stays_parked(self):
        """waiting + user_reply with wake_at still in the future = the ask is OUT; leave it."""
        store = _store()
        wid, gid = _mk_wo(store)
        future = datetime.now(timezone.utc) + timedelta(minutes=50)
        _ask_node(store, wid, gid, node_id="ask1", status="waiting", wake_at=future)

        _run_promote()
        assert _node_status(store, wid, "ask1") == "waiting"

    def test_repair_escalated_ask_promotes_for_first_surface(self):
        """proposed + user_reply + no wake_at (work_repair escalate) -> actionable next tick."""
        store = _store()
        wid, gid = _mk_wo(store)
        _ask_node(store, wid, gid, node_id="ask1", status="proposed", wake_at=None)

        _run_promote()
        assert _node_status(store, wid, "ask1") == "actionable"

    def test_event_wait_still_not_promoted(self):
        store = _store()
        wid, gid = _mk_wo(store)
        store.apply("add_node", {"work_id": wid, "id": "ev1", "type": "subtask", "parent_id": gid,
                                 "title": "wait for brother"})
        store.apply("set_status", {"work_id": wid, "node_id": "ev1", "status": "waiting"})
        store.apply("defer_node", {"work_id": wid, "node_id": "ev1", "wake_kind": "event",
                                   "wake_at": None, "wake_ref": "a reply from the brother"})

        _run_promote()
        assert _node_status(store, wid, "ev1") == "waiting"

    def test_prep_shows_due_ask_as_promotion_candidate(self):
        """What the state_mover LLM sees must match what the persist promotes (hold parity)."""
        store = _store()
        wid, gid = _mk_wo(store)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        _ask_node(store, wid, gid, node_id="ask1", status="waiting", wake_at=past)

        bb = FakeBlackboard()
        prep = StateMoverPrepNode(name="state_mover_prep_node", blackboard=bb,
                                  agent_registry={}, tool_registry={})
        prep.action_handler(message=None)
        ready = bb.get_state_value("ready_work_nodes", [])
        assert any(r["task_id"] == f"{wid}::ask1" for r in ready)

    def test_held_ask_keeps_user_reply_wake(self):
        """A state_mover HOLD on a due ask pushes wake_at back but keeps wake_kind=user_reply so a
        reply arriving during the hold still matches."""
        store = _store()
        wid, gid = _mk_wo(store)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        _ask_node(store, wid, gid, node_id="ask1", status="waiting", wake_at=past)

        wake = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
        bb = FakeBlackboard({"held_work_nodes": [
            {"task_id": f"{wid}::ask1", "hold_reason": "quiet hours", "reactivate_at": wake},
        ]})
        _run_promote(bb)
        node = store.load(wid).nodes["ask1"]
        assert node.status == "waiting"
        assert node.wake_kind == "user_reply"
        assert node.wake_at is not None and node.wake_at > datetime.now(timezone.utc)


class TestAskReplyAndListing:

    def test_promoted_ask_is_listed_for_dispatch(self):
        store = _store()
        wid, gid = _mk_wo(store)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        _ask_node(store, wid, gid, node_id="ask1", status="waiting", wake_at=past)
        _run_promote()

        bb = FakeBlackboard()
        mat = WorkNodeMaterializerNode(name="work_node_materializer_node", blackboard=bb,
                                       agent_registry={}, tool_registry={})
        mat.action_handler(message=None)
        items = bb.get_state_value("actionable_items", [])
        assert any(i["item_id"] == f"{wid}::ask1" for i in items)

    def test_reply_wins_over_redispatch(self, monkeypatch):
        """A reply that arrived by materialize time completes the ask (evidence + done) instead of
        re-ticketing it — even when promotion already made it actionable this tick."""
        store = _store()
        wid, gid = _mk_wo(store)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        _ask_node(store, wid, gid, node_id="ask1", status="waiting", wake_at=past)
        _run_promote()
        assert _node_status(store, wid, "ask1") == "actionable"

        import app.assistant.ticket_manager as tm_mod
        monkeypatch.setattr(tm_mod, "get_ticket_manager",
                            lambda: FakeTicketManager([FakeTicket(f"{wid}::ask1", "Sounds good, done!")]))

        bb = FakeBlackboard()
        mat = WorkNodeMaterializerNode(name="work_node_materializer_node", blackboard=bb,
                                       agent_registry={}, tool_registry={})
        mat.action_handler(message=None)

        wo = store.load(wid)
        assert wo.nodes["ask1"].status == "done"          # completed, awaiting the finalizer's judgment
        replies = [n for n in wo.nodes.values()
                   if n.parent_id == "ask1" and n.type == "evidence"]
        assert replies and "Sounds good" in replies[0].content
        items = bb.get_state_value("actionable_items", [])
        assert not any(i["item_id"] == f"{wid}::ask1" for i in items)
