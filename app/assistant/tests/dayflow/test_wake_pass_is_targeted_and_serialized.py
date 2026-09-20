"""A work-node time wake is its OWN pass (dayflow_wake_manager), and passes run one at a time.

Until 2026-09-18 a wake was a routing hint inside the planning tick's manager: tick_router_node
jumped to the state_mover when it saw `triggered_work_node`. It read the activation Message; the
trigger was on the blackboard; so it never saw one, and every wake silently took the default
branch — a full planning pass, architect included. Three nodes sharing one wake_at were three
orchestrators on one graph twenty milliseconds apart, with no gate between them or the tick.

Now: a wake opens dayflow_wake_manager, whose state_map holds no planning stage, so there is
nothing to fall into. And both lanes hold the scheduler's _run_gate for the length of a pass.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.assistant.control_nodes.state_mover_persist_node import StateMoverPersistNode
from app.assistant.control_nodes.work_node_wake_prep_node import WorkNodeWakePrepNode
from app.assistant.dayflow_orchestrator.dayflow_scheduler import DayflowScheduler
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from app.assistant.utils.pydantic_classes import Message

_WAKE_CONFIG = Path(__file__).resolve().parents[2] / "multi_agents" / "dayflow_wake_manager" / "config.yaml"
_TICK_CONFIG = Path(__file__).resolve().parents[2] / "multi_agents" / "dayflow_orchestrator_manager" / "config.yaml"
_PLANNING_STAGES = {
    "intake_triage_prep_node", "dayflow_orchestrator::intake_triage", "triage_persist_node",
    "strategic_planner_wo_prep_node", "dayflow_orchestrator::strategic_planner_wo",
    "work_architect_node", "work_node_materializer_node", "dayflow_orchestrator::action_selector",
}


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _ready_node(store, title="Notify at five", nid="n1"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": nid, "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": title, "content": "Tell the user."})
    store.apply("defer_node", {"work_id": wo.id, "node_id": nid, "wake_kind": "time", "wake_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    return f"{wo.id}::{nid}"


def _prep(bb):
    return WorkNodeWakePrepNode(name="work_node_wake_prep_node", blackboard=bb,
                                agent_registry={}, tool_registry={})


def _activation():
    # What the manager actually hands a control node: no data on it. The trigger is on the blackboard.
    return Message(data_type="agent_activation")


class TestTheWakeManagerCannotPlan:

    def test_its_state_map_holds_no_planning_stage(self):
        cfg = yaml.safe_load(_WAKE_CONFIG.read_text(encoding="utf-8"))
        state_map = cfg["flow_config"]["state_map"]
        named = set(state_map) | set(state_map.values())
        assert not (named & _PLANNING_STAGES), named & _PLANNING_STAGES
        declared = {a["name"] for a in cfg["agents"]} | {c["name"] for c in cfg["control_nodes"]}
        undeclared = {n for n in named if n != "graceful_exit"} - declared
        assert not undeclared, f"state_map names nodes the manager does not declare: {undeclared}"
        assert cfg["flow_config"]["flow"]["normal"]["source_agent"] == "work_node_wake_prep_node"

    def test_the_planning_tick_no_longer_knows_about_wakes(self):
        cfg = yaml.safe_load(_TICK_CONFIG.read_text(encoding="utf-8"))
        state_map = cfg["flow_config"]["state_map"]
        named = set(state_map) | set(state_map.values())
        assert "tick_router_node" not in named
        assert "work_node_wake_router_node" not in named
        assert state_map["room::delegator"] == "intake_triage_prep_node"

    def test_the_scheduler_opens_the_wake_manager(self, monkeypatch):
        ref = _ready_node(_store())
        inv = _Invoker(hold=0.0)
        s, factory = _scheduler(monkeypatch, inv)
        s._fire_work_node(*ref.partition("::")[::2])
        assert factory.names == ["dayflow_wake_manager"]
        assert inv.calls == [ref]


class TestTheWakePrepStagesOneNode:

    def test_a_ready_node_is_the_state_movers_only_candidate(self):
        store = _store()
        ref = _ready_node(store)
        _ready_node(store, title="Some other ready node", nid="n2")     # a second WO, also ready
        bb = FakeBlackboard({"triggered_work_node": ref, "wake_reason": f"work_node_wake:{ref}"})
        _prep(bb).action_handler(_activation())
        assert bb.get_state_value("next_agent") is None, "state_map carries on to the state_mover"
        assert bb.get_state_value("task").startswith("Notify at five")
        assert [c["task_id"] for c in bb.get_state_value("ready_work_nodes")] == [ref]
        assert bb.get_state_value("waiting_work_nodes") == []
        assert bb.get_state_value("node_status_legend")
        assert bb.get_state_value("day_of_week")

    def test_a_node_no_longer_ready_ends_the_pass(self):
        store = _store()
        ref = _ready_node(store)
        wid, _, nid = ref.partition("::")
        for st in ("actionable", "dispatched"):
            store.apply("set_status", {"work_id": wid, "node_id": nid, "status": st})
        bb = FakeBlackboard({"triggered_work_node": ref})
        _prep(bb).action_handler(_activation())
        assert bb.get_state_value("next_agent") == "post_room_finalize_node"

    def test_a_missing_work_object_ends_the_pass(self):
        bb = FakeBlackboard({"triggered_work_node": "work_gone::n1"})
        _prep(bb).action_handler(_activation())
        assert bb.get_state_value("next_agent") == "post_room_finalize_node"

    def test_a_wake_pass_without_a_trigger_is_an_error_not_a_quiet_tick(self):
        with pytest.raises(ValueError, match="without a triggered_work_node"):
            _prep(FakeBlackboard()).action_handler(_activation())


class TestTheWakePassMovesOnlyItsNode:

    def _persist(self, bb):
        return StateMoverPersistNode(name="state_mover_persist_node", blackboard=bb,
                                     agent_registry={}, tool_registry={})

    def test_promotion_is_scoped_to_the_woken_node(self):
        store = _store()
        mine = _ready_node(store, title="Woken", nid="w")
        other = _ready_node(store, title="Bystander", nid="b")
        bb = FakeBlackboard({"triggered_work_node": mine, "held_work_nodes": []})
        self._persist(bb).action_handler(message=None)
        wid, _, nid = mine.partition("::")
        assert store.load(wid).nodes[nid].status == "actionable"
        owid, _, onid = other.partition("::")
        assert store.load(owid).nodes[onid].status == "proposed", "a wake pass never touches the rest"

    def test_a_planning_tick_still_promotes_everything_ready(self):
        store = _store()
        refs = [_ready_node(store, title=f"Ready {i}", nid=f"r{i}") for i in range(2)]
        bb = FakeBlackboard({"held_work_nodes": []})
        self._persist(bb).action_handler(message=None)
        for ref in refs:
            wid, _, nid = ref.partition("::")
            assert store.load(wid).nodes[nid].status == "actionable"


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


class _Factory:
    def __init__(self):
        self.names = []

    def create_manager(self, name):
        self.names.append(name)
        return object()


def _scheduler(monkeypatch, invoker):
    @contextmanager
    def _ctx():
        yield
    app = SimpleNamespace(app_context=_ctx)
    s = DayflowScheduler(timing_engine=SimpleNamespace(scheduler=SimpleNamespace(get_jobs=lambda: [], add_job=lambda **kw: None)), app=app)
    s._started = True
    monkeypatch.setattr("app.assistant.dayflow_orchestrator.dayflow_scheduler.setup_complete", lambda: True)
    monkeypatch.setattr("app.assistant.scope.loader.load_scope_for_source", lambda **kw: None)
    from app.assistant.ServiceLocator.service_locator import DI
    factory = _Factory()
    monkeypatch.setattr(DI, "multi_agent_manager_factory", factory, raising=False)
    monkeypatch.setattr(DI, "manager_invoker", invoker, raising=False)
    return s, factory


class TestPassesRunOneAtATime:

    def test_two_wakes_at_the_same_instant_run_one_after_the_other(self, monkeypatch):
        store = _store()
        refs = [_ready_node(store, title=f"Wake {i}", nid=f"w{i}") for i in range(3)]
        inv = _Invoker()
        s, _ = _scheduler(monkeypatch, inv)
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
        s, _ = _scheduler(monkeypatch, inv)

        s._run_gate.acquire()                        # the planning tick is running
        t = threading.Thread(target=s._fire_work_node, args=(wid, nid))
        t.start()
        time.sleep(0.2)
        assert inv.calls == [], "the wake must not run beside the tick"
        for st in ("actionable", "dispatched"):      # the tick claims the node before it finishes
            store.apply("set_status", {"work_id": wid, "node_id": nid, "status": st})
        s._run_gate.release()
        t.join(timeout=5)
        assert inv.calls == [], "re-checked inside the gate: no longer ready, no pass"


def test_tick_and_wake_render_full_task_context_for_timing():
    from app.assistant.control_nodes.state_mover_prep_node import StateMoverPrepNode
    from jinja2 import Environment, FileSystemLoader
    store = _store()
    ref = _ready_node(store, title="Research options")
    wid, nid = ref.split("::")
    directive = "Read public information. " * 20 + "Do not contact the user; save findings for the later notification."
    store.apply("set_status", {"work_id": wid, "node_id": nid, "status": "waiting", "content": directive,
        "finalizer": {"verdict": "retry", "outcome": "Previous research found a useful source; reuse it.", "dispatch_epoch": 1}})
    tick = FakeBlackboard()
    StateMoverPrepNode(name="prep", blackboard=tick, agent_registry={}, tool_registry={})._build_promotion_candidates()
    tick_candidate = next(c for c in tick.get_state_value("ready_work_nodes") if c["task_id"] == ref)
    wake = FakeBlackboard({"triggered_work_node": ref})
    _prep(wake).action_handler(_activation())
    assert wake.get_state_value("ready_work_nodes") == [tick_candidate]
    assert tick_candidate["task"]["directive"] == directive
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[2] / "agents")))
    rendered = env.get_template("dayflow_orchestrator/state_mover/prompts/user.j2").render(ready_work_nodes=[tick_candidate])
    assert directive in rendered
    assert "Previous research found a useful source; reuse it." in rendered
    assert "WORK OBJECTIVE: Research options" in rendered
