"""One thread per open task + tick-side supervision.

The dispatch GATE (work_node_dispatch_node) claims the node (-> dispatched) before any tool is
called; open_session then runs the tool on its own job thread and the dispatching pass returns
immediately. Tests that call open_session directly must claim first, exactly as the gate does —
open_session refuses an unclaimed node rather than claiming it a second way. sweep_stuck_work_nodes supervises the in-flight
jobs each tick: ORPHANED (dispatched, no live thread — restart/crash) and FROZEN (thread alive but
no subtree/job activity past the timeout) both -> failed, for work_repair to adjudicate. The
transition machine fences zombie writes (failed -> done is illegal).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.assistant.dayflow_orchestrator.node_dispatch as nd
import app.assistant.dayflow_orchestrator.work_session as ws
from app.assistant.control_nodes.work_node_dispatch_node import WorkNodeDispatchNode
from app.assistant.dayflow_orchestrator.dispatch_sweeper import sweep_stuck_work_nodes
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Job WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    return wo.id, wo.goal_node_id


def _sub(store, wid, gid, node_id, status="actionable"):
    store.apply("add_node", {"work_id": wid, "id": node_id, "type": "subtask",
                             "parent_id": gid, "title": f"step {node_id}"})
    if status != "proposed":
        store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": status})
    return node_id


def _claim(store, wid, node_id):
    """What work_node_dispatch_node does before calling any tool."""
    store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "dispatched"},
                actor="dispatch_gate")


def _long_after():
    """A moment past the longest a call may legitimately block, so the sweeper judges a
    node quiet. Moving the CLOCK rather than backdating rows keeps the test honest about
    what the sweeper actually reads."""
    from app.assistant.dayflow_orchestrator.dispatch_sweeper import _WORK_NODE_FROZEN_TIMEOUT_S
    return datetime.now(timezone.utc) + timedelta(seconds=_WORK_NODE_FROZEN_TIMEOUT_S + 60)


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestJobThread:

    def test_do_work_claims_then_runs_on_job_thread(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")
        ref = f"{wid}::n1"

        gate = threading.Event()
        signals = []
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: signals.append(r))

        # The dispatch room IS the slow thing now: open_session hands the node to
        # dayflow_dispatch_manager, which blocks until its tool returns. Patch the invocation so
        # the test controls when that happens.
        def slow_room(manager, message):
            gate.wait(timeout=5)
            store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "done"})

        from app.assistant.ServiceLocator.service_locator import DI
        monkeypatch.setattr(DI.manager_invoker, "invoke", slow_room)

        _claim(store, wid, "n1")
        ws.open_session(store, wid, "n1", "work_emi_team_manager")
        # Returned immediately: job registered and alive, worker still running.
        assert store.load(wid).nodes["n1"].status == "dispatched"
        assert ws.session_alive(wid, "n1") is True

        gate.set()
        assert _wait_for(lambda: store.load(wid).nodes["n1"].status == "done")
        assert _wait_for(lambda: not ws.session_alive(wid, "n1"))
        assert signals == [ref]

    def test_the_planning_tick_does_not_wait_for_the_tool(self, monkeypatch):
        """The whole point of the dispatch room, and the bug it closes.

        The three call stages used to run in the planning tick, so the tick lasted as long as
        the tool did — and create_dayflow_ticket blocks for the full ask window. Since
        DayflowScheduler admits one tick at a time and spaces the next from the previous tick's
        FINISH, a single unanswered notify was an hour in which nothing planned, woke, or
        dispatched. The gate must claim and return while the call is still in flight.
        """
        from app.assistant.control_nodes.work_node_dispatch_node import WorkNodeDispatchNode
        from app.assistant.ServiceLocator.service_locator import DI

        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        ref = f"{wid}::n1"

        in_call, release = threading.Event(), threading.Event()

        def blocking_room(manager, message):
            in_call.set()
            release.wait(timeout=5)          # stands in for an unanswered ticket

        monkeypatch.setattr(DI.manager_invoker, "invoke", blocking_room)
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: None)

        bb = FakeBlackboard({
            "delegate_to": "create_dayflow_ticket",
            "acted_on_item_ids": [ref],
            "actionable_items": [{"item_id": ref}],
        })
        gate = WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})
        try:
            gate.action_handler(message=None)     # must RETURN, not wait

            assert in_call.wait(timeout=5), "the dispatch room never opened"
            # The gate has returned while the call is still blocked — the tick is free.
            assert store.load(wid).nodes["n1"].status == "dispatched"
            assert ws.session_alive(wid, "n1") is True
            assert bb.get_state_value("work_node_ref") == ref
        finally:
            release.set()
            _wait_for(lambda: not ws.session_alive(wid, "n1"))

    def test_job_crash_fails_the_node(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")

        def boom(*a, **kw):
            raise RuntimeError("worker exploded")

        from app.assistant.ServiceLocator.service_locator import DI
        monkeypatch.setattr(DI.manager_invoker, "invoke", boom)

        _claim(store, wid, "n1")
        ws.open_session(store, wid, "n1", "work_emi_team_manager")
        assert _wait_for(lambda: store.load(wid).nodes["n1"].status == "failed")


class TestSupervision:

    def test_a_node_that_has_gone_quiet_is_failed(self):
        """The call that owned it stopped existing — the run crashed, or it wedged. Both
        look identical from the graph (nothing written), and both want the same remedy."""
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})

        sweep_stuck_work_nodes(now_utc=_long_after())
        assert store.load(wid).nodes["n1"].status == "failed"

    def test_a_node_still_inside_its_call_is_left_alone(self):
        """THE invariant that protects a live question. An ask blocks for the whole window
        it was given and writes nothing meanwhile; failing it would take a question away
        from the user while they are still looking at it."""
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})

        sweep_stuck_work_nodes()
        assert store.load(wid).nodes["n1"].status == "dispatched"

    def test_live_fresh_job_is_left_alone(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})
        sid = ws.session_id_for(wid, "n1")
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True),
                                      "started_at": datetime.now(timezone.utc)}
        try:
            sweep_stuck_work_nodes()
            assert store.load(wid).nodes["n1"].status == "dispatched"
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_frozen_job_is_failed_and_zombie_write_is_fenced(self):
        """Idle past the frozen timeout, which is derived from the longest legitimate tool call
        (see dispatch_sweeper) — so this backdates relative to that rather than to a literal, and
        stays correct if the ask window moves."""
        from app.assistant.dayflow_orchestrator.dispatch_sweeper import _WORK_NODE_FROZEN_TIMEOUT_S
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})
        sid = ws.session_id_for(wid, "n1")
        old = datetime.now(timezone.utc) - timedelta(seconds=_WORK_NODE_FROZEN_TIMEOUT_S + 600)
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True), "started_at": old}
        # Backdate the whole subtree so "no activity" holds.
        conn = getattr(store, "_conn", None)
        conn.execute("UPDATE nodes SET updated_at=? WHERE work_id=?", (old.isoformat(), wid))
        conn.commit()
        try:
            sweep_stuck_work_nodes()
            wo = store.load(wid)
            assert wo.nodes["n1"].status == "failed"
            # The abandoned zombie's late 'done' write is rejected by the transition machine.
            import pytest
            with pytest.raises(ValueError):
                store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "done"})
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_goal_node_is_skipped(self):
        store = _store()
        wid, gid = _mk_wo(store)
        store.apply("set_status", {"work_id": wid, "node_id": gid, "status": "dispatched"})

        sweep_stuck_work_nodes()
        assert store.load(wid).nodes[gid].status == "dispatched"

    def test_subtree_nodes_under_live_session_are_not_orphaned(self):
        # A worker grows sub-steps inside its session; they inherit payload.session_id
        # at creation (store-level), so supervision is one lookup — a live session's
        # subtree is never orphan-failed (the 2026-07-31 duplicate-team class).
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        sid = ws.session_id_for(wid, "n1")
        store.apply("set_status", {"work_id": wid, "node_id": "n1",
                                   "status": "dispatched", "session_id": sid})
        store.apply("add_node", {"work_id": wid, "id": "n1a", "type": "subtask",
                                 "parent_id": "n1", "title": "worker sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a", "status": "dispatched"})
        store.apply("add_node", {"work_id": wid, "id": "n1a1", "type": "subtask",
                                 "parent_id": "n1a", "title": "worker sub-sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a1", "status": "dispatched"})
        wo = store.load(wid)
        assert wo.nodes["n1a"].payload.get("session_id") == sid   # inherited at creation
        assert wo.nodes["n1a1"].payload.get("session_id") == sid  # two levels deep
        with ws._sessions_lock:
            ws._live_sessions[sid] = {"thread": SimpleNamespace(is_alive=lambda: True),
                                      "started_at": datetime.now(timezone.utc)}
        try:
            sweep_stuck_work_nodes()
            wo = store.load(wid)
            assert wo.nodes["n1"].status == "dispatched"
            assert wo.nodes["n1a"].status == "dispatched"
            assert wo.nodes["n1a1"].status == "dispatched"
        finally:
            with ws._sessions_lock:
                ws._live_sessions.pop(sid, None)

    def test_a_whole_quiet_subtree_is_failed(self):
        """A run that dies strands everything it grew, not just the node it was given."""
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})
        store.apply("add_node", {"work_id": wid, "id": "n1a", "type": "subtask",
                                 "parent_id": "n1", "title": "worker sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a", "status": "dispatched"})

        sweep_stuck_work_nodes(now_utc=_long_after())
        wo = store.load(wid)
        assert wo.nodes["n1"].status == "failed"
        assert wo.nodes["n1a"].status == "failed"

    def test_a_quiet_sub_step_is_failed_even_after_its_root_already_was(self):
        """Each dispatched node is judged on its own quiet, so an abandoned sub-step
        cannot be left in flight forever by whatever happened to its parent."""
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})
        store.apply("add_node", {"work_id": wid, "id": "n1a", "type": "subtask",
                                 "parent_id": "n1", "title": "worker sub-step"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1a", "status": "dispatched"})
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "failed"})

        sweep_stuck_work_nodes(now_utc=_long_after())
        assert store.load(wid).nodes["n1a"].status == "failed"


class TestOneDispatchPerTick:

    def test_dispatch_claims_clears_acted_on_and_signals_more_ready(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")
        _sub(store, wid, gid, "n2")
        signals = []
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: signals.append(r))

        bb = FakeBlackboard({
            "acted_on_item_ids": [f"{wid}::n1"],
            "delegate_to": "run_work_node",
            "actionable_items": [
                {"item_id": f"{wid}::n1"},
                {"item_id": f"{wid}::n2"},
            ],
        })
        node = WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})
        node.action_handler(message=None)

        # The gate claimed it BEFORE the tool is called — that is what makes it invisible to
        # every other consumer for the rest of the call. The CALL is the next two stages.
        assert store.load(wid).nodes["n1"].status == "dispatched"
        assert bb.get_state_value("work_node_ref") == f"{wid}::n1"
        assert bb.get_state_value("acted_on_item_ids") == []
        # Falls through: the state_map runs the arguments node, then the tool caller.
        assert bb.get_state_value("next_agent") is None
        assert signals == ["more_ready:1"]

    def test_no_signal_when_nothing_else_ready(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")
        signals = []
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: signals.append(r))

        bb = FakeBlackboard({
            "acted_on_item_ids": [f"{wid}::n1"],
            "delegate_to": "create_dayflow_ticket",
            "actionable_items": [{"item_id": f"{wid}::n1"}],
        })
        WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                             agent_registry={}, tool_registry={}).action_handler(message=None)
        assert signals == []


class TestRefCanonicalization:
    """The selector ECHOES ids from its rendered list; the runtime dispatches the
    CANONICAL id from the offered set, never the transcription (2026-08-22: the
    prompt's "- task: " label glued onto the id made dispatch AND the fail path
    KeyError, so the node re-fired every pass)."""

    def _node(self, bb):
        return WorkNodeDispatchNode(name="work_node_dispatch_node", blackboard=bb,
                                    agent_registry={}, tool_registry={})

    def test_glued_label_prefix_is_canonicalized(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "deliver_x--2a403d")
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: None)
        bb = FakeBlackboard({
            "acted_on_item_ids": [f"task:{wid}::deliver_x--2a403d"],
            "delegate_to": "create_dayflow_ticket",
            "actionable_items": [{"item_id": f"{wid}::deliver_x--2a403d"}],
        })
        self._node(bb).action_handler(message=None)
        # The CANONICAL id is what the following stages see, never the transcription.
        assert bb.get_state_value("work_node_ref") == f"{wid}::deliver_x--2a403d"
        assert store.load(wid).nodes["deliver_x--2a403d"].status == "dispatched"

    def test_exact_match_passes_through(self, monkeypatch):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")
        _sub(store, wid, gid, "n2")
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: None)
        bb = FakeBlackboard({
            "acted_on_item_ids": [f"{wid}::n1"],
            "delegate_to": "run_work_node",
            "actionable_items": [{"item_id": f"{wid}::n1"}, {"item_id": f"{wid}::n2"}],
        })
        self._node(bb).action_handler(message=None)
        assert bb.get_state_value("work_node_ref") == f"{wid}::n1"
        assert store.load(wid).nodes["n1"].status == "dispatched"

    def test_ambiguous_echo_left_unchanged_and_fails_loud(self, monkeypatch):
        """Two offered ids both contained in the echo -> no unique join; the echo is NOT
        silently rewritten to either candidate. It fails at the CLAIM (no such node), which
        now ends the run rather than being absorbed — no tool is ever called with a
        transcribed id, and neither candidate is touched."""
        import pytest
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1")
        _sub(store, wid, gid, "n12")
        monkeypatch.setattr(nd, "signal_work_progress", lambda r: None)
        bb = FakeBlackboard({
            "acted_on_item_ids": [f"{wid}::n1 and {wid}::n12"],
            "delegate_to": "run_work_node",
            "actionable_items": [{"item_id": f"{wid}::n1"}, {"item_id": f"{wid}::n12"}],
        })
        with pytest.raises(Exception):
            self._node(bb).action_handler(message=None)
        # Neither candidate was touched — no silent rewrite to whichever looked closest.
        assert store.load(wid).nodes["n1"].status == "actionable"
        assert store.load(wid).nodes["n12"].status == "actionable"


class TestTargetedWakeRouting:
    """A scheduler time-wake becomes a targeted room invocation — same room, same dispatch, no
    bespoke wake path. It routes through the STATE_MOVER first: no re-planning, but the moment is
    always re-judged, because the world moved since the timer was set. work_node_wake_router_node
    then dispatches the node, or lets the pass end if it was held.

    The trigger's `triggered_work_node` reaches the router on the BLACKBOARD (the manager copies the
    trigger's data there once); the activation Message a control node receives carries no data.
    These tests used to hand it in via message.data, which the router read — and that is why the
    router in production never saw a wake and ran every one as a full planning tick (2026-09-18)."""

    def _router(self, bb):
        from app.assistant.control_nodes.tick_router_node import TickRouterNode
        return TickRouterNode(name="tick_router_node", blackboard=bb,
                              agent_registry={}, tool_registry={})

    def _wake_router(self, bb):
        from app.assistant.control_nodes.work_node_wake_router_node import WorkNodeWakeRouterNode
        return WorkNodeWakeRouterNode(name="work_node_wake_router_node", blackboard=bb,
                                      agent_registry={}, tool_registry={})

    def test_ready_node_goes_to_the_state_mover_first(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        bb = FakeBlackboard({"triggered_work_node": f"{wid}::n1", "wake_reason": "t"})
        self._router(bb).action_handler(SimpleNamespace(data={}))
        assert bb.get_state_value("next_agent") == "state_mover_prep_node"
        assert bb.get_state_value("triggered_work_node") == f"{wid}::n1"
        assert "step n1" in bb.get_state_value("task")

    def test_a_node_the_state_mover_left_alone_dispatches(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        bb = FakeBlackboard({"triggered_work_node": f"{wid}::n1"})
        self._wake_router(bb).action_handler(message=None)
        assert bb.get_state_value("next_agent") == "dayflow_orchestrator::switchboard"
        assert bb.get_state_value("acted_on_item_ids") == [f"{wid}::n1"]
        assert bb.get_state_value("actionable_items") == [{"item_id": f"{wid}::n1"}]

    def test_the_switchboard_is_given_the_node_it_must_route(self):
        """The switchboard routes on `task`/`information` — its only view of the node.

        This pass skips the materializer -> action_selector -> router chain that fills them on a
        normal tick, so it has to fill them itself. It did not, and the switchboard was handed an
        EMPTY task: with nothing but the clock left in its prompt it routed on the clock, answering
        "UI notification showing the current time" -> create_dayflow_ticket. Every
        time-waked node went that way. On 2026-09-16 that ticketed "set the AC to 70F" back to the
        user at 21:00 and "turn off the whole-house lights" at 22:00 — two device actions the user
        had to answer by hand instead of them simply happening.
        """
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "actionable",
                                   "content": "Turn off all whole-house lights globally."})

        bb = FakeBlackboard({"triggered_work_node": f"{wid}::n1"})
        self._wake_router(bb).action_handler(message=None)

        task = bb.get_state_value("task") or ""
        info = bb.get_state_value("information") or ""
        assert task.strip(), "the switchboard would route on an empty task"
        assert "lights" in info, "the switchboard must see the node's actual goal"

    def test_a_node_with_no_goal_at_all_is_refused_not_routed(self):
        """Routing on an empty goal is what broke; guessing at one would hide the next occurrence."""
        import pytest

        store = _store()
        wid, gid = _mk_wo(store)
        store.apply("add_node", {"work_id": wid, "id": "blank", "type": "subtask",
                                 "parent_id": gid, "title": ""})
        store.apply("set_status", {"work_id": wid, "node_id": "blank", "status": "actionable"})

        bb = FakeBlackboard({"triggered_work_node": f"{wid}::blank"})
        with pytest.raises(ValueError, match="neither title nor content"):
            self._wake_router(bb).action_handler(message=None)

    def test_a_held_node_does_not_fire(self):
        """The hole this closes: a 10pm reminder used to fire regardless of quiet hours purely
        because it arrived through the timed door. A HOLD parks it `waiting`, and its
        reactivate_at re-arms the wake — nothing is lost, it just is not now."""
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "waiting"})
        bb = FakeBlackboard({"triggered_work_node": f"{wid}::n1"})
        self._wake_router(bb).action_handler(message=None)
        # Straight to the room's tail: the finalizer runs in the dispatch room that made the
        # call, so a pass which dispatched nothing has nothing here to judge.
        assert bb.get_state_value("next_agent") == "post_room_finalize_node"
        assert not bb.get_state_value("acted_on_item_ids", [])

    def test_a_normal_tick_falls_through_to_the_materializer(self):
        bb = FakeBlackboard({})
        self._wake_router(bb).action_handler(message=None)
        # next_agent untouched -> the state_map's default (the materializer) applies.
        assert bb.get_state_value("next_agent") is None

    def test_stale_wake_exits_cleanly(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _sub(store, wid, gid, "n1", status="actionable")
        store.apply("set_status", {"work_id": wid, "node_id": "n1", "status": "dispatched"})
        bb = FakeBlackboard({"triggered_work_node": f"{wid}::n1"})
        self._router(bb).action_handler(SimpleNamespace(data={}))
        assert bb.get_state_value("next_agent") == "post_room_finalize_node"

    def test_missing_work_object_exits_cleanly(self):
        bb = FakeBlackboard({"triggered_work_node": "work_gone::n1"})
        self._router(bb).action_handler(SimpleNamespace(data={}))
        assert bb.get_state_value("next_agent") == "post_room_finalize_node"
