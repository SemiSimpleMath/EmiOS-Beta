"""A work-node time wake is a TARGETED pass, and passes run one at a time.

Two defects found together on 2026-09-18, reading the 18:00 log of the day before:

  * tick_router_node read `triggered_work_node` off the activation Message. The manager copies the
    trigger's data onto the blackboard once and then activates control nodes with a bare Message,
    so the router never saw it: every timed wake ran the FULL pipeline — intake, steward, architect —
    and only the wake router at the tail knew it was a wake.
  * _fire_work_node had no gate against the planning tick or against other wakes. Three nodes
    sharing one wake_at were three orchestrator passes on one graph twenty milliseconds apart.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from app.assistant.control_nodes.tick_router_node import TickRouterNode
from app.assistant.dayflow_orchestrator.dayflow_scheduler import DayflowScheduler
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from app.assistant.utils.pydantic_classes import Message


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _ready_node(store, title="Notify at five", nid="n1"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": nid, "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": title, "content": "Tell the user."})
    return f"{wo.id}::{nid}"


class TestTheWakePassIsTargeted:

    def test_a_wake_on_the_blackboard_routes_straight_to_the_state_mover(self):
        ref = _ready_node(_store())
        # What the manager actually hands a control node: the trigger's data is on the
        # blackboard, the activation Message carries none of it.
        bb = FakeBlackboard({"triggered_work_node": ref, "wake_reason": f"work_node_wake:{ref}"})
        TickRouterNode(name="tick_router_node", blackboard=bb, agent_registry={}, tool_registry={}) \
            .action_handler(Message(data_type="agent_activation"))
        assert bb.get_state_value("next_agent") == "state_mover_prep_node", \
            "a wake must skip intake / steward / architect"
        assert bb.get_state_value("task").startswith("Notify at five")
        assert bb.get_state_value("triggered_work_node") == ref

    def test_a_wake_only_in_message_data_is_not_a_wake(self):
        """The old read. If this ever routes, someone put the data back on the Message and the
        blackboard will disagree with it."""
        ref = _ready_node(_store())
        bb = FakeBlackboard()
        TickRouterNode(name="tick_router_node", blackboard=bb, agent_registry={}, tool_registry={}) \
            .action_handler(Message(data_type="agent_activation", data={"triggered_work_node": ref}))
        assert bb.get_state_value("next_agent") is None      # state_map carries on: normal tick

    def test_a_normal_tick_falls_through(self):
        bb = FakeBlackboard({"wake_reason": "ceiling"})
        TickRouterNode(name="tick_router_node", blackboard=bb, agent_registry={}, tool_registry={}) \
            .action_handler(Message(data_type="agent_activation"))
        assert bb.get_state_value("next_agent") is None

    def test_a_wake_for_a_node_that_is_no_longer_ready_exits_cleanly(self):
        store = _store()
        ref = _ready_node(store)
        wid, _, nid = ref.partition("::")
        for st in ("actionable", "dispatched"):
            store.apply("set_status", {"work_id": wid, "node_id": nid, "status": st})
        bb = FakeBlackboard({"triggered_work_node": ref})
        TickRouterNode(name="tick_router_node", blackboard=bb, agent_registry={}, tool_registry={}) \
            .action_handler(Message(data_type="agent_activation"))
        assert bb.get_state_value("next_agent") == "post_room_finalize_node"


class _Invoker:
    """Records how many manager passes overlap. Each invoke sleeps so overlap is observable."""

    def __init__(self, hold=0.15):
        self.hold = hold
        self.active = 0
        self.peak = 0
        self.calls = []
        self._lock = threading.Lock()

    def invoke(self, manager, msg):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.calls.append(msg.data.get("triggered_work_node"))
            time.sleep(self.hold)
        finally:
            with self._lock:
                self.active -= 1


def _scheduler(monkeypatch, invoker):
    @contextmanager
    def _ctx():
        yield
    app = SimpleNamespace(app_context=_ctx)
    s = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=SimpleNamespace()), app=app)
    s._started = True
    monkeypatch.setattr("app.assistant.dayflow_orchestrator.dayflow_scheduler.setup_complete", lambda: True)
    monkeypatch.setattr("app.assistant.scope.loader.load_scope_for_source", lambda **kw: None)
    from app.assistant.ServiceLocator.service_locator import DI
    monkeypatch.setattr(DI, "multi_agent_manager_factory",
                        SimpleNamespace(create_manager=lambda name: object()), raising=False)
    monkeypatch.setattr(DI, "manager_invoker", invoker, raising=False)
    return s


class TestPassesRunOneAtATime:

    def test_two_wakes_at_the_same_instant_run_one_after_the_other(self, monkeypatch):
        store = _store()
        refs = [_ready_node(store, title=f"Wake {i}", nid=f"w{i}") for i in range(3)]
        inv = _Invoker()
        s = _scheduler(monkeypatch, inv)
        threads = [threading.Thread(target=s._fire_work_node, args=tuple(r.partition("::")[::2]))
                   for r in refs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert sorted(inv.calls) == sorted(refs), "every wake still runs"
        assert inv.peak == 1, f"{inv.peak} passes overlapped"

    def test_a_wake_waits_for_the_planning_tick_and_re_checks_readiness(self, monkeypatch):
        """The tick holds the same gate. A wake fired mid-tick waits; when the tick has dispatched
        that very node meanwhile, the wake finds it not ready and does nothing."""
        store = _store()
        ref = _ready_node(store)
        wid, _, nid = ref.partition("::")
        inv = _Invoker(hold=0.0)
        s = _scheduler(monkeypatch, inv)

        s._run_gate.acquire()                        # the planning tick is running
        t = threading.Thread(target=s._fire_work_node, args=(wid, nid))
        t.start()
        time.sleep(0.2)
        assert inv.calls == [], "the wake must not run beside the tick"
        # The tick claims the node before it finishes.
        for st in ("actionable", "dispatched"):
            store.apply("set_status", {"work_id": wid, "node_id": nid, "status": st})
        s._run_gate.release()
        t.join(timeout=5)
        assert inv.calls == [], "re-checked inside the gate: no longer ready, no pass"
