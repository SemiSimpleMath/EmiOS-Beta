"""Ask (user_reply) lifecycle — an ask is a TOOL CALL whose result is the user's reply.

Surfaced ask = `dispatched + wake_kind=user_reply` (in flight; no re-ask timer). It ends by
reply/dismissal (materializer -> done), ticket timeout (sweeper -> failed, work_repair
adjudicates), or the work object closing (cascade). A repair-escalated ask sits
`proposed + user_reply` with no wake_at and promotes for its first surface; the state_mover
may HOLD a pre-surface ask (parks `waiting`, keeps user_reply so a late reply still matches).

History: asks used to park `waiting + wake_at=<re-ask time>` and re-ticket hourly — the
2026-07-01 timesheets ask parked forever when promotion paths excluded user_reply, and the
2026-08-17 walk storm minted five tickets for one question. The 2026-08-18 owner ruling
retired the re-ask timer and made the status truthful.
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


def _add_node(store, wid, gid, node_id):
    store.apply("add_node", {"work_id": wid, "id": node_id, "type": "subtask", "parent_id": gid,
                             "title": "Ask the user something", "content": "Ask the user something."})


def _inflight_ask(store, wid, gid, *, node_id, wake_ref="What do you say?"):
    """A surfaced ask: dispatched + user_reply, no wake_at (the ticket owns the timeout)."""
    _add_node(store, wid, gid, node_id)
    store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "dispatched"})
    store.apply("defer_node", {"work_id": wid, "node_id": node_id, "wake_kind": "user_reply",
                               "wake_at": None, "wake_ref": wake_ref})


def _presurface_ask(store, wid, gid, *, node_id, wake_ref="What do you say?"):
    """A repair-escalated ask awaiting its FIRST surface: proposed + user_reply, no wake_at."""
    _add_node(store, wid, gid, node_id)
    store.apply("defer_node", {"work_id": wid, "node_id": node_id, "wake_kind": "user_reply",
                               "wake_at": None, "wake_ref": wake_ref})


def _node(store, wid, nid):
    return _store().load(wid).nodes[nid]


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

    def test_inflight_ask_is_invisible_to_promotion(self):
        """dispatched + user_reply = the question is OUT — promotion never touches it."""
        store = _store()
        wid, gid = _mk_wo(store)
        _inflight_ask(store, wid, gid, node_id="ask1")

        _run_promote()
        assert _node(store, wid, "ask1").status == "dispatched"

    def test_repair_escalated_ask_promotes_for_first_surface(self):
        """proposed + user_reply + no wake_at (work_repair escalate) -> actionable next tick."""
        store = _store()
        wid, gid = _mk_wo(store)
        _presurface_ask(store, wid, gid, node_id="ask1")

        _run_promote()
        assert _node(store, wid, "ask1").status == "actionable"

    def test_event_wait_still_not_promoted(self):
        store = _store()
        wid, gid = _mk_wo(store)
        store.apply("add_node", {"work_id": wid, "id": "ev1", "type": "subtask", "parent_id": gid,
                                 "title": "wait for brother"})
        store.apply("set_status", {"work_id": wid, "node_id": "ev1", "status": "waiting"})
        store.apply("defer_node", {"work_id": wid, "node_id": "ev1", "wake_kind": "event",
                                   "wake_at": None, "wake_ref": "a reply from the brother"})

        _run_promote()
        assert _node(store, wid, "ev1").status == "waiting"

    def test_prep_lists_presurface_ask_not_inflight_ask(self):
        """What the state_mover LLM sees must match what the persist promotes."""
        store = _store()
        wid, gid = _mk_wo(store)
        _presurface_ask(store, wid, gid, node_id="fresh")
        _inflight_ask(store, wid, gid, node_id="out")

        bb = FakeBlackboard()
        prep = StateMoverPrepNode(name="state_mover_prep_node", blackboard=bb,
                                  agent_registry={}, tool_registry={})
        prep.action_handler(message=None)
        ready = {r["task_id"] for r in bb.get_state_value("ready_work_nodes", [])}
        assert f"{wid}::fresh" in ready
        assert f"{wid}::out" not in ready

    def test_held_presurface_ask_keeps_user_reply_wake(self):
        """A state_mover HOLD on a pre-surface ask parks it `waiting` with the pushed wake_at
        but keeps wake_kind=user_reply so a late reply to an earlier ticket still matches."""
        store = _store()
        wid, gid = _mk_wo(store)
        _presurface_ask(store, wid, gid, node_id="ask1")

        wake = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
        bb = FakeBlackboard({"held_work_nodes": [
            {"task_id": f"{wid}::ask1", "hold_reason": "quiet hours", "reactivate_at": wake},
        ]})
        _run_promote(bb)
        node = _node(store, wid, "ask1")
        assert node.status == "waiting"
        assert node.wake_kind == "user_reply"
        assert node.wake_at is not None and node.wake_at > datetime.now(timezone.utc)


class TestAskReplyAndListing:

    def test_promoted_ask_is_listed_for_dispatch(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _presurface_ask(store, wid, gid, node_id="ask1")
        _run_promote()

        bb = FakeBlackboard()
        mat = WorkNodeMaterializerNode(name="work_node_materializer_node", blackboard=bb,
                                       agent_registry={}, tool_registry={})
        mat.action_handler(message=None)
        items = bb.get_state_value("actionable_items", [])
        assert any(i["item_id"] == f"{wid}::ask1" for i in items)

    def test_reply_completes_inflight_ask(self, monkeypatch):
        """A reply to an in-flight (dispatched) ask IS its result: evidence + done, no re-listing."""
        store = _store()
        wid, gid = _mk_wo(store)
        _inflight_ask(store, wid, gid, node_id="ask1")

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
