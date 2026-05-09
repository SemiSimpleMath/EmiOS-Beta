from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


@dataclass(frozen=True)
class AtomicScenario:
    marker: str
    task_text: str
    phase_i_atoms: dict[str, Any]
    compiled_task: dict[str, Any]
    logic_tree: dict[str, Any]


def _scenario_map() -> dict[str, AtomicScenario]:
    base_info = "\nAlready provided facts and artifacts:\n- fact_1: preloaded boolean gate.\n"
    return {
        "AT1": AtomicScenario(
            marker="AT1",
            task_text="[AT1] One action only, then end.",
            phase_i_atoms={
                "events": [],
                "conditions": [],
                "actions": [{"id": "action_1", "executor": "personal_admin_manager", "title": "Send ping"}],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "action",
                        "title": "Send ping",
                        "executor": "personal_admin_manager",
                        "instruction": "Send one ping email.",
                        "next_step": "s2",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s2", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [{"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"}],
                "bindings": [{"step_id": "s1", "atomic_type": "action", "atomic_id": "action_1"}],
            },
        ),
        "AT2": AtomicScenario(
            marker="AT2",
            task_text="[AT2] Use preloaded input fact_1 in one action." + base_info,
            phase_i_atoms={
                "events": [],
                "conditions": [],
                "actions": [{"id": "action_1", "executor": "emi_team_manager", "title": "Use input"}],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "action",
                        "title": "Consume preloaded fact",
                        "executor": "emi_team_manager",
                        "instruction": "Read fact_1 and prepare artifact_1.",
                        "next_step": "s2",
                        "consumes_data_ids": ["fact_1"],
                        "produces_data_ids": ["artifact_1"],
                    },
                    {"id": "s2", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [{"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"}],
                "bindings": [{"step_id": "s1", "atomic_type": "action", "atomic_id": "action_1"}],
            },
        ),
        "AT3": AtomicScenario(
            marker="AT3",
            task_text="[AT3] Wait gate with two subscriptions, then end.",
            phase_i_atoms={
                "events": [
                    {
                        "id": "event_1",
                        "event_type": "watch_event",
                        "canonical_event_name": "signal_router.watch.reply_arrived",
                        "description": "Reply watcher.",
                    },
                    {
                        "id": "event_2",
                        "event_type": "time_event",
                        "canonical_event_name": "clock.local.22_00",
                        "description": "Cutoff.",
                    },
                ],
                "conditions": [],
                "actions": [],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "wait_gate",
                        "title": "Wait gate",
                        "subscriptions": ["signal_router.watch.reply_arrived", "clock.local.22_00"],
                        "release_condition": '"signal_router.watch.reply_arrived" in task_state.facts.events_observed or "clock.local.22_00" in task_state.facts.events_observed',
                        "next_step": "s2",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s2", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [{"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"}],
                "bindings": [
                    {"step_id": "s1", "atomic_type": "event", "atomic_id": "event_1"},
                    {"step_id": "s1", "atomic_type": "event", "atomic_id": "event_2"},
                ],
            },
        ),
        "AT4": AtomicScenario(
            marker="AT4",
            task_text="[AT4] Wait gate with extra local-time bound (<23:00).",
            phase_i_atoms={
                "events": [
                    {
                        "id": "event_1",
                        "event_type": "watch_event",
                        "canonical_event_name": "signal_router.watch.reply_arrived",
                        "description": "Reply watcher.",
                    }
                ],
                "conditions": [],
                "actions": [],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "wait_gate",
                        "title": "Wait until reply before 23:00",
                        "subscriptions": ["signal_router.watch.reply_arrived"],
                        "release_condition": '"signal_router.watch.reply_arrived" in task_state.facts.events_observed and task_state.facts.time_local_hhmm < "23:00"',
                        "next_step": "s2",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s2", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [{"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"}],
                "bindings": [{"step_id": "s1", "atomic_type": "event", "atomic_id": "event_1"}],
            },
        ),
        "AT5": AtomicScenario(
            marker="AT5",
            task_text="[AT5] Simple if statement on fact_1.",
            phase_i_atoms={
                "events": [],
                "conditions": [{"id": "condition_1", "condition_type": "state_predicate"}],
                "actions": [{"id": "action_1", "executor": "emi_team_manager"}],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "decision",
                        "title": "If fact_1",
                        "condition": "task_state.facts.fact_1 == True",
                        "on_true": "s2",
                        "on_false": "s3",
                        "consumes_data_ids": ["fact_1"],
                        "produces_data_ids": [],
                    },
                    {
                        "id": "s2",
                        "kind": "action",
                        "title": "True branch",
                        "executor": "emi_team_manager",
                        "instruction": "Handle true branch.",
                        "next_step": "s4",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s3", "kind": "end", "title": "False end"},
                    {"id": "s4", "kind": "end", "title": "True end"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [
                    {"from_step_id": "s1", "to_step_id": "s2", "reason": "on_true"},
                    {"from_step_id": "s1", "to_step_id": "s3", "reason": "on_false"},
                    {"from_step_id": "s2", "to_step_id": "s4", "reason": "next_step"},
                ],
                "bindings": [{"step_id": "s1", "atomic_type": "condition", "atomic_id": "condition_1"}],
            },
        ),
        "AT6": AtomicScenario(
            marker="AT6",
            task_text="[AT6] Loop until fact_1 becomes true.",
            phase_i_atoms={
                "events": [],
                "conditions": [{"id": "condition_1", "condition_type": "state_predicate"}],
                "actions": [{"id": "action_1", "executor": "emi_team_manager"}],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "action",
                        "title": "Do work",
                        "executor": "emi_team_manager",
                        "instruction": "Perform one loop unit.",
                        "next_step": "s2",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {
                        "id": "s2",
                        "kind": "decision",
                        "title": "Stop?",
                        "condition": "task_state.facts.fact_1 == True",
                        "on_true": "s3",
                        "on_false": "s1",
                        "consumes_data_ids": ["fact_1"],
                        "produces_data_ids": [],
                    },
                    {"id": "s3", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [
                    {"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"},
                    {"from_step_id": "s2", "to_step_id": "s3", "reason": "on_true"},
                    {"from_step_id": "s2", "to_step_id": "s1", "reason": "on_false"},
                ],
                "bindings": [{"step_id": "s2", "atomic_type": "condition", "atomic_id": "condition_1"}],
            },
        ),
        "AT7": AtomicScenario(
            marker="AT7",
            task_text="[AT7] wait_for_event then action then end.",
            phase_i_atoms={
                "events": [
                    {
                        "id": "event_1",
                        "event_type": "watch_event",
                        "canonical_event_name": "signal_router.watch.email_ping",
                        "description": "Email ping.",
                    }
                ],
                "conditions": [],
                "actions": [{"id": "action_1", "executor": "personal_admin_manager"}],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "wait_for_event",
                        "title": "Wait ping",
                        "event_name": "signal_router.watch.email_ping",
                        "next_step": "s2",
                        "clear_condition": "",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {
                        "id": "s2",
                        "kind": "action",
                        "title": "Handle ping",
                        "executor": "personal_admin_manager",
                        "instruction": "Send ack.",
                        "next_step": "s3",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s3", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [
                    {"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"},
                    {"from_step_id": "s2", "to_step_id": "s3", "reason": "next_step"},
                ],
                "bindings": [
                    {"step_id": "s1", "atomic_type": "event", "atomic_id": "event_1"},
                    {"step_id": "s2", "atomic_type": "action", "atomic_id": "action_1"},
                ],
            },
        ),
        "AT8": AtomicScenario(
            marker="AT8",
            task_text="[AT8] Relative timer wait_for_event clock.timer.15m.",
            phase_i_atoms={
                "events": [
                    {
                        "id": "event_1",
                        "event_type": "time_event",
                        "canonical_event_name": "clock.timer.15m",
                        "description": "15-minute timer.",
                    }
                ],
                "conditions": [],
                "actions": [],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "wait_for_event",
                        "title": "Wait 15m",
                        "event_name": "clock.timer.15m",
                        "next_step": "s2",
                        "clear_condition": "",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s2", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [{"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"}],
                "bindings": [{"step_id": "s1", "atomic_type": "event", "atomic_id": "event_1"}],
            },
        ),
        "AT9": AtomicScenario(
            marker="AT9",
            task_text="[AT9] Branch true/false to separate actions.",
            phase_i_atoms={
                "events": [],
                "conditions": [{"id": "condition_1", "condition_type": "state_predicate"}],
                "actions": [
                    {"id": "action_1", "executor": "personal_admin_manager"},
                    {"id": "action_2", "executor": "personal_admin_manager"},
                ],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "decision",
                        "title": "Gate",
                        "condition": "task_state.facts.fact_1 == True",
                        "on_true": "s2",
                        "on_false": "s3",
                        "consumes_data_ids": ["fact_1"],
                        "produces_data_ids": [],
                    },
                    {
                        "id": "s2",
                        "kind": "action",
                        "title": "True action",
                        "executor": "personal_admin_manager",
                        "instruction": "Do true action.",
                        "next_step": "s4",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {
                        "id": "s3",
                        "kind": "action",
                        "title": "False action",
                        "executor": "personal_admin_manager",
                        "instruction": "Do false action.",
                        "next_step": "s4",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s4", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [
                    {"from_step_id": "s1", "to_step_id": "s2", "reason": "on_true"},
                    {"from_step_id": "s1", "to_step_id": "s3", "reason": "on_false"},
                    {"from_step_id": "s2", "to_step_id": "s4", "reason": "next_step"},
                    {"from_step_id": "s3", "to_step_id": "s4", "reason": "next_step"},
                ],
                "bindings": [{"step_id": "s1", "atomic_type": "condition", "atomic_id": "condition_1"}],
            },
        ),
        "AT10": AtomicScenario(
            marker="AT10",
            task_text="[AT10] action -> wait_gate -> action flow.",
            phase_i_atoms={
                "events": [
                    {
                        "id": "event_1",
                        "event_type": "watch_event",
                        "canonical_event_name": "signal_router.watch.external_ready",
                        "description": "External ready signal.",
                    }
                ],
                "conditions": [],
                "actions": [
                    {"id": "action_1", "executor": "emi_team_manager"},
                    {"id": "action_2", "executor": "emi_team_manager"},
                ],
            },
            compiled_task={
                "entry_step_id": "s1",
                "steps": [
                    {
                        "id": "s1",
                        "kind": "action",
                        "title": "Prepare",
                        "executor": "emi_team_manager",
                        "instruction": "Prepare state.",
                        "next_step": "s2",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {
                        "id": "s2",
                        "kind": "wait_gate",
                        "title": "Wait external ready",
                        "subscriptions": ["signal_router.watch.external_ready"],
                        "release_condition": '"signal_router.watch.external_ready" in task_state.facts.events_observed',
                        "next_step": "s3",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {
                        "id": "s3",
                        "kind": "action",
                        "title": "Finalize",
                        "executor": "emi_team_manager",
                        "instruction": "Finalize state.",
                        "next_step": "s4",
                        "consumes_data_ids": [],
                        "produces_data_ids": [],
                    },
                    {"id": "s4", "kind": "end", "title": "Done"},
                ],
            },
            logic_tree={
                "entry_step_id": "s1",
                "edges": [
                    {"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"},
                    {"from_step_id": "s2", "to_step_id": "s3", "reason": "next_step"},
                    {"from_step_id": "s3", "to_step_id": "s4", "reason": "next_step"},
                ],
                "bindings": [
                    {"step_id": "s1", "atomic_type": "action", "atomic_id": "action_1"},
                    {"step_id": "s2", "atomic_type": "event", "atomic_id": "event_1"},
                    {"step_id": "s3", "atomic_type": "action", "atomic_id": "action_2"},
                ],
            },
        ),
    }


class _AtomicCompileStub:
    def __init__(self, scenarios: dict[str, AtomicScenario]) -> None:
        self._scenarios = scenarios

    def _scenario_from_prompt(self, prompt_blob: str) -> AtomicScenario:
        marker_match = re.search(r"\[(AT\d+)\]", prompt_blob)
        if not marker_match:
            raise ValueError("Atomic compile stub could not find scenario marker [ATx] in prompt.")
        marker = str(marker_match.group(1))
        scenario = self._scenarios.get(marker)
        if scenario is None:
            raise ValueError(f"Atomic compile stub missing scenario for marker '{marker}'.")
        return scenario

    def structured_output(self, messages, use_json=False, **params):
        _ = use_json
        _ = params
        prompt_blob = "\n".join(
            str(msg.get("content") or "")
            for msg in messages
            if isinstance(msg, dict) and isinstance(msg.get("content"), str)
        )
        scenario = self._scenario_from_prompt(prompt_blob)
        if "Phase I objective" in prompt_blob:
            return {
                "what_i_am_thinking": "Extracting atomic units.",
                "summary": "Phase I complete.",
                "checklist": ["events", "conditions", "actions"],
                "progress": ["phase_i_complete"],
                "plan": "phase_i",
                "action": "return_control",
                "action_input": "{}",
                "phase_i_atoms": scenario.phase_i_atoms,
                "final_answer_answer": "phase i done",
            }
        if "Phase II objective" in prompt_blob:
            return {
                "what_i_am_thinking": "Composing flow graph.",
                "summary": "Phase II complete.",
                "checklist": ["steps", "logic_tree"],
                "progress": ["phase_ii_complete"],
                "plan": "phase_ii",
                "action": "return_control",
                "action_input": "{}",
                "compiled_task": scenario.compiled_task,
                "logic_tree": scenario.logic_tree,
                "final_answer_answer": "phase ii done",
            }
        raise ValueError("Atomic compile stub received unknown prompt phase.")


def _build_request(task: str, *, marker: str) -> Message:
    info = {"task_id": f"atomic_{marker.lower()}", "task_description": f"atomic scenario {marker}"}
    return Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=task,
        information=json.dumps(info, ensure_ascii=False),
    )


def _compile_atomic(monkeypatch, marker: str) -> dict[str, Any]:
    scenarios = _scenario_map()
    scenario = scenarios[marker]
    monkeypatch.setattr(
        "app.assistant.agent_runtime.services.llm_client.LLMFactory.get_llm_interface",
        lambda **kwargs: _AtomicCompileStub(scenarios),
    )
    monkeypatch.setattr(
        "app.assistant.control_nodes.task_compile_post_node.TaskCompilePostNode._persist_compiled_task",
        lambda self, merged_task, cfg: f"memory://{marker}.json",
    )
    DI.manager_registry.preload_all()
    manager = DI.multi_agent_manager_factory.create_manager("task_compile_manager")
    result = manager.request_handler(_build_request(scenario.task_text, marker=marker))
    assert result.result_type == "final_answer"
    assert isinstance(result.data, dict)
    data_list = result.data.get("final_answer_data_list", [])
    assert isinstance(data_list, list)
    compiled_item = next(
        (item for item in data_list if isinstance(item, dict) and item.get("key") == "compiled_task"),
        None,
    )
    assert isinstance(compiled_item, dict)
    compiled_json = compiled_item.get("value")
    assert isinstance(compiled_json, str) and compiled_json.strip()
    return json.loads(compiled_json)


def test_atomic_1_action_only(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT1")
    kinds = [str(step.get("kind") or "") for step in compiled.get("steps", []) if isinstance(step, dict)]
    assert kinds.count("action") == 1
    assert kinds.count("end") >= 1


def test_atomic_2_action_consumes_input(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT2")
    action = next(step for step in compiled["steps"] if isinstance(step, dict) and step.get("kind") == "action")
    assert "fact_1" in list(action.get("consumes_data_ids") or [])


def test_atomic_3_wait_gate(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT3")
    wait_step = next(step for step in compiled["steps"] if isinstance(step, dict) and step.get("kind") == "wait_gate")
    subs = list(wait_step.get("subscriptions") or [])
    assert "signal_router.watch.reply_arrived" in subs
    assert "clock.local.22_00" in subs


def test_atomic_4_wait_gate_with_time_bound(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT4")
    wait_step = next(step for step in compiled["steps"] if isinstance(step, dict) and step.get("kind") == "wait_gate")
    release_condition = str(wait_step.get("release_condition") or "")
    assert "time_local_hhmm" in release_condition
    assert "< \"23:00\"" in release_condition


def test_atomic_5_if_statement(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT5")
    decision = next(step for step in compiled["steps"] if isinstance(step, dict) and step.get("kind") == "decision")
    assert str(decision.get("on_true") or "").strip()
    assert str(decision.get("on_false") or "").strip()


def test_atomic_6_loop(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT6")
    steps = [step for step in compiled.get("steps", []) if isinstance(step, dict)]
    step_ids = [str(step.get("id") or "") for step in steps]
    index_by_id = {step_id: idx for idx, step_id in enumerate(step_ids)}
    has_back_edge = False
    for step in steps:
        src = str(step.get("id") or "")
        for edge_key in ("next_step", "on_true", "on_false"):
            dst = str(step.get(edge_key) or "").strip()
            if dst and dst in index_by_id and index_by_id[dst] <= index_by_id[src]:
                has_back_edge = True
    assert has_back_edge


def test_atomic_7_wait_for_event(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT7")
    wait_step = next(
        step for step in compiled["steps"] if isinstance(step, dict) and step.get("kind") == "wait_for_event"
    )
    assert str(wait_step.get("event_name") or "").startswith("signal_router.watch.")


def test_atomic_8_relative_timer_wait(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT8")
    wait_step = next(
        step for step in compiled["steps"] if isinstance(step, dict) and step.get("kind") == "wait_for_event"
    )
    assert str(wait_step.get("event_name") or "") == "clock.timer.15m"


def test_atomic_9_if_else_actions(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT9")
    actions = [step for step in compiled["steps"] if isinstance(step, dict) and step.get("kind") == "action"]
    assert len(actions) == 2


def test_atomic_10_action_wait_action(monkeypatch):
    compiled = _compile_atomic(monkeypatch, "AT10")
    kinds = [str(step.get("kind") or "") for step in compiled.get("steps", []) if isinstance(step, dict)]
    assert kinds[:3] == ["action", "wait_gate", "action"]
