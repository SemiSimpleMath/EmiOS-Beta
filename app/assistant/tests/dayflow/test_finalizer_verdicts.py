"""The work finalizer: what it judges, and what each verdict writes to the graph.

One agent call per dispatch, on the ONE node that dispatch ran, and the verdict is written to
emi.db in the same control node. The verdict answers one question — was the node's goal achieved —
and the tool's own status (returned / reported failure) is input to that judgment, never a limit on
it. Every verdict carries `outcome` prose for the planner; every verdict except `achieved` carries a
recommendation with a route the architect acts on next tick.

Two things are guarded hardest here, because both were broken by tests that passed:
  * the handoff to the architect crosses a tick boundary and must live on the NODE;
  * the prompt the finalizer actually receives must not narrow its verdicts by tool status —
    which it did, in three places the previous tests never rendered.
"""
from __future__ import annotations

import pytest

from app.assistant.control_nodes.work_architect_node import (
    _finalizer_block, _pending_finalizer_instructions, _render_existing_graph,
)
from app.assistant.control_nodes.work_finalizer_node import (
    WorkFinalizerNode, _REPEAT_FAILURE_LIMIT,
)
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _returned_wo(store, title="Finalizer WO"):
    """A top-level node whose call RETURNED — sitting at `done`, awaiting judgment."""
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "n1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Research the deadline"})
    for st in ("actionable", "dispatched", "done"):
        store.apply("set_status", {"work_id": wo.id, "node_id": "n1", "status": st})
    return wo.id


def _errored_wo(store, title="Errored WO"):
    """A top-level node whose TOOL reported it could not run — sitting at `failed`."""
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "nf1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Call that errored"})
    for st in ("actionable", "dispatched", "failed"):
        store.apply("set_status", {"work_id": wo.id, "node_id": "nf1", "status": st})
    return wo.id


def _run(monkeypatch, work_id, node_id, verdict_data):
    """Drive the finalizer over one node with the agent call stubbed to `verdict_data`."""
    monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)
    monkeypatch.setattr(WorkFinalizerNode, "_judge", lambda self, wo, node, scope: verdict_data)
    bb = FakeBlackboard({"work_node_ref": f"{work_id}::{node_id}"})
    WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                      agent_registry={}, tool_registry={}).action_handler(message=None)
    return bb


def _capture_prompt(monkeypatch, work_id, node_id, reply):
    """Run the REAL _judge with a spy in place of the agent; return the `information` it received."""
    seen = {}
    monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)
    import app.assistant.control_nodes.work_finalizer_node as mod

    class _Spy:
        def action_handler(_s, message):
            seen["information"] = message.information
            seen["task"] = message.task
            class _R: data = reply
            return _R()

    monkeypatch.setattr(mod.DI, "agent_factory",
                        type("F", (), {"create_agent": staticmethod(lambda *a, **k: _Spy())})())
    bb = FakeBlackboard({"work_node_ref": f"{work_id}::{node_id}"})
    WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                      agent_registry={}, tool_registry={}).action_handler(message=None)
    return seen


ACHIEVED = {"verdict": "achieved", "outcome": "the deadline is confirmed as October 9"}
PLAN_CHANGES = {"verdict": "achieved_plan_changes",
                "outcome": "the unit is under warranty, so a contractor is the wrong route",
                "recommendation": "drop finding a contractor; contact the manufacturer instead"}
RETRY = {"verdict": "retry", "outcome": "the account token had expired before the lookup ran",
         "recommendation": "re-authorise the account first, then the same lookup works"}
STOP = {"verdict": "unrecoverable", "next_step": "stop",
        "outcome": "the user replied 'drop it' — this line is not wanted",
        "recommendation": "stop this branch"}
NEW_APPROACH = {"verdict": "unrecoverable", "next_step": "new_approach",
                "outcome": "the public site has no such page; scraping it cannot work",
                "recommendation": "ask the vendor's API instead of the website"}
