"""Tests for PostRoomFinalizeNode — the multi-phase finalizer that persists everything."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.post_room_finalize_node import (
    PostRoomFinalizeNode,
    _collect_pod_references,
)
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_all_items,
    load_item_by_id,
    make_dayflow_message,
    make_plan_synopsis_message,
    seed_items,
)


def _make_node(bb: FakeBlackboard) -> PostRoomFinalizeNode:
    return PostRoomFinalizeNode(
        name="post_room_finalize_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


def _base_bb_state(**overrides) -> dict:
    """Minimal blackboard state the finalize node expects."""
    defaults = {
        "existing_dayflow_items": [],
        "intake_items_deduped": [],
        "planned_tasks": [],
        "state_mutations": [],
        "acted_on_item_ids": [],
        "active_dispatch_records": [],
        "action_result_events": [],
        "plan_synopses": [],
        "completed_plan_ids": [],
        "closed_task_ids": [],
        "state_mutations_persisted_tf": False,
        "short_id_mapping": {},
        "reasoning": "",
    }
    defaults.update(overrides)
    return defaults


class TestCollectPodReferences:
    """A dispatched manager that minted research findings returns pod_references
    on its result_payload; the collector dedups them across action_result_events
    for stamping onto the action_result + plan step.
    """

    def test_dedups_across_events_and_skips_podless(self):
        events = [
            {"result_payload": {"final_answer_answer": "x", "pod_references": [
                {"pod_id": "p:a", "one_liner": "A"},
                {"pod_id": "p:b", "one_liner": "B"},
            ]}},
            {"result_payload": {"pod_references": [{"pod_id": "p:b", "one_liner": "B"}]}},  # dup
            {"result_payload": {"final_answer_answer": "no pods"}},  # none
        ]
        out = _collect_pod_references(events)
        assert [r["pod_id"] for r in out] == ["p:a", "p:b"]
        assert out[0] == {"pod_id": "p:a", "one_liner": "A"}

    def test_empty_when_no_events_or_no_pods(self):
        assert _collect_pod_references([]) == []
        assert _collect_pod_references([{"result_payload": {"final_answer_answer": "y"}}]) == []


class TestBuildPlannedTaskMessages:

    def test_new_tasks_assigned_short_ids(self):
        """New planned tasks should get unique short_ids."""
        bb = FakeBlackboard(_base_bb_state(
            planned_tasks=[
                {"task_id": "1", "plan_id": "P1", "task": "Task A", "dependencies": []},
                {"task_id": "2", "plan_id": "P1", "task": "Task B", "dependencies": ["1"]},
            ],
            plan_synopses=[{
                "plan_id": "P1", "is_new": True, "changed": True,
                "objective": "Test", "synopsis": "Test plan",
            }],
        ))

        node = _make_node(bb)
        node.action_handler(message=None)

        items = load_all_items()
        plan_tasks = [i for i in items if get_meta(i).get("source_type") == "plan_task"]
        assert len(plan_tasks) == 2

        sids = [get_meta(t).get("short_id") for t in plan_tasks]
        assert len(set(sids)) == 2  # Unique
        assert all(s is not None for s in sids)

    def test_dependencies_resolved_to_canonical_ids(self):
        """Planner uses refs like '1', '2' — finalize must map to real item_ids."""
        bb = FakeBlackboard(_base_bb_state(
            planned_tasks=[
                {"task_id": "1", "plan_id": "P1", "task": "First", "dependencies": []},
                {"task_id": "2", "plan_id": "P1", "task": "Depends on first", "dependencies": ["1"]},
            ],
            plan_synopses=[{
                "plan_id": "P1", "is_new": True, "changed": True,
                "objective": "O", "synopsis": "S",
            }],
        ))

        node = _make_node(bb)
        node.action_handler(message=None)

        items = load_all_items()
        plan_tasks = [i for i in items if get_meta(i).get("source_type") == "plan_task"]
        dep_task = next(
            (t for t in plan_tasks if "Depends" in get_meta(t).get("summary", "")),
            None,
        )
        assert dep_task is not None
        blocked_by = get_meta(dep_task).get("blocked_by", [])
        # Should reference the canonical item_id of the first task, not "1"
        assert len(blocked_by) >= 1
        assert all(b.startswith("task:") for b in blocked_by)


class TestStateMutations:

    def test_acted_on_items_get_marked(self):
        """Items in acted_on_item_ids should be transitioned."""
        seed_items([
            make_dayflow_message(item_id="task:acted", short_id=10, state="actionable"),
        ])

        bb = FakeBlackboard(_base_bb_state(
            existing_dayflow_items=load_all_items(),
            acted_on_item_ids=["task:acted"],
            short_id_mapping={"10": "task:acted"},
        ))

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:acted")
        meta = get_meta(item)
        # Should be dispatched or closed (depends on whether there's a dispatch result)
        assert meta["state"] in ("dispatched", "closed")

    def test_skips_already_persisted_mutations(self):
        """If state_mutations_persisted_tf is True, don't re-apply state_mutations."""
        seed_items([
            make_dayflow_message(item_id="task:stable", short_id=20, state="actionable"),
        ])

        bb = FakeBlackboard(_base_bb_state(
            existing_dayflow_items=load_all_items(),
            state_mutations=[{
                "item_id": "task:stable",
                "to_state": "closed",
                "reason": "Should be skipped",
            }],
            state_mutations_persisted_tf=True,  # Already persisted
            short_id_mapping={"20": "task:stable"},
        ))

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:stable")
        meta = get_meta(item)
        # Should NOT be closed because mutations were already persisted
        assert meta["state"] == "actionable"


