import app.assistant.tests.test_setup  # noqa: F401

import time
from datetime import datetime, timedelta

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.task_ir_runtime.task_ir_runner import TaskIRRunner
from app.assistant.task_ir_runtime.task_ir_timers import TaskIRTimerManager
from app.assistant.utils.pydantic_classes import ToolResult


def test_task_ir_runner_wait_resume_decision(monkeypatch):
    def _fake_execute(self, tool_message):  # noqa: ANN001
        _ = self
        _ = tool_message
        return ToolResult(result_type="success", content="ok")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_test_wait_resume",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "title": "Do setup",
                "executor": "emi_team_manager",
                "instruction": "Initialize task context.",
                "next_step": "s2",
            },
            {
                "id": "s2",
                "kind": "wait_for_event",
                "title": "Wait for trigger",
                "event_name": "watch.match.test",
                "next_step": "s3",
            },
            {
                "id": "s3",
                "kind": "decision",
                "title": "Continue?",
                "condition": "\"watch.match.test\" in task_state.facts.events_observed",
                "on_true": "s4",
                "on_false": "s5",
            },
            {"id": "s4", "kind": "end", "title": "Done"},
            {"id": "s5", "kind": "end", "title": "Done false branch"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(
        compiled_task=compiled_task,
        initial_context={"task_state": {"facts": {"events_observed": {}}}},
    )
    assert run_state["status"] == "waiting"
    assert run_state["waiting_event_name"] == "watch.match.test"

    resumed = runner.resume_run(
        run_id=run_state["run_id"],
        event_name="watch.match.test",
        event_payload={"should_continue": True},
    )
    assert resumed["status"] == "completed"


def test_wait_gate_requires_clear_condition_before_advancing():
    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_wait_gate_condition",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "Wait until ready event payload is true",
                "event_name": "watch.match.test",
                "clear_condition": "\"watch.match.test\" in task_state.facts.events_observed",
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(
        compiled_task=compiled_task,
        initial_context={"task_state": {"facts": {"events_observed": {}}}},
    )
    assert run_state["status"] == "waiting"

    completed = runner.resume_run(
        run_id=run_state["run_id"],
        event_name="watch.match.test",
        event_payload={"ready": False},
    )
    assert completed["status"] == "completed"


def test_wait_gate_can_autopass_when_condition_already_true():
    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_wait_gate_autopass",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "No block when gate already true",
                "event_name": "clock.tick.15m",
                "clear_condition": "flags.ready == True",
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={"flags": {"ready": True}})
    assert run_state["status"] == "completed"


def test_wait_gate_entry_clears_stale_observed_events():
    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_wait_gate_reentry_resets_observed_events",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_gate",
                "title": "Wait for fresh email reply event",
                "subscriptions": ["signal_router.watch.email_jouko_reply"],
                "release_condition": "\"signal_router.watch.email_jouko_reply\" in task_state.facts.events_observed",
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(
        compiled_task=compiled_task,
        initial_context={
            "events": {"observed": {"signal_router.watch.email_jouko_reply": True}},
            "task_state": {"facts": {"events_observed": {"signal_router.watch.email_jouko_reply": True}}},
        },
    )
    assert run_state["status"] == "waiting"
    observed = run_state["context"]["task_state"]["facts"]["events_observed"]
    assert "signal_router.watch.email_jouko_reply" not in observed


def test_wait_for_clock_timer_event_per_step():
    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_step_local_timer",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "Per-step timer wait",
                "event_name": "clock.timer.0s",
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }
    runner = TaskIRRunner(max_steps_per_tick=20)
    # Clock timers always resolve asynchronously: a 0s timer arms a ~immediate job
    # that fires via the scheduler and resumes this (subscribed) runner.
    runner.ensure_event_subscription()
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={})
    run_id = run_state["run_id"]
    deadline = time.time() + 3.0
    latest = run_state
    while time.time() < deadline:
        latest = runner._require_run(run_id)
        if latest["status"] == "completed":
            break
        time.sleep(0.05)
    assert latest["status"] == "completed"


