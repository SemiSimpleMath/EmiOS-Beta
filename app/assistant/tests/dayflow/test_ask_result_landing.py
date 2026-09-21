"""An ask is a tool call: the dispatch makes it, the recorder writes its result.

2026-09-16. The user declined the same picture-day work on 09-12 ("This is not important") and again
on 09-15 ("the picture was already taken so this is all moot"). Both declines reached the
steward, which correctly abandoned the work object both times. Neither ever reached the GRAPH: the
ticket lane minted its ticket by hand and returned nothing, so the response had to be reconstructed
later from the ticket store — and the steward, which runs first, had already made the object
terminal. `propagate_work_outcome` therefore found no user words, the concern stayed active, and a
fresh work object was minted the next morning. Four of them, four days running.

Now the ask is an ordinary dispatch: the gate claims the node, the session calls
`create_dayflow_ticket`, the tool blocks until the user answers or its window lapses, and the same
`record_tool_result` that lands a manager's result lands this one.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.assistant.utils.pydantic_classes import ToolResult


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Ask WO", constraints=None):
    wo = store.apply("create_work_object", {
        "title": title, "goal_content": title,
        "satisfied_when_kind": "all_owned_children_done",
        "constraints": constraints or {},
    })
    return wo.id, wo.goal_node_id


def _claimed_ask(store, wid, gid, node_id="ask1", content="Tell the user the plan."):
    """A node the dispatch gate has claimed, ready for its tool call."""
    store.apply("add_node", {"work_id": wid, "id": node_id, "type": "subtask", "parent_id": gid,
                             "title": "Give the user the plan", "content": content})
    store.apply("set_status", {"work_id": wid, "node_id": node_id, "status": "dispatched"},
                actor="dispatch_gate")
    return node_id


def _response(text, question="the picture-day plan…"):
    """What create_dayflow_ticket returns when the user answers."""
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
        format_response_result,
    )
    return ToolResult(result_type="ticket_response",
                      content=format_response_result(answer=text, question=question),
                      data={"action": "acknowledge", "user_text": text})


def _timeout(question="Are the dogs handled this morning?"):
    """What it returns when nobody answered."""
    from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
        format_expiry_result,
    )
    return ToolResult(result_type="ticket_response",
                      content=format_expiry_result(reason="no response within 3600s",
                                                   question=question),
                      data={"action": "timeout", "user_text": ""})


# ── 1. a response becomes the node's result ──────────────────────────────────


class TestResponseIsTheResult:

    def test_decline_lands_on_the_node(self):
        from work_objects.result_recorder import record_tool_result
        store = _store()
        wid, gid = _mk_wo(store)
        _claimed_ask(store, wid, gid)

        assert record_tool_result(
            store, wid, "ask1",
            _response("the picture was already taken so this is all moot."),
            actor="ask", evidence_title="user response") is True

        wo = store.load(wid)
        assert wo.nodes["ask1"].status == "done"      # a result exists; the finalizer judges it
        evidence = [n for n in wo.nodes.values() if n.parent_id == "ask1" and n.type == "evidence"]
        assert len(evidence) == 1
        content = evidence[0].content
        assert "this is all moot" in content
        assert "picture-day plan" in content          # the question rides along
        # The answer comes FIRST: a projection caps joined evidence, and the user's own words are
        # the half that has to survive the cap.
        assert content.index("moot") < content.index("picture-day plan")

    def test_expiry_is_a_result_not_a_failure(self):
        """The message went out and the user was not reached. That is something that HAPPENED,
        so the finalizer judges it and decides whether to ask again — it is not repair's."""
        from work_objects.result_recorder import record_tool_result
        store = _store()
        wid, gid = _mk_wo(store)
        _claimed_ask(store, wid, gid)

        record_tool_result(store, wid, "ask1", _timeout(), actor="ask",
                           evidence_title="user response")

        wo = store.load(wid)
        assert wo.nodes["ask1"].status == "done"
        evidence = [n for n in wo.nodes.values() if n.parent_id == "ask1" and n.type == "evidence"]
        assert "user not reached" in evidence[0].content
        assert "Are the dogs handled" in evidence[0].content

    def test_an_outcome_already_on_the_graph_is_never_overwritten(self):
        from work_objects.result_recorder import record_tool_result
        store = _store()
        wid, gid = _mk_wo(store)
        _claimed_ask(store, wid, gid)

        assert record_tool_result(store, wid, "ask1", _response("First answer."),
                                  actor="ask") is True
        assert record_tool_result(store, wid, "ask1", _response("Second answer."),
                                  actor="ask") is False
        evidence = [n for n in store.load(wid).nodes.values()
                    if n.parent_id == "ask1" and n.type == "evidence"]
        assert len(evidence) == 1 and "First answer" in evidence[0].content

    def test_an_empty_result_fails_and_says_why(self):
        """A tool that returned NOTHING is closer to "it did not run" than to "here is the
        outcome". It fails — and it fails WITH a reason, because a blocked goal whose WHY/RESULT
        renders blank tells repair and the steward that something broke and nothing about what."""
        from work_objects.result_recorder import record_tool_result
        from app.assistant.dayflow_orchestrator.work_portfolio import node_result
        store = _store()
        wid, gid = _mk_wo(store)
        _claimed_ask(store, wid, gid)

        record_tool_result(store, wid, "ask1",
                           ToolResult(result_type="success", content="", data={}), actor="ask")

        wo = store.load(wid)
        assert wo.nodes["ask1"].status == "failed"
        why = node_result(wo, wo.nodes["ask1"])
        assert why, "a failed node must never render a blank WHY/RESULT"
        assert "returned no result" in why

    def test_the_judged_result_is_not_truncated(self):
        """The finalizer decides what a result MEANS by reading it, and a failure's WHY is the only
        account of why a goal is blocked. Neither may arrive clipped — this was capped at 400
        characters for every reader while the finalizer's contract promised the full result."""
        from work_objects.result_recorder import record_tool_result
        from app.assistant.dayflow_orchestrator.work_portfolio import node_result
        store = _store()
        wid, gid = _mk_wo(store)
        _claimed_ask(store, wid, gid)
        long_answer = "The school confirmed the packet details. " + ("x" * 2000)

        record_tool_result(store, wid, "ask1",
                           ToolResult(result_type="success", content=long_answer, data={}),
                           actor="ask")

        wo = store.load(wid)
        assert node_result(wo, wo.nodes["ask1"]) == long_answer
        # Volume-sensitive renders still cap explicitly, at the call site rather than for everyone.
        assert len(node_result(wo, wo.nodes["ask1"], limit=400)) == 400

    def test_a_tool_error_is_repairs_lane(self):
        """The TOOL saying it could not run is different from a result the finalizer must read."""
        from work_objects.result_recorder import record_tool_result
        store = _store()
        wid, gid = _mk_wo(store)
        _claimed_ask(store, wid, gid)

        record_tool_result(store, wid, "ask1",
                           ToolResult(result_type="error", content="ticket system unavailable",
                                      data={}), actor="ask")
        assert store.load(wid).nodes["ask1"].status == "failed"