class TestPlanCompletion:

    def test_completed_plan_closes_tasks(self):
        seed_items([
            make_plan_synopsis_message(plan_id="P_complete"),
            make_dayflow_message(
                item_id="task:c1", short_id=30, state="dispatched",
                plan_id="P_complete", source_type="plan_task",
            ),
            make_dayflow_message(
                item_id="task:c2", short_id=31, state="waiting",
                plan_id="P_complete", source_type="plan_task",
            ),
        ])

        bb = FakeBlackboard(_base_bb_state(
            existing_dayflow_items=load_all_items(),
            completed_plan_ids=["P_complete"],
            short_id_mapping={"30": "task:c1", "31": "task:c2"},
        ))

        node = _make_node(bb)
        node.action_handler(message=None)

        for tid in ["task:c1", "task:c2"]:
            item = load_item_by_id(tid)
            meta = get_meta(item)
            assert meta["state"] == "closed", f"{tid} should be closed, got {meta['state']}"


class TestCancelledTasks:

    def test_cancelled_tasks_get_closed(self):
        seed_items([
            make_dayflow_message(
                item_id="task:cancel1", short_id=40, state="waiting",
                source_type="plan_task",
            ),
        ])

        bb = FakeBlackboard(_base_bb_state(
            existing_dayflow_items=load_all_items(),
            closed_task_ids=["40"],
            short_id_mapping={"40": "task:cancel1"},
        ))

        node = _make_node(bb)
        node.action_handler(message=None)

        item = load_item_by_id("task:cancel1")
        meta = get_meta(item)
        assert meta["state"] == "closed"


class TestAutoSynopsisSafetyNet:
    """#1 fix: a plan created implicitly via a task's plan_id (no plan_synopses emitted) must
    still get a synopsis, or it leaves no memory and a standing concern re-creates it (the HVAC
    re-research loop). Standalone tasks (plan_id=None) must NOT spawn a synopsis."""

    def test_plan_via_task_without_synopsis_gets_one(self):
        bb = FakeBlackboard(_base_bb_state(
            planned_tasks=[
                {"task_id": "1", "plan_id": "plan-ac-service",
                 "task": "web_search: Find Irvine HVAC providers", "dependencies": []},
            ],
            plan_synopses=[],   # the bug: plan introduced via task, no synopsis emitted
        ))
        _make_node(bb).action_handler(message=None)

        synopses = [i for i in load_all_items() if get_meta(i).get("source_type") == "plan_synopsis"]
        assert any(get_meta(s).get("plan_id") == "plan-ac-service" for s in synopses)

    def test_standalone_task_gets_no_synopsis(self):
        bb = FakeBlackboard(_base_bb_state(
            planned_tasks=[
                {"task_id": "1", "plan_id": None,
                 "task": "personal_admin: remind about timesheets", "dependencies": []},
            ],
            plan_synopses=[],
        ))
        _make_node(bb).action_handler(message=None)

        synopses = [i for i in load_all_items() if get_meta(i).get("source_type") == "plan_synopsis"]
        assert synopses == []   # no fake plan for a standalone task

    def test_existing_synopsis_not_duplicated(self):
        seed_items([make_plan_synopsis_message(plan_id="P_existing")])
        bb = FakeBlackboard(_base_bb_state(
            existing_dayflow_items=load_all_items(),
            planned_tasks=[
                {"task_id": "1", "plan_id": "P_existing", "task": "next step", "dependencies": []},
            ],
            plan_synopses=[],
        ))
        _make_node(bb).action_handler(message=None)

        syn = [i for i in load_all_items()
               if get_meta(i).get("source_type") == "plan_synopsis"
               and get_meta(i).get("plan_id") == "P_existing"]
        assert len(syn) == 1   # safety-net must not duplicate an existing synopsis


class TestFullRoundTrip:

    def test_finalize_then_reload_consistency(self):
        """After finalize persists everything, a fresh DB load should match."""
        bb = FakeBlackboard(_base_bb_state(
            planned_tasks=[
                {"task_id": "1", "plan_id": "P_full", "task": "Full RT task", "dependencies": []},
            ],
            plan_synopses=[{
                "plan_id": "P_full", "is_new": True, "changed": True,
                "objective": "Full round trip", "synopsis": "Complete test",
            }],
        ))

        node = _make_node(bb)
        node.action_handler(message=None)

        # Reload from DB
        items = load_all_items()
        synopses = [i for i in items if get_meta(i).get("source_type") == "plan_synopsis"]
        tasks = [i for i in items if get_meta(i).get("source_type") == "plan_task"]

        assert len(synopses) >= 1
        assert any(get_meta(s).get("plan_id") == "P_full" for s in synopses)

        assert len(tasks) >= 1
        assert all(get_meta(t).get("plan_id") == "P_full" for t in tasks)

        # Task should have a short_id
        assert all(get_meta(t).get("short_id") is not None for t in tasks)