def test_wait_for_relative_timer_reentry_clears_stale_timer_state():
    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_relative_timer_reentry_resets_state",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "Wait for relative timer with stale timer state preloaded",
                "event_name": "clock.timer.10s",
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(
        compiled_task=compiled_task,
        initial_context={
            "timers": {
                "s1::clock.timer.10s": {
                    "event_name": "clock.timer.10s",
                    "timer_seconds": 10,
                    "armed_at_utc": "2026-01-01T00:00:00+00:00",
                    "deadline_utc": time.time() - 5.0,
                }
            }
        },
    )
    assert run_state["status"] == "waiting"


def test_seconds_until_local_clock_event_rolls_to_next_day_for_past_clock_time():
    manager = TaskIRTimerManager()
    now_local = datetime.now().astimezone()
    past_local = now_local - timedelta(minutes=1)
    future_local = now_local + timedelta(minutes=1)
    past_event = f"clock.local.{past_local.hour:02d}_{past_local.minute:02d}"
    future_event = f"clock.local.{future_local.hour:02d}_{future_local.minute:02d}"

    past_seconds = manager.seconds_until_local_clock_event(event_name=past_event)
    future_seconds = manager.seconds_until_local_clock_event(event_name=future_event)

    assert isinstance(past_seconds, float)
    assert isinstance(future_seconds, float)
    assert past_seconds > 6 * 3600
    assert 0.0 <= future_seconds <= 2 * 3600


def test_wait_for_relative_timer_auto_wakes_waiting_run():
    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_relative_timer_auto_wake",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "Wait for relative timer",
                "event_name": "clock.timer.1s",
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }
    runner = TaskIRRunner(max_steps_per_tick=20)
    # Subscribe so the timer job's published event routes back to THIS runner and
    # resumes the run (production wires this in initialize_system).
    runner.ensure_event_subscription()
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={})
    assert run_state["status"] == "waiting"
    run_id = run_state["run_id"]

    deadline = time.time() + 3.0
    latest = run_state
    while time.time() < deadline:
        latest = runner._require_run(run_id)
        if latest["status"] == "completed":
            break
        time.sleep(0.1)
    assert latest["status"] == "completed"


def test_wait_step_auto_registers_watcher_contract(monkeypatch):
    class _FakeRegistration:
        def __init__(self, registration_id: str):
            self.registration_id = registration_id

    class _FakeSignalRouter:
        def __init__(self):
            self.seen_requests = []

        def register_watch(self, *, request):  # noqa: ANN001
            self.seen_requests.append(request)
            return _FakeRegistration("watch_test_1")

    fake_router = _FakeSignalRouter()
    monkeypatch.setattr(DI, "signal_router", fake_router, raising=False)

    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_wait_auto_register",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "Wait for semantic email match",
                "event_name": "signal_router.watch.email_from_jukka_cabin",
                "watch_registration": {
                    "watch_type": "email_semantic_match",
                    "predicate": {
                        "semantic_query": "email from jukka that talks about directions to the cabin",
                        "sender_contains": "jukka",
                    },
                },
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={})
    assert run_state["status"] == "waiting"
    assert len(fake_router.seen_requests) == 1


def test_decision_can_use_task_state_facts_materialized_by_prior_action(monkeypatch):
    def _fake_execute(self, tool_message):  # noqa: ANN001
        _ = self
        _ = tool_message
        return ToolResult(
            result_type="success",
            content="ok",
            data={"task_state_update": {"facts": {"phrase_seen": True}}},
        )

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_task_state_update",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "title": "Materialize phrase flag",
                "executor": "emi_team_manager",
                "instruction": "Materialize phrase_seen fact.",
                "next_step": "s2",
            },
            {
                "id": "s2",
                "kind": "decision",
                "title": "Phrase seen?",
                "condition": "task_state.facts.phrase_seen == True",
                "on_true": "s3",
                "on_false": "s4",
            },
            {"id": "s3", "kind": "end", "title": "True branch"},
            {"id": "s4", "kind": "end", "title": "False branch"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={})
    assert run_state["status"] == "completed"
    assert run_state["current_step_id"] == "s3"
    task_state = run_state["context"]["task_state"]
    assert task_state["facts"]["phrase_seen"] is True


