"""The architect owns a work object from creation to end; the steward only asks (2026-09-30).

2026-09-30: the finalizer judged the 7:00 thermostat work unrecoverable and asked for the owner; the
architect planned the question for 10:20; the steward abandoned the work at 7:20 because 7:00 had
passed, and the abandon cancelled the question. Pins: the steward's end request is recorded, not
applied; the architect ends a goal or revises its objective in its own revision, with the reason; a
revision that also acts on instructions keeps both; a stranded goal (nothing in it can run) is found,
once per state; and the architect's schema refuses contradictory goal changes. Invented data only.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from work_objects.store import WorkStore
from app.assistant.control_nodes.work_architect_node import stranded_fingerprint
from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
from app.assistant.dayflow_orchestrator.work_persist import persist_steward_output

CONCERN = "c1c1c1c1-0000-4000-8000-000000000001"


@pytest.fixture
def store(tmp_path):
    s = WorkStore(str(tmp_path / "work.db"))
    yield s
    s.close()


def goal(store, *, refs=(), tasks=("set",)):
    wo = store.apply("create_work_object", {
        "title": "Set the thermostat to 75F at 7:00", "goal_content": "Set the thermostat to 75F at 7:00",
        "satisfied_when_kind": "all_owned_children_done",
        "constraints": {"objective": "Set the thermostat to 75F at 7:00", "concern_refs": list(refs)}})
    for t in tasks:
        store.apply("add_node", {"work_id": wo.id, "id": t, "type": "subtask", "parent_id": wo.goal_node_id,
                                 "title": t})
    return store.load(wo.id)


def test_the_steward_asks_and_the_work_stays_open(store):
    wo = goal(store)
    out = persist_steward_output(store, {"end_requests": [{"work_id": wo.id, "reason": "duplicate of work_b"}]})
    assert out["end_requested"] == [wo.id]
    wo = store.load(wo.id)
    assert wo.status == "active"
    [entry] = wo.constraints["architect_instructions"]
    assert (entry["kind"], entry["reason"], entry["from"]) == ("end", "duplicate of work_b", "steward")
    with pytest.raises(ValueError, match="work_id and a reason"):
        persist_steward_output(store, {"end_requests": [{"work_id": wo.id, "reason": ""}]})


def test_the_architect_ends_the_goal_with_its_reason_and_the_concern_hears(store):
    wo = goal(store, refs=[f"concern:{CONCERN}"])
    store.acknowledge_concern_feedback(store.pending_concern_feedback()[0]["id"])   # the attachment
    persist_steward_output(store, {"end_requests": [{"work_id": wo.id, "reason": "superseded by new intake"}]})
    wo = store.load(wo.id)
    res = apply_architect_dag(store, wo.id, [], goal_instructions=wo.constraints["architect_instructions"],
                              end_goal={"status": "abandoned", "reason": "the owner set it by hand"},
                              expected_updated_at=wo.updated_at)
    assert res["ended"] == "abandoned"
    wo = store.load(wo.id)
    assert wo.status == "abandoned"
    assert wo.constraints["terminal"]["reason"] == "architect: the owner set it by hand"
    assert wo.nodes["set"].status == "abandoned"                 # the cascade still runs
    assert all(e.get("consumed_at") for e in wo.constraints["architect_instructions"])
    assert [r["outcome"] for r in store.pending_concern_feedback()] == ["abandoned"]


def test_a_revised_objective_keeps_the_instructions_and_review_it_acted_on(store):
    wo = goal(store)
    persist_steward_output(store, {"end_requests": [{"work_id": wo.id, "reason": "7:00 has passed"}]})
    wo = store.load(wo.id)
    res = apply_architect_dag(
        store, wo.id, [{"node_id": "ask", "title": "Ask about access",
                        "detail": "Ask the user whether thermostat access is linked"}],
        goal_instructions=wo.constraints["architect_instructions"], review_fingerprint="set:proposed",
        revise_objective={"objective": "Resolve thermostat access with the owner", "success_criteria": "",
                          "reason": "7:00 passed; the finalizer's question still stands"},
        expected_updated_at=wo.updated_at)
    assert res["revised"] == "Resolve thermostat access with the owner"
    wo = store.load(wo.id)
    assert wo.status == "active" and wo.title == "Resolve thermostat access with the owner"
    [revision] = wo.constraints["objective_revisions"]
    assert revision["from"] == "Set the thermostat to 75F at 7:00" and "question" in revision["reason"]
    assert all(e.get("consumed_at") for e in wo.constraints["architect_instructions"])
    assert wo.constraints["stranded_review"]["fingerprint"] == "set:proposed"
    assert "ask--" + wo.id.split("_")[-1][:6] in wo.nodes


def test_a_goal_is_stranded_only_when_nothing_in_it_can_run(store):
    wo = goal(store, tasks=("set", "verify"))
    store.apply("add_edge", {"work_id": wo.id, "src": "set", "dst": "verify", "relation": "depends_on"})
    assert stranded_fingerprint(store.load(wo.id)) is None            # "set" can run
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    store.apply("defer_node", {"work_id": wo.id, "node_id": "set", "wake_kind": "time", "wake_at": later})
    assert stranded_fingerprint(store.load(wo.id)) is None            # a time lies ahead
    store.apply("set_status", {"work_id": wo.id, "node_id": "set", "status": "abandoned",
                               "reason": "the 7:00 window passed", "licensed": True})
    fingerprint = stranded_fingerprint(store.load(wo.id))              # "verify" waits on a dead step
    assert fingerprint == "set:abandoned|verify:proposed"
    wo = store.load(wo.id)
    apply_architect_dag(store, wo.id, [], review_fingerprint=fingerprint, expected_updated_at=wo.updated_at)
    assert stranded_fingerprint(store.load(wo.id)) is None            # reviewed in this state


def test_the_architect_schema_refuses_contradictory_goal_changes():
    from app.assistant.agents.dayflow_orchestrator.work_architect.agent_form import AgentForm
    end = {"status": "done", "reason": "reached"}
    revise = {"objective": "x", "reason": "y"}
    with pytest.raises(ValidationError, match="either ends or continues"):
        AgentForm(architect_summary="s", end_goal=end, revise_objective=revise)
    with pytest.raises(ValidationError, match="adds no work"):
        AgentForm(architect_summary="s", end_goal=end,
                  nodes=[{"node_id": "a", "title": "a", "detail": "a"}])
    assert AgentForm(architect_summary="s", end_goal=end).end_goal.status == "done"


def test_an_ended_goal_takes_no_instruction(store):
    wo = goal(store)
    apply_architect_dag(store, wo.id, [], end_goal={"status": "done", "reason": "reached"},
                        expected_updated_at=wo.updated_at)
    with pytest.raises(ValueError, match="already done"):
        store.apply("instruct_goal", {"work_id": wo.id, "kind": "end", "reason": "late"}, actor="steward")


def test_the_steward_form_has_no_closing_lists():
    from app.assistant.agents.dayflow_orchestrator.strategic_planner_wo.agent_form import AgentForm
    fields = set(AgentForm.model_fields)
    assert "end_requests" in fields and not {"complete_work_ids", "abandon_work_ids"} & fields