ASK = {"verdict": "unrecoverable", "next_step": "ask_user",
       "outcome": "searched pods and mail thoroughly; the packet is in nothing we can reach",
       "recommendation": "the user holds the school account",
       "question_for_user": "Did the picture-day packet arrive through ParentSquare?"}


class TestWhatGetsJudged:

    def test_it_judges_only_the_node_that_ran(self, monkeypatch):
        store = _store()
        mine = _returned_wo(store, title="The one that ran")
        other = store.apply("create_work_object", {"title": "Someone else's finished node",
                                                   "goal_content": "other",
                                                   "satisfied_when_kind": "all_owned_children_done"})
        store.apply("add_node", {"work_id": other.id, "id": "other_n1", "type": "subtask",
                                 "parent_id": other.goal_node_id, "title": "Not this pass's business"})
        for st in ("actionable", "dispatched", "done"):
            store.apply("set_status", {"work_id": other.id, "node_id": "other_n1", "status": st})

        _run(monkeypatch, mine, "n1", ACHIEVED)

        assert store.load(mine).nodes["n1"].status == "closed"
        assert store.load(other.id).nodes["other_n1"].status == "done", "untouched"

    def test_a_pass_that_ran_nothing_judges_nothing(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        monkeypatch.setattr(WorkFinalizerNode, "_scope", lambda self, message: None)
        bb = FakeBlackboard()
        WorkFinalizerNode(name="work_finalizer_node", blackboard=bb,
                          agent_registry={}, tool_registry={}).action_handler(message=None)
        assert bb.get_state_value("work_finalizer_result", []) == []
        assert store.load(wid).nodes["n1"].status == "done"

    def test_an_errored_node_IS_judged(self, monkeypatch):
        """The result text is the only account of the failure MODE, and the mode decides."""
        store = _store()
        wid = _errored_wo(store)
        seen = _capture_prompt(monkeypatch, wid, "nf1", ASK)
        assert "nf1" in seen["information"]

    def test_a_worker_internal_node_is_not_judged(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        store.apply("add_node", {"work_id": wid, "id": "n1_child", "type": "subtask",
                                 "parent_id": "n1", "title": "the worker's own step"})
        for st in ("actionable", "dispatched", "done"):
            store.apply("set_status", {"work_id": wid, "node_id": "n1_child", "status": st})
        _run(monkeypatch, wid, "n1_child", ACHIEVED)
        assert store.load(wid).nodes["n1_child"].status == "done", "not the finalizer's to close"


class TestThePromptDoesNotNarrowTheVerdict:
    """The bug the previous tests could not see, because every one of them stubbed _judge.

    The system prompt said 'judge the goal'; the per-call injection, the user prompt, and the
    schema all said a returned call gets proceed or amend. Three-to-one, and the three were nearer
    the decision. So this renders the REAL per-call text and asserts it names no verdict at all.
    """

    def test_a_returned_call_is_not_told_which_verdicts_it_may_give(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        info = _capture_prompt(monkeypatch, wid, "n1", ACHIEVED)["information"]
        assert "the call returned" in info
        for word in ("proceed", "amend", "'achieved'", "Your verdict is"):
            assert word not in info, f"the per-call text narrows the verdict: {word!r}"

    def test_an_errored_call_is_not_told_either(self, monkeypatch):
        store = _store()
        wid = _errored_wo(store)
        info = _capture_prompt(monkeypatch, wid, "nf1", ASK)["information"]
        assert "could not run" in info
        for word in ("replan", "blocked", "'retry'", "Your verdict is"):
            assert word not in info, f"the per-call text narrows the verdict: {word!r}"

    def test_the_user_prompt_and_schema_carry_no_status_based_limit(self):
        from pathlib import Path
        import app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form as form_mod
        root = Path(form_mod.__file__).parent
        user = (root / "prompts" / "user.j2").read_text(encoding="utf-8")
        for stale in ("proceed / amend", "(proceed", "AMEND"):
            assert stale not in user
        schema = "\n".join(f.description or "" for f in form_mod.AgentForm.model_fields.values())
        assert "the call FAILED" not in schema, "the schema ties a verdict to tool status"


class TestTheVerdictsWrite:

    def test_achieved_closes_the_node_and_completes_the_goal(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", ACHIEVED)
        wo = store.load(wid)
        assert wo.nodes["n1"].status == "closed"
        assert wo.status == "done"
        assert wo.nodes["n1"].payload["finalizer"]["outcome"] == ACHIEVED["outcome"]
        assert wo.nodes["n1"].payload["finalizer"]["next_step"] == ""

    def test_plan_changes_closes_the_node_and_holds_the_goal_for_the_architect(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", PLAN_CHANGES)
        wo = store.load(wid)
        assert wo.nodes["n1"].status == "closed"
        assert wo.status == "active", "held until the architect consumes the recommendation"
        fin = wo.nodes["n1"].payload["finalizer"]
        assert fin["next_step"] == "plan_changes"
        assert fin["recommendation"] == PLAN_CHANGES["recommendation"]

    def test_retry_on_a_returned_call_counts_the_attempt_and_reopens_the_node(self, monkeypatch):
        """The case that used to raise 'illegal transition done->proposed' and be swallowed."""
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", RETRY)
        wo = store.load(wid)
        assert wo.nodes["n1"].status == "proposed", "back in the architect's inbox"
        assert wo.nodes["n1"].payload["failure_count"] == 1, "it passed through failed"
        assert wo.nodes[wo.goal_node_id].payload["goal_unmet_attempts"] == 1
        assert wo.nodes["n1"].payload["finalizer"]["next_step"] == "retry"

    def test_retry_on_an_errored_call_reopens_it_too(self, monkeypatch):
        store = _store()
        wid = _errored_wo(store)
        _run(monkeypatch, wid, "nf1", RETRY)
        assert store.load(wid).nodes["nf1"].status == "proposed"

    @pytest.mark.parametrize("data", [STOP, NEW_APPROACH, ASK], ids=["stop", "new_approach", "ask_user"])
    def test_unrecoverable_fails_a_returned_call_and_carries_its_route(self, monkeypatch, data):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", data)
        wo = store.load(wid)
        assert wo.nodes["n1"].status == "failed", "a returned call that achieved nothing is not satisfied"
        assert wo.status == "active"
        fin = wo.nodes["n1"].payload["finalizer"]
        assert fin["next_step"] == data["next_step"]
        assert fin["outcome"] == data["outcome"]
        if data is ASK:
            assert fin["question_for_user"] == ASK["question_for_user"]

    def test_unrecoverable_on_an_already_failed_node_records_without_double_counting(self, monkeypatch):
        store = _store()
        wid = _errored_wo(store)                       # failed once already
        _run(monkeypatch, wid, "nf1", ASK)
        wo = store.load(wid)
        assert wo.nodes["nf1"].status == "failed"
        assert wo.nodes["nf1"].payload["failure_count"] == 1
        assert wo.nodes["nf1"].payload["finalizer"]["question_for_user"]

    def test_it_never_ends_a_work_object(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        store.apply("add_node", {"work_id": wid, "id": "n2", "type": "subtask",
                                 "parent_id": store.load(wid).goal_node_id, "title": "Still to do"})
        _run(monkeypatch, wid, "n1", STOP)
        wo = store.load(wid)
        assert wo.status == "active", "only the steward ends a work object"
        assert wo.nodes["n2"].status == "proposed"

    def test_an_unusable_verdict_is_refused_loudly(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        bb = _run(monkeypatch, wid, "n1", {"verdict": "blocked", "outcome": "retired verdict"})
        assert store.load(wid).nodes["n1"].status == "done"
        assert bb.get_state_value("work_finalizer_result") == []


class TestTheContract:

    def test_outcome_is_required_on_every_verdict(self):
        from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm
        with pytest.raises(ValueError, match="outcome is required"):
            AgentForm(verdict="achieved", outcome="")

    def test_recommendation_is_required_unless_achieved(self):
        from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm
        AgentForm(verdict="achieved", outcome="done")
        for v in ("achieved_plan_changes", "retry"):
            with pytest.raises(ValueError, match="recommendation is required"):
                AgentForm(verdict=v, outcome="x")

    def test_unrecoverable_needs_a_typed_next_step(self):
        from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm
        with pytest.raises(ValueError, match="next_step must be one of"):
            AgentForm(verdict="unrecoverable", outcome="x", recommendation="y")
        with pytest.raises(ValueError, match="question_for_user is required"):
            AgentForm(verdict="unrecoverable", next_step="ask_user", outcome="x", recommendation="y")
        assert AgentForm(**ASK).next_step == "ask_user"

    def test_next_step_is_refused_on_other_verdicts(self):
        from app.assistant.agents.dayflow_orchestrator.work_finalizer.agent_form import AgentForm
        with pytest.raises(ValueError, match="only for verdict='unrecoverable'"):
            AgentForm(verdict="retry", next_step="stop", outcome="x", recommendation="y")


class TestRepeatedFailureIsEscalated:
    """Once a goal has accumulated the limit of attempts that did not achieve it, the runtime — not
    the finalizer — decides: the user is asked, and the same thing is not tried again."""

    def _exhaust(self, store, wid, node_id):
        for _ in range(_REPEAT_FAILURE_LIMIT - 1):
            store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "dispatched"})
            wo = store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "failed"})
            store.apply("finalize_task", {"work_id": wid, "node_id": node_id,
                "expected_dispatch_epoch": wo.nodes[node_id].payload["dispatch_epoch"],
                "finalizer": {"verdict": "retry", "outcome": "Attempt did not achieve the goal", "next_step": "retry"}})

    def test_a_retry_at_the_limit_becomes_ask_user_and_does_not_reopen(self, monkeypatch):
        store = _store()
        wid = _errored_wo(store)                       # attempt 1
        self._exhaust(store, wid, "nf1")               # attempts up to limit-1 ... this verdict is the limit-th
        # Re-dispatch so the finalizer sees a fresh result to judge.
        store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "dispatched"})
        store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "done"})

        _run(monkeypatch, wid, "nf1", RETRY)

        wo = store.load(wid)
        assert wo.nodes["nf1"].status == "failed", "not re-opened — the same thing is not tried again"
        fin = wo.nodes["nf1"].payload["finalizer"]
        assert fin["next_step"] == "ask_user"
        assert fin["escalated"] is True
        assert fin["question_for_user"], "an escalation carries a question, taken from the recommendation"

    def test_below_the_limit_a_retry_is_honoured(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", RETRY)
        fin = store.load(wid).nodes["n1"].payload["finalizer"]
        assert fin["next_step"] == "retry" and fin["escalated"] is False

    def test_the_finalizer_is_told_the_goal_keeps_failing(self, monkeypatch):
        store = _store()
        wid = _errored_wo(store)
        for _ in range(_REPEAT_FAILURE_LIMIT):
            store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "dispatched"})
            wo = store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "failed"})
            store.apply("finalize_task", {"work_id": wid, "node_id": "nf1",
                "expected_dispatch_epoch": wo.nodes["nf1"].payload["dispatch_epoch"],
                "finalizer": {"verdict": "retry", "outcome": "Attempt did not achieve the goal", "next_step": "retry"}})
        store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "dispatched"})
        store.apply("set_status", {"work_id": wid, "node_id": "nf1", "status": "failed"})
        info = _capture_prompt(monkeypatch, wid, "nf1", ASK)["information"]
        assert "HAVE NOT ACHIEVED THIS GOAL" in info
        assert "question_for_user" in info

    def test_a_first_failure_gets_no_such_warning(self, monkeypatch):
        store = _store()
        wid = _errored_wo(store)
        info = _capture_prompt(monkeypatch, wid, "nf1", ASK)["information"]
        assert "HAVE NOT ACHIEVED THIS GOAL" not in info

    def test_the_steward_sees_the_verdict_on_a_failed_node(self, monkeypatch):
        """Before: the steward saw the worker's evidence and a bare 'failed'. The finalizer's own
        judgment — the part that says 'this needs the user' — reached nobody."""
        from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", ASK)
        rendered = render_work_portfolio(store.load(wid))
        assert "FINALIZER: unrecoverable" in rendered
        assert "NEXT: ask_user" in rendered
        assert ASK["question_for_user"] in rendered


