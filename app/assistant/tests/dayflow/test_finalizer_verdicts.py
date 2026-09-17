"""The work finalizer: what it judges, and what each verdict writes to the graph.

One agent call per pass, on the ONE node that pass dispatched, and the verdict is written to
emi.db in the same control node — the agent's schema is already in hand there, so nothing is
handed to a second node through a blackboard. (It was two nodes until 2026-09-16.)

The part worth guarding hardest is the handoff to the architect. An `amend` or `replan` is advice
for a planner that runs on a LATER tick, and every tick builds a fresh manager with a fresh
Blackboard — so the instruction has to be on the node, in the database, or it is gone before its
reader exists. It was gone, silently, and both halves had passing tests.

Its reach is the node it judged. Until 2026-09-16 a third verdict, RESOLVE, let it set the whole
WorkObject done or abandoned from one node's result — that belongs to the steward, which sees
every work object's outcomes each tick.
"""
from __future__ import annotations

import pytest

from app.assistant.control_nodes.work_architect_node import (
    _finalizer_block, _pending_finalizer_instructions,
)
from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _wo_with_judged_node(store, title="Finalizer WO"):
    """A top-level node whose call RETURNED — sitting at `done`, awaiting judgment."""
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "n1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Research the deadline"})
    for st in ("actionable", "dispatched", "done"):
        store.apply("set_status", {"work_id": wo.id, "node_id": "n1", "status": st})
    return wo.id


def _failed_wo(store, title="Failed WO"):
    """A top-level node whose call FAILED — the tool reported it could not run."""
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "nf1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Call that failed"})
    for st in ("actionable", "dispatched", "failed"):
        store.apply("set_status", {"work_id": wo.id, "node_id": "nf1", "status": st})
    return wo.id


def _run(monkeypatch, work_id, node_id, verdict_data):
    """Drive the finalizer over one node with the agent call stubbed to `verdict_data`."""
    monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)
    monkeypatch.setattr(WorkFinalizerNode, "_judge",
                        lambda self, wo, node, scope: verdict_data)
    bb = FakeBlackboard({"work_node_ref": f"{work_id}::{node_id}"})
    WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                      agent_registry={}, tool_registry={}).action_handler(message=None)
    return bb


class TestWhatGetsJudged:

    def test_it_judges_only_the_node_that_ran(self, monkeypatch):
        """Not a sweep. Another work object sitting at `done` is none of this pass's
        business — a run makes one call, so one result can have appeared."""
        store = _store()
        mine = _wo_with_judged_node(store, title="The one that ran")

        # A second work object, also at `done`. Node ids are globally unique, so it gets its own.
        other = store.apply("create_work_object", {"title": "Someone else's finished node",
                                                   "goal_content": "other",
                                                   "satisfied_when_kind": "all_owned_children_done"})
        store.apply("add_node", {"work_id": other.id, "id": "other_n1", "type": "subtask",
                                 "parent_id": other.goal_node_id, "title": "Not this pass's business"})
        for st in ("actionable", "dispatched", "done"):
            store.apply("set_status", {"work_id": other.id, "node_id": "other_n1", "status": st})

        _run(monkeypatch, mine, "n1", {"verdict": "proceed", "reasoning": "done"})

        assert store.load(mine).nodes["n1"].status == "closed"
        assert store.load(other.id).nodes["other_n1"].status == "done", "untouched"

    def test_a_pass_that_ran_nothing_judges_nothing(self, monkeypatch):
        store = _store()
        wid = _wo_with_judged_node(store)
        monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)
        bb = FakeBlackboard()          # no work_node_ref — nothing was dispatched
        WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)

        assert bb.get_state_value("work_finalizer_result", []) == []
        assert store.load(wid).nodes["n1"].status == "done"

    def test_a_failed_node_IS_judged(self, monkeypatch):
        """Judging the failure is the point: the result text is the only account of the failure
        MODE, and the mode is what decides whether trying again could go differently."""
        store = _store()
        wid = _failed_wo(store)
        seen = {}

        monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)

        def _judge(self, wo, node, scope):
            seen["status"] = node.status
            return {"verdict": "blocked", "reasoning": "the account cannot be reached at all"}

        monkeypatch.setattr(WorkFinalizerNode, "_judge", _judge)
        bb = FakeBlackboard({"work_node_ref": f"{wid}::nf1"})
        WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)

        assert seen["status"] == "failed", "the finalizer must see the failed node"

    def test_a_worker_internal_node_is_not_judged(self, monkeypatch):
        """Only the architect's own units count toward the goal. A worker's nested checklist step
        is internal to the call that grew it."""
        store = _store()
        wid = _wo_with_judged_node(store)
        store.apply("add_node", {"work_id": wid, "id": "n1_child", "type": "subtask",
                                 "parent_id": "n1", "title": "the worker's own step"})
        for st in ("actionable", "dispatched", "done"):
            store.apply("set_status", {"work_id": wid, "node_id": "n1_child", "status": st})

        _run(monkeypatch, wid, "n1_child", {"verdict": "proceed", "reasoning": "nope"})

        assert store.load(wid).nodes["n1_child"].status == "done", "not the finalizer's to close"


