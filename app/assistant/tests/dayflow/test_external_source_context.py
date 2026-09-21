"""External source identity must survive intake, wake, replan, and rendered task views."""
from pathlib import Path
import pytest
from jinja2 import Environment, FileSystemLoader, ChainableUndefined
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from app.assistant.control_nodes.state_mover_prep_node import StateMoverPrepNode
from app.assistant.control_nodes.state_mover_persist_node import StateMoverPersistNode
from app.assistant.dayflow_orchestrator.work_context import worker_data, render_view
from app.assistant.dayflow_orchestrator.work_persist import persist_steward_output
from app.assistant.dayflow_orchestrator.work_portfolio import node_result
from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store


def email(item_id, text):
    return {"metadata": {"item_id": item_id, "source_type": "email",
        "email_sender": "approver@example.test", "email_subject": "Appliance selection",
        "created_at": "2026-09-20T15:00:00+00:00", "email_summary": "Selection discussion",
        "email_body_excerpt": text, "pod_id": "datapod:email:" + item_id,
        "linked_email_unified_id": "message-" + item_id, "email_thread_id": "thread-selection"}}


def scenario():
    store = get_dayflow_work_store()
    result = persist_steward_output(store, {"new_or_changed": [{
        "objective": "Research appliances and relay the approved choice", "based_on": ["request"]}]},
        admitted_artifacts=[email("request", "Compare three appliances.")])
    wid = result["created"][0]["work_id"]
    wo = store.load(wid)
    for nid in ("research", "relay"):
        store.apply("add_node", {"work_id": wid, "id": nid, "parent_id": wo.goal_node_id,
            "type": "subtask", "title": nid, "content": "Send the approved choice" if nid == "relay" else "Compare options"})
    store.apply("defer_node", {"work_id": wid, "node_id": "relay", "wake_kind": "event",
                               "wake_ref": "Approver explicitly approves one appliance"})
    return store, wid


def prepare(wid, items):
    bb = FakeBlackboard()
    StateMoverPrepNode(name="prep", blackboard=bb, agent_registry={}, tool_registry={})._build_work_object_waits(items)
    bb.update_state_value("node_wakes", [{"task_id": wid + "::relay", "source_item_id": "approval",
        "evidence": "The reply explicitly approves option B", "pod_id": "forged-model-reference"}])
    return bb


def persist(bb):
    StateMoverPersistNode(name="persist", blackboard=bb, agent_registry={}, tool_registry={})._apply_node_wakes()


def test_source_visible_through_wake_takeover_replan_and_finalizer():
    store, wid = scenario()
    bb = prepare(wid, [email("approval", "Yes, choose option B.")])
    persist(bb)
    wo = store.load(wid)
    assert wo.nodes["relay"].content == "Send the approved choice"
    assert wo.nodes["relay"].wake_kind is None
    receipt, = wo.provenance_for("relay")
    assert receipt.pod_ref == "datapod:email:approval"
    assert receipt.payload["external_source"]["message_id"] == "message-approval"
    assert not wo.is_work_unit(receipt)
    assert node_result(wo, wo.nodes["relay"]) == ""  # Wake context is not a tool result.
    # A new main task inherits durable source context without copying the email body.
    store.apply("add_node", {"work_id": wid, "id": "replacement", "parent_id": wo.goal_node_id,
                            "type": "subtask", "title": "Relay the approved choice"})
    wo = store.load(wid)
    for nid in ("relay", "replacement"):
        view = worker_data(wo, nid)
        for text in (render_view("worker", view=view),
                     render_view("finalizer_input", view=view, result_text="Recorded worker result", repeat_failure_limit=2)):
            for value in ("datapod:email:request", "datapod:email:approval", "Yes, choose option B.",
                          "approver@example.test", "2026-09-20T15:00:00+00:00", "thread-selection",
                          "matched waiting condition", "wake interpretation (agent judgment)"):
                assert value in text
            assert "forged-model-reference" not in text
    portfolio = render_view("portfolio", work=view["work"])
    assert "datapod:email:approval" in portfolio
    assert "source excerpt:" not in portfolio  # Planners get attribution + summary.
    persist(bb)
    assert len(store.load(wid).provenance_for("relay")) == 1


@pytest.mark.parametrize("invalid", ["missing-source", "changed-gate", "changed-task"])
def test_untrusted_or_stale_match_cannot_release_task(invalid):
    store, wid = scenario()
    bb = prepare(wid, [email("approval", "Yes, choose option B.")])
    if invalid == "missing-source":
        bb.get_state_value("node_wakes")[0]["source_item_id"] = "unknown"
    elif invalid == "changed-gate":
        store.apply("defer_node", {"work_id": wid, "node_id": "relay", "wake_kind": "event", "wake_ref": "A different approval"})
    else:
        store.apply("set_status", {"work_id": wid, "node_id": "relay", "status": "proposed", "content": "Different task"})
    before = store.load(wid).model_dump(mode="json")
    persist(bb)
    assert store.load(wid).model_dump(mode="json") == before
    assert not bb.get_state_value("woken_work_nodes")


def test_state_mover_render_keeps_candidate_beyond_thirty_and_full_directive():
    store, wid = scenario()
    long_directive = "Check this specific selection. " * 12 + "Only option B is acceptable."
    store.apply("set_status", {"work_id": wid, "node_id": "relay", "status": "proposed", "content": long_directive})
    bb = prepare(wid, [email(str(i), "Unrelated message") for i in range(35)] + [email("approval", "Yes, choose option B.")])
    env = Environment(loader=FileSystemLoader(str(Path("app/assistant/agents"))), undefined=ChainableUndefined)
    text = env.get_template("dayflow_orchestrator/state_mover/prompts/user.j2").render(
        waiting_work_nodes=bb.get_state_value("waiting_work_nodes"), work_wait_intake=bb.get_state_value("work_wait_intake"))
    assert long_directive in text
    assert "datapod:email:approval" in text
    assert "Yes, choose option B." in text
    assert "source item: approval" in text


def test_internal_records_are_not_external_wake_candidates_and_empty_pass_clears_intake():
    store, wid = scenario()
    internal = {"metadata": {"item_id": "internal-result", "source_type": "action_result",
                             "summary": "An agent says approval arrived"}}
    bb = prepare(wid, [internal, email("approval", "Yes, choose option B.")])
    assert [s["item_id"] for s in bb.get_state_value("work_wait_intake")] == ["approval"]
    persist(bb)
    StateMoverPrepNode(name="prep", blackboard=bb, agent_registry={}, tool_registry={})._build_work_object_waits([])
    assert bb.get_state_value("waiting_work_nodes") == []
    assert bb.get_state_value("work_wait_intake") == []