class TestTheHandoffToTheArchitect:
    """The recommendation has to outlive the dispatch that produced it, on the node."""

    @pytest.mark.parametrize("data,route", [(PLAN_CHANGES, "plan_changes"), (RETRY, "retry"),
                                            (STOP, "stop"), (NEW_APPROACH, "new_approach"), (ASK, "ask_user")])
    def test_every_non_achieved_verdict_reaches_the_architect(self, monkeypatch, data, route):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", data)

        pending = _pending_finalizer_instructions(store)        # THE NEXT TICK: fresh everything
        assert wid in pending, f"{route} did not outlive its dispatch"
        entry = pending[wid][0]
        assert entry["node_id"] == "n1" and entry["next_step"] == route
        assert entry["outcome"] == data["outcome"]

        block, licence = _finalizer_block(pending[wid])
        assert "n1" in block and data["outcome"] in block
        assert licence, "a recommendation is the prune licence"
        if route == "ask_user":
            assert ASK["question_for_user"] in block and "exactly ONE node" in block

    def test_achieved_asks_the_architect_for_nothing(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", ACHIEVED)
        assert wid not in _pending_finalizer_instructions(store)

    def test_a_recommendation_is_read_once(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", RETRY)
        assert wid in _pending_finalizer_instructions(store)
        store.apply("consume_finalizer_instruction", {"work_id": wid, "node_id": "n1", "expected_finalizer": store.load(wid).nodes["n1"].payload["finalizer"]}, actor="architect")
        assert wid not in _pending_finalizer_instructions(store)
        assert store.load(wid).nodes["n1"].payload["finalizer"]["consumed_at"]

    def test_a_failed_node_shows_its_outcome_in_the_replan_view(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", ASK)
        rendered = _render_existing_graph(store.load(wid))
        assert "[failed] n1" in rendered
        assert "NEXT: ask_user" in rendered and ASK["outcome"] in rendered


class TestTheRollupDoesNotRaceTheArchitect:

    def test_a_pending_plan_change_holds_the_goal_open(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", PLAN_CHANGES)
        assert store.load(wid).nodes["n1"].status == "closed"
        assert store.load(wid).status == "active"

    def test_it_completes_once_consumed(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", PLAN_CHANGES)
        store.apply("consume_finalizer_instruction", {"work_id": wid, "node_id": "n1", "expected_finalizer": store.load(wid).nodes["n1"].payload["finalizer"]}, actor="architect")
        store.apply("edit_node", {"work_id": wid, "node_id": "n1", "title": "Research the deadline"})
        assert store.load(wid).status == "done"

    def test_achieved_completes_immediately(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", ACHIEVED)
        assert store.load(wid).status == "done"

    def test_the_steward_can_still_close_it_explicitly(self, monkeypatch):
        store = _store()
        wid = _returned_wo(store)
        _run(monkeypatch, wid, "n1", PLAN_CHANGES)
        assert store.load(wid).status == "active"
        store.apply("set_work_status", {"work_id": wid, "status": "done", "reason": "steward: met"},
                    actor="steward")
        assert store.load(wid).status == "done"