class TestTheVerdictsWrite:

    def test_proceed_closes_the_node_and_completes_the_goal(self, monkeypatch):
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1",
             {"verdict": "proceed", "reasoning": "the deadline is confirmed"})

        wo = store.load(wid)
        assert wo.nodes["n1"].status == "closed"   # the satisfied terminal is_satisfied keys on
        assert wo.status == "done"                 # rollup completes the goal on the last close

    def test_amend_closes_the_node_and_records_the_revised_intent(self, monkeypatch):
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1", {
            "verdict": "amend", "reasoning": "the packet details were never found",
            "amend_intent": "ask the school directly instead of searching",
        })

        node = store.load(wid).nodes["n1"]
        assert node.status == "closed"
        handed = node.payload["finalizer"]
        assert handed["instruction"] == "ask the school directly instead of searching"
        assert handed["verdict"] == "amend"
        # Closing writes the epitaph; the instruction sits beside it, not instead of it.
        assert node.payload["terminal"]["status"] == "closed"

    def test_replan_reopens_the_node_to_the_architects_inbox(self, monkeypatch):
        store = _store()
        wid = _failed_wo(store)

        _run(monkeypatch, wid, "nf1", {
            "verdict": "replan", "reasoning": "auth expired",
            "replan_instruction": "re-authorise, then repeat the same lookup",
        })

        node = store.load(wid).nodes["nf1"]
        assert node.status == "proposed"
        handed = node.payload["finalizer"]
        assert handed["verdict"] == "replan"
        assert "re-authorise" in handed["instruction"]
        assert handed["reasoning"] == "auth expired"

    def test_blocked_leaves_the_node_failed_but_records_why(self, monkeypatch):
        """`blocked` changes no status — `failed` is already the truth — but the finalizer's
        account of WHY still has to reach the graph, or the steward inherits a mute failure.
        It used to reach nothing at all: no status change meant no write of any kind."""
        store = _store()
        wid = _failed_wo(store)

        _run(monkeypatch, wid, "nf1",
             {"verdict": "blocked", "reasoning": "there is no route to that account"})

        node = store.load(wid).nodes["nf1"]
        assert node.status == "failed", "nothing pretends it succeeded"
        assert node.payload["finalizer"]["reasoning"] == "there is no route to that account"

    def test_it_never_ends_a_work_object(self, monkeypatch):
        """A goal that has become moot is the steward's call. No verdict reaches set_work_status."""
        store = _store()
        wid = _wo_with_judged_node(store)
        store.apply("add_node", {"work_id": wid, "id": "n2", "type": "subtask",
                                 "parent_id": store.load(wid).goal_node_id, "title": "Still to do"})

        _run(monkeypatch, wid, "n1", {
            "verdict": "amend", "reasoning": "the restaurant is closed; this goal may be moot",
            "amend_intent": "nothing further is possible here",
        })

        wo = store.load(wid)
        assert wo.status == "active", "only the steward ends a work object"
        assert wo.nodes["n2"].status == "proposed", "the rest of the plan is untouched"

    def test_an_unusable_verdict_is_refused_loudly(self, monkeypatch):
        store = _store()
        wid = _wo_with_judged_node(store)

        bb = _run(monkeypatch, wid, "n1",
                  {"verdict": "resolve", "reasoning": "retired verdict"})

        assert store.load(wid).nodes["n1"].status == "done"   # nothing applied
        assert bb.get_state_value("work_finalizer_result") == []


