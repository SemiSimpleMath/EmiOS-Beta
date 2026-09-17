"""StateMoverPersistNode — applies the state_mover's two outputs to the graph.

The node used to sit behind state_transition_guard_node and set a
``state_mutations_persisted_tf`` handshake so post_room would not re-apply the state_mover's item
mutations. The item lane is retired (2026-09-16) and the state_mover no longer emits mutations, so
the guard and the handshake are gone. What remains is the work-object half, which is the whole job:
wake nodes whose awaited event arrived, promote every ready node, park the few the LLM held.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.state_mover_persist_node import StateMoverPersistNode
from app.assistant.tests.dayflow.conftest import (
    FakeBlackboard,
    get_meta,
    load_item_by_id,
    make_dayflow_message,
    seed_items,
)


def _make_node(bb: FakeBlackboard) -> StateMoverPersistNode:
    return StateMoverPersistNode(
        name="state_mover_persist_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _wo_with_node(store, *, node_id, wake_kind=None, wake_ref=None, title="Do the thing"):
    wo = store.apply("create_work_object", {
        "title": title, "goal_content": title,
        "satisfied_when_kind": "all_owned_children_done",
    })
    store.apply("add_node", {"work_id": wo.id, "id": node_id, "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": title, "content": title})
    if wake_kind:
        store.apply("defer_node", {"work_id": wo.id, "node_id": node_id,
                                   "wake_kind": wake_kind, "wake_at": None, "wake_ref": wake_ref})
    return wo.id


class TestStateMoverPersistNode:

    def test_ready_node_is_promoted(self):
        """Promotion is the safe default: anything ready that the LLM did not hold goes
        `actionable` so the action_selector can dispatch it."""
        store = _store()
        wid = _wo_with_node(store, node_id="ready1")

        bb = FakeBlackboard()
        _make_node(bb).action_handler(message=None)

        assert store.load(wid).nodes["ready1"].status == "actionable"

    def test_held_node_is_parked_not_promoted(self):
        """A hold is the state_mover's one veto — the node waits for its reactivate_at
        instead of reaching the user at a bad moment."""
        store = _store()
        wid = _wo_with_node(store, node_id="held1", title="Nudge the user")
        wake = (datetime.now(timezone.utc) + timedelta(hours=9)).isoformat()

        bb = FakeBlackboard({"held_work_nodes": [
            {"task_id": f"{wid}::held1", "hold_reason": "quiet hours until 07:00",
             "reactivate_at": wake},
        ]})
        _make_node(bb).action_handler(message=None)

        node = store.load(wid).nodes["held1"]
        assert node.status == "waiting"
        assert node.wake_at is not None and node.wake_at > datetime.now(timezone.utc)

    def test_node_wake_clears_the_event_wait(self):
        """The state_mover's real judgment: the awaited thing arrived. Clearing the wait is
        what lets is_ready pick the node up again."""
        store = _store()
        wid = _wo_with_node(store, node_id="parked1", wake_kind="event",
                            wake_ref="the school replies with the packet details")

        bb = FakeBlackboard({"node_wakes": [
            {"task_id": f"{wid}::parked1", "evidence": "School emailed: packets due Friday."},
        ]})
        _make_node(bb).action_handler(message=None)

        node = store.load(wid).nodes["parked1"]
        assert node.wake_kind != "event", "the event wait must be cleared once it arrived"

    def test_db_mutation_visible_after_persist(self):
        """If we mutate DB directly, subsequent DB reads reflect the change."""
        seed_items([
            make_dayflow_message(item_id="task:before", state="actionable", short_id=1),
        ])

        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        write_dayflow_item("task:before", state="dispatched", reason="test", caller="test")

        item = load_item_by_id("task:before")
        assert get_meta(item)["state"] == "dispatched"