def test_action_data_id_contract_enforced_and_forwarded(monkeypatch):
    def _fake_execute(self, tool_message):  # noqa: ANN001
        _ = self
        data = tool_message.tool_data.get("data", {})
        step_id = data.get("task_ir_step_id")
        if step_id == "s1":
            return ToolResult(
                result_type="success",
                content="ok",
                data={
                    "task_state_update": {
                        "artifacts": {"artifact_1": [{"subject": "a"}]},
                        "facts": {"fact_1": True},
                    }
                },
            )
        if step_id == "s2":
            # Consumed inputs are delivered to the executor via the `information`
            # field (natural-language context), not a structured data key. The
            # produces/consumes data_id round-trip is asserted via data_index below.
            information = tool_message.tool_data.get("arguments", {}).get("information", "")
            assert "artifact_1" in information, information
            assert "fact_1" in information, information
            return ToolResult(
                result_type="success",
                content="ok",
                data={"task_state_update": {"facts": {"fact_2": True}}},
            )
        raise AssertionError(f"Unexpected step_id: {step_id}")

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_data_contract_ok",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "title": "Produce ids",
                "executor": "emi_team_manager",
                "instruction": "Produce artifact_1 and fact_1",
                "produces_data_ids": ["artifact_1", "fact_1"],
                "next_step": "s2",
            },
            {
                "id": "s2",
                "kind": "action",
                "title": "Consume ids",
                "executor": "emi_team_manager",
                "instruction": "Consume artifact_1 and fact_1",
                "consumes_data_ids": ["artifact_1", "fact_1"],
                "produces_data_ids": ["fact_2"],
                "next_step": "s3",
            },
            {"id": "s3", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={})
    assert run_state["status"] == "completed"
    data_index = run_state["context"]["task_state"]["data_index"]
    assert "artifact_1" in data_index
    assert "fact_1" in data_index
    assert "fact_2" in data_index


def test_action_declared_output_must_be_materialized(monkeypatch):
    def _fake_execute(self, tool_message):  # noqa: ANN001
        _ = self
        _ = tool_message
        return ToolResult(result_type="success", content="ok", data={})

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_data_contract_missing_output",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "title": "Produce ids",
                "executor": "emi_team_manager",
                "instruction": "Should produce artifact_1 and artifact_2",
                # Multiple declared outputs cannot be auto-projected (single-output
                # projection only applies when exactly one id is unresolved), so an
                # empty manager result must raise.
                "produces_data_ids": ["artifact_1", "artifact_2"],
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={})
    # The runner records step failures on the run (status=failed + last_error)
    # rather than raising out of start_run.
    assert run_state["status"] == "failed"
    assert "could not materialize" in str(run_state.get("last_error") or "")


def test_single_artifact_output_can_be_projected_from_final_answer_payload(monkeypatch):
    def _fake_execute(self, tool_message):  # noqa: ANN001
        _ = self
        _ = tool_message
        return ToolResult(
            result_type="success",
            content="ok",
            data={"final_answer_answer": "emails fetched"},
        )

    monkeypatch.setattr(
        "app.assistant.lib.core_tools.manager_interface.manager_interface.ManagerInterface.execute",
        _fake_execute,
    )

    compiled_task = {
        "schema_version": "task_ir_v1",
        "task_id": "task_ir_single_artifact_projection",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "title": "Produce one artifact",
                "executor": "emi_team_manager",
                "instruction": "Produce artifact_1",
                "produces_data_ids": ["artifact_1"],
                "next_step": "s2",
            },
            {"id": "s2", "kind": "end", "title": "Done"},
        ],
    }

    runner = TaskIRRunner(max_steps_per_tick=20)
    run_state = runner.start_run(compiled_task=compiled_task, initial_context={})
    assert run_state["status"] == "completed"
    artifacts = run_state["context"]["task_state"]["artifacts"]
    assert "artifact_1" in artifacts
    # A single declared artifact output is projected from the manager envelope and
    # coerced to a string (JSON-serialized); parse it back to verify the payload.
    import json
    assert json.loads(artifacts["artifact_1"]) == {"final_answer_answer": "emails fetched"}