class TestTheContract:
    """A replan must name what will be DIFFERENT. An unchanged retry reproduces the error —
    which is how one bad argument became 22 identical attempts in two hours."""

    def test_replan_without_an_instruction_is_refused_by_the_contract(self):
        from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm

        with pytest.raises(ValueError, match="replan_instruction is required"):
            AgentForm(reasoning="it failed", verdict="replan")

    def test_replan_with_an_instruction_is_accepted(self):
        from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm

        form = AgentForm(reasoning="the account was not authorised", verdict="replan",
                         replan_instruction="re-authorise the Google account first, then the "
                                            "same lookup works")
        assert form.verdict == "replan"

    def test_blocked_needs_no_extra_field(self):
        from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm

        assert AgentForm(reasoning="no route exists", verdict="blocked").verdict == "blocked"


class TestRepeatedFailure:
    """Two failures at the same step means the step is wrong, not the luck.

    Nothing counted failures, so every failure looked like a first failure to every agent that saw
    one: a delivery node wrote the SAME tool error onto its graph seventeen times, and the only
    thing that grew was a count the projections read back as progress.
    """

    def test_each_failure_is_counted_on_the_node(self):
        store = _store()
        wid = _failed_wo(store)                      # already failed once
        assert store.load(wid).nodes["nf1"].payload["failure_count"] == 1

        for _ in range(2):                           # failed -> dispatched -> failed
            store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "dispatched"})
            store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "failed"})
        assert store.load(wid).nodes["nf1"].payload["failure_count"] == 3

    def test_re_entering_failed_is_what_counts_not_every_write(self):
        """A status write that leaves the node `failed` (the finalizer's `blocked` verdict does
        exactly this) must not inflate the count — otherwise judging a failure looks like one."""
        store = _store()
        wid = _failed_wo(store)
        store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "failed",
                                   "reason": "still failed"})
        assert store.load(wid).nodes["nf1"].payload["failure_count"] == 1

    def test_the_finalizer_is_told_when_a_step_keeps_failing(self, monkeypatch):
        """The finalizer judges ONE result, so repetition is invisible to it unless stated."""
        from app.assistant.control_nodes.work_finalizer_node import _REPEAT_FAILURE_LIMIT

        store = _store()
        wid = _failed_wo(store)
        for _ in range(_REPEAT_FAILURE_LIMIT):
            store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "dispatched"})
            store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "failed"})

        seen = {}
        monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)

        real_judge = WorkFinalizerNode._judge

        def _capture(self, wo, node, scope):
            # Intercept the prompt the agent would receive.
            import app.assistant.control_nodes.work_finalizer_node as mod

            class _Spy:
                def action_handler(_s, message):
                    seen["information"] = message.information
                    class _R: data = {"verdict": "blocked", "reasoning": "cannot be made to work"}
                    return _R()

            monkeypatch.setattr(mod.DI, "agent_factory",
                                type("F", (), {"create_agent": staticmethod(lambda *a, **k: _Spy())})())
            return real_judge(self, wo, node, scope)

        monkeypatch.setattr(WorkFinalizerNode, "_judge", _capture)
        bb = FakeBlackboard({"work_node_ref": f"{wid}::nf1"})
        WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)

        info = seen.get("information", "")
        assert "HAS NOW FAILED" in info, "the finalizer was not told the step keeps failing"
        assert "ask" in info.lower(), "it must be offered the ask-the-user route, not only 'stop'"

    def test_a_first_failure_gets_no_such_warning(self, monkeypatch):
        """One failure is ordinary. Crying wolf on it would make the real signal worthless."""
        store = _store()
        wid = _failed_wo(store)                      # exactly one failure
        seen = {}
        monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)
        real_judge = WorkFinalizerNode._judge

        def _capture(self, wo, node, scope):
            import app.assistant.control_nodes.work_finalizer_node as mod

            class _Spy:
                def action_handler(_s, message):
                    seen["information"] = message.information
                    class _R: data = {"verdict": "blocked", "reasoning": "no"}
                    return _R()

            monkeypatch.setattr(mod.DI, "agent_factory",
                                type("F", (), {"create_agent": staticmethod(lambda *a, **k: _Spy())})())
            return real_judge(self, wo, node, scope)

        monkeypatch.setattr(WorkFinalizerNode, "_judge", _capture)
        bb = FakeBlackboard({"work_node_ref": f"{wid}::nf1"})
        WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)

        assert "HAS NOW FAILED" not in seen.get("information", "")

    def test_the_steward_sees_the_repeat_count_in_its_portfolio(self):
        from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio

        store = _store()
        wid = _failed_wo(store)
        for _ in range(2):
            store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "dispatched"})
            store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "failed"})

        rendered = render_work_portfolio(store.load(wid))
        assert "HAS FAILED 3 TIMES" in rendered
        assert "Retrying it will not help" in rendered


