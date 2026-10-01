"""work_emi_team_manager's result is this attempt's findings on the node, not a final-answer retelling."""
from types import SimpleNamespace

import pytest
import yaml

from app.assistant.agent_classes.WorkPlanner import WorkPlanner
from app.assistant.control_nodes.work_result_node import WorkResultNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard
from app.assistant.utils.path_utils import get_repo_root
from work_objects.result_recorder import _is_failure
from work_objects.runtime import reset_work_context, set_work_context
from work_objects.store import WorkStore

PLANNER = "work_emi_team::planner"


class Board(FakeBlackboard):
    def __init__(self):
        super().__init__({"role_bindings": {"planner": PLANNER}})
        self.messages = []

    def add_msg(self, msg):
        self.messages.append(msg)

    def get_messages(self):
        return self.messages


@pytest.fixture
def task(tmp_path):
    store = WorkStore(str(tmp_path / "work.db"))
    wo = store.apply("create_work_object", dict(title="t", goal_content="Set the thermostat",
                                                satisfied_when_kind="all_owned_children_done"))
    store.apply("add_node", dict(work_id=wo.id, id="main", type="subtask", parent_id=wo.goal_node_id,
                                 title="Set the thermostat"))
    store.apply("set_status", dict(work_id=wo.id, node_id="main", status="actionable"))
    store.apply("claim_task", dict(work_id=wo.id, node_id="main"))
    token = set_work_context(store, wo.id, "main", PLANNER)
    yield store, wo.id
    reset_work_context(token)
    store.close()


def reconcile(findings):
    planner = SimpleNamespace(_transition_subtask=WorkPlanner._transition_subtask,
                              _attach_shared_fact=WorkPlanner._attach_shared_fact)
    WorkPlanner._reconcile_to_graph(planner, {"findings": findings})


def findings_on(store, wid):
    return [n for n in store.load(wid).nodes.values() if n.parent_id == "main" and n.type == "evidence"]


def test_findings_are_tagged_with_the_attempt_and_deduped_within_it(task):
    store, wid = task
    epoch = store.load(wid).nodes["main"].payload["dispatch_epoch"]
    store.apply("add_node", dict(work_id=wid, id="earlier", type="evidence", parent_id="main",
                                 content="Set to 75F.", status="assumed", payload={"finding_attempt": epoch - 1}))
    reconcile(["Set to 75F."])
    reconcile(["Set to 75F."])
    current = [n for n in findings_on(store, wid) if n.payload.get("finding_attempt") == epoch]
    assert [n.content for n in current] == ["Set to 75F."]   # a retry's same conclusion is still recorded


def test_result_is_this_attempts_findings(task):
    store, wid = task
    epoch = store.load(wid).nodes["main"].payload["dispatch_epoch"]
    store.apply("add_node", dict(work_id=wid, id="earlier", type="evidence", parent_id="main",
                                 content="Earlier attempt's claim.", status="assumed",
                                 payload={"finding_attempt": epoch - 1}))
    reconcile(["Nest applied set_mode and set_target_temperature (this attempt's call)."])
    bb = Board()
    WorkResultNode(name="work_result_node", blackboard=bb, agent_registry={}, tool_registry={}).action_handler(None)
    final = bb.get_state_value("final_answer")
    assert final["final_answer_answer"] == "- Nest applied set_mode and set_target_temperature (this attempt's call)."
    assert "exit_state" not in final and not _is_failure(SimpleNamespace(data=final, result_type="final_answer"))
    assert bb.get_state_value("next_agent") is None


def test_empty_return_goes_back_to_the_planner_once_then_fails(task):
    store, wid = task
    bb = Board()
    node = WorkResultNode(name="work_result_node", blackboard=bb, agent_registry={}, tool_registry={})
    node.action_handler(None)
    assert bb.get_state_value("next_agent") == PLANNER
    assert bb.get_state_value("final_answer") is None
    [msg] = bb.messages
    assert msg.receiver == PLANNER and "without recording a result" in msg.content
    node.action_handler(None)
    final = bb.get_state_value("final_answer")
    assert "No result was recorded" in final["final_answer_answer"]
    assert _is_failure(SimpleNamespace(data=final, result_type="final_answer"))


def test_work_manager_routes_return_to_the_result_node():
    config = yaml.safe_load((get_repo_root() / "app/assistant/multi_agents/work_emi_team_manager/config.yaml")
                            .read_text(encoding="utf-8"))
    state_map = config["flow_config"]["state_map"]
    assert state_map["work_emi_team::planner_return_control"] == "work_result_node"
    assert state_map["work_result_node"] == "manager_exit_node"
    assert state_map["graceful_exit_control_node"] == "manager_exit_node"
    assert "emi_team::final_answer" not in {a["name"] for a in config["agents"]}