# ── 2. the dispatch builds a real tool call ──────────────────────────────────


class TestAskIsDispatchedAsAToolCall:

    def test_the_tool_is_called_with_the_node_ref_and_one_timeout(self, monkeypatch):
        import app.assistant.dayflow_orchestrator.node_dispatch as nd
        from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import (
            CreateDayflowTicketTool,
        )
        store = _store()
        wid, gid = _mk_wo(store)
        _claimed_ask(store, wid, gid, content="Tell the user the October 9 deadline.")

        calls = []
        monkeypatch.setattr(CreateDayflowTicketTool, "execute",
                            lambda self, tm: calls.append(tm) or _response("ok"))
        nd.surface_and_await(store, wid, "ask1", store.load(wid).nodes["ask1"])

        args = calls[0].tool_data["arguments"]
        assert args["trigger_context"]["work_node"] == f"{wid}::ask1"
        assert "October 9 deadline" in args["ticket_brief"]
        # The ticket's validity window IS the call's timeout — one clock, not two.
        assert args["wait_timeout_seconds"] == args["valid_hours"] * 3600


# ── 3. a session waiting on a person is not a frozen job ─────────────────────


class TestWaitingIsNotFrozen:

    def test_the_frozen_timeout_outlasts_the_longest_tool_call(self):
        """A session blocked in a tool writes nothing meanwhile, so silence cannot be the signal
        that a job died — the frozen timeout has to outlast the slowest legitimate call. The
        slowest is the ask, which waits out the ticket's whole validity window. Deriving the one
        from the other is what stops an ask being reaped mid-question if either number moves.

        (Crashes and restarts are caught by ORPHAN detection instead — no live thread — which is
        immediate and does not wait for this timeout.)"""
        from app.assistant.dayflow_orchestrator import dispatch_sweeper as ds
        from app.assistant.dayflow_orchestrator.node_dispatch import _ASK_TIMEOUT_HOURS

        assert ds._WORK_NODE_FROZEN_TIMEOUT_S > _ASK_TIMEOUT_HOURS * 3600