class TestTheHandoffToTheArchitect:
    """The instruction has to outlive the tick that produced it. This is where it didn't."""

    def test_the_instruction_survives_into_the_next_tick(self, monkeypatch):
        """The bug every other test here missed, for both of the reasons it was missable.

        The contract refused a replan with no instruction, and the writer handed one on
        correctly — but the writer's tests fed it a verdict dict built BY HAND with the field
        already present, while the real producer never copied it out of the agent's schema. And
        the handoff itself lived on the manager's Blackboard, which does not exist by the time
        the architect runs. So this drives the real producer and then reads the way the next
        tick reads: from the database, with nothing in memory carried across.
        """
        store = _store()
        wid = _failed_wo(store)

        _run(monkeypatch, wid, "nf1", {
            "verdict": "replan", "reasoning": "the token had expired",
            "replan_instruction": "re-authorise the Google account first, then the same lookup works",
        })

        # THE NEXT TICK: a fresh manager, a fresh blackboard, nothing shared but emi.db.
        pending = _pending_finalizer_instructions(store)
        assert wid in pending, (
            "the finalizer's instruction did not outlive its tick — the architect that reads it "
            "runs later, with a fresh blackboard, and would find nothing")
        entry = pending[wid][0]
        assert entry["node_id"] == "nf1"
        assert "re-authorise" in entry["instruction"]
        assert entry["reasoning"] == "the token had expired"

        # And the architect reads it as a licence to prune, which an empty instruction is not.
        block, instruction = _finalizer_block(pending[wid])
        assert instruction, "no instruction means no prune licence"
        assert "nf1" in block and "re-authorise" in block

    def test_an_instruction_is_read_once_and_not_re_applied_every_tick(self, monkeypatch):
        """Consumed after the architect acts, or the same judgment is re-applied for the life of
        the work object — a step dealt with ticks ago keeps re-arriving as fresh advice."""
        store = _store()
        wid = _failed_wo(store)

        _run(monkeypatch, wid, "nf1", {
            "verdict": "replan", "reasoning": "the token had expired",
            "replan_instruction": "re-authorise the Google account first",
        })

        assert wid in _pending_finalizer_instructions(store)
        store.apply("consume_finalizer_instruction", {"work_id": wid, "node_id": "nf1"},
                    actor="architect")
        assert wid not in _pending_finalizer_instructions(store)

        # Stamped, not deleted: the node keeps the record of what was decided.
        entry = store.load(wid).nodes["nf1"].payload["finalizer"]
        assert "re-authorise" in entry["instruction"] and entry["consumed_at"]

    def test_proceed_asks_the_architect_for_nothing(self, monkeypatch):
        """A verdict that carries no instruction must not drag its work object into the replan
        set — that would re-plan every goal on every successful step."""
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1",
             {"verdict": "proceed", "reasoning": "the deadline is confirmed"})

        assert wid not in _pending_finalizer_instructions(store)