# ── 4. the regression: a recorded decline reaches the concern ────────────────


class TestDeclineReachesTheConcern:

    def _register(self, tmp_path, concern_id):
        path = tmp_path / "resource_concerns_register.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "active": [{"concern_id": concern_id, "title": "Picture Day needs preparation",
                        "reinforcement_count": 5}],
            "addressing": [], "resolved": [], "dormant": [],
        }), encoding="utf-8")
        return path

    def test_declined_work_parks_its_concern_dormant(self, monkeypatch, tmp_path):
        """The whole chain: tool result -> graph -> steward abandon -> back-propagation -> the
        concern stops being active. Before the fix this journaled "(no user words recorded)" and
        left the concern in `active`, where the next morning's steward turned it straight back
        into work."""
        from work_objects.result_recorder import record_tool_result
        from app.assistant.subconscious import concern_feedback, persist
        import app.assistant.subconscious.answer_capture as answer_capture

        concern_id = "3a9f2c71-6b84-4d15-ae32-91f0c7b5e628"
        register_path = self._register(tmp_path, concern_id)
        monkeypatch.setattr(persist, "get_repo_root", lambda: tmp_path)
        monkeypatch.setattr(persist, "_REGISTER_REL", register_path.name)
        monkeypatch.setattr(answer_capture, "trigger_noticer", lambda reason=None: None)

        store = _store()
        wid, gid = _mk_wo(store, constraints={"concern_refs": [f"concern:{concern_id[:8]}"]})
        _claimed_ask(store, wid, gid)

        record_tool_result(
            store, wid, "ask1",
            ToolResult(result_type="ticket_response", content="No thanks", data={
                "ticket_id": "ticket-decline", "action": "answer", "user_text": "No thanks",
                "response_details": {"label": "No thanks", "meaning": "decline", "typed_text": "",
                                     "scope": "Continue this preparation task"}}),
            actor="create_dayflow_ticket", evidence_title="user response")
        store.apply("set_work_status", {"work_id": wid, "status": "abandoned",
                                        "reason": "steward: objective dropped"}, actor="steward")
        concern_feedback.propagate_work_outcome(store, wid, "abandoned")

        register = json.loads(register_path.read_text(encoding="utf-8"))
        assert register["active"] == []
        assert [c["concern_id"] for c in register["dormant"]] == [concern_id]
        parked = register["dormant"][0]
        assert parked.get("user_declined_at_utc")
        assert "No thanks" in parked["reinforcement_notes"]