class TestTheRollupDoesNotRaceTheArchitect:
    """A goal must not auto-complete while a verdict is still asking to change its plan.

    2026-09-17, observed live: a lights node was judged `amend` — the finalizer's own words were
    "the result does not show that the lights were actually turned off" — and `amend` maps to
    `closed`. Closing the only child triggered the rollup, the work object went `done`, and the
    instruction written for the architect became unreachable, because
    _pending_finalizer_instructions scans ACTIVE objects only. The judgment was right and the
    rollup overrode it: the goal reported success while the lights were still on.
    """

    def test_an_unconsumed_amend_holds_the_goal_open(self, monkeypatch):
        store = _store()
        wid = _wo_with_judged_node(store)          # a single top-level node, at `done`

        _run(monkeypatch, wid, "n1", {
            "verdict": "amend", "reasoning": "the result does not show the work happened",
            "amend_intent": "try the other route before calling this finished",
        })

        wo = store.load(wid)
        assert wo.nodes["n1"].status == "closed", "the node is still judged and closed"
        assert wo.status == "active", (
            "the goal completed while a verdict was still asking the architect to change it — "
            "the instruction is now unreachable")
        assert wid in _pending_finalizer_instructions(store)

    def test_it_completes_once_the_architect_has_consumed(self, monkeypatch):
        """The hold is temporary: it lasts until the instruction is acted on, not forever."""
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1", {
            "verdict": "amend", "reasoning": "changes the plan",
            "amend_intent": "do the other thing",
        })
        assert store.load(wid).status == "active"

        store.apply("consume_finalizer_instruction", {"work_id": wid, "node_id": "n1"},
                    actor="architect")
        # Any subsequent write re-runs the rollup; nothing is pending now.
        store.apply("edit_node", {"work_id": wid, "node_id": "n1", "title": "Research the deadline"})
        assert store.load(wid).status == "done"

    def test_proceed_still_completes_immediately(self, monkeypatch):
        """`proceed` asks for nothing, so it must not hold the goal open."""
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1", {"verdict": "proceed", "reasoning": "on plan"})

        assert store.load(wid).status == "done"

    def test_the_steward_can_still_close_it_explicitly(self, monkeypatch):
        """Only the automatic rollup defers. A person deciding the goal is over outranks a pending
        note about how to continue it."""
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1", {
            "verdict": "amend", "reasoning": "changes the plan", "amend_intent": "do X instead",
        })
        assert store.load(wid).status == "active"

        store.apply("set_work_status",
                    {"work_id": wid, "status": "done", "reason": "steward: objective met"},
                    actor="steward")
        assert store.load(wid).status == "done"


class TestACallCanReturnAndAchieveNothing:
    """BLOCKED on a call that RETURNED — the case the verdict set could not express.

    2026-09-17: a worker ran four pod searches and two Gmail searches for a school picture
    packet and returned an accurate account of finding none. The node sat at `done`, whose only
    exits were `closed` (which counts as SATISFYING the goal) and `superseded`. So the verdict
    became `amend` — continue — and the continuation it prescribed needed the user's own
    credentials, which sent a browser at the school's public contact form instead.
    """

    def test_blocked_fails_a_node_whose_call_returned(self, monkeypatch):
        store = _store()
        wid = _wo_with_judged_node(store)          # node sits at `done`

        _run(monkeypatch, wid, "n1", {
            "verdict": "blocked",
            "reasoning": "searched pods and mail thoroughly; the packet is not in anything we "
                         "can reach, and the remaining routes need the user's own account",
        })

        node = store.load(wid).nodes["n1"]
        assert node.status == "failed", "a returned call that achieved nothing is not satisfied"
        assert node.payload["finalizer"]["verdict"] == "blocked"

    def test_a_blocked_node_does_not_complete_its_goal(self, monkeypatch):
        """`closed` is what is_satisfied keys on. Blocked must not look like success."""
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1", {"verdict": "blocked", "reasoning": "needs the user"})

        assert store.load(wid).status == "active", "the goal cannot report done on a blocked step"

    def test_blocked_counts_as_an_attempt_that_did_not_land(self, monkeypatch):
        """It reaches the goal tally through `failed`, like replan does."""
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1", {"verdict": "blocked", "reasoning": "needs the user"})

        wo = store.load(wid)
        assert int((wo.nodes[wo.goal_node_id].payload or {}).get("goal_unmet_attempts") or 0) == 1

    def test_the_architect_sees_it_as_failed_work_to_plan_around(self, monkeypatch):
        """`failed` is the channel to the architect — work_repair retired into it. A blocked
        node has to be visible there, or asking the user never gets planned."""
        from app.assistant.control_nodes.work_architect_node import _render_existing_graph
        store = _store()
        wid = _wo_with_judged_node(store)

        _run(monkeypatch, wid, "n1", {
            "verdict": "blocked", "reasoning": "the packet needs the user's ParentSquare account",
        })

        rendered = _render_existing_graph(store.load(wid))
        assert "status=failed" in rendered
        assert "ParentSquare" in rendered, "the WHY has to travel with it"

    def test_blocked_on_an_already_failed_node_is_unchanged(self, monkeypatch):
        """The original case still behaves exactly as before."""
        store = _store()
        wid = _failed_wo(store)

        _run(monkeypatch, wid, "nf1", {"verdict": "blocked", "reasoning": "no route"})

        assert store.load(wid).nodes["nf1"].status == "failed"
