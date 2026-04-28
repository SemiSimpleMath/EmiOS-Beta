"""Tests for TriageSpawnGuardNode — validates triage LLM decisions."""
from __future__ import annotations

from app.assistant.control_nodes.triage_spawn_guard_node import TriageSpawnGuardNode
from app.assistant.tests.dayflow.conftest import FakeBlackboard


def _make_node(bb: FakeBlackboard) -> TriageSpawnGuardNode:
    return TriageSpawnGuardNode(
        name="triage_spawn_guard_node",
        blackboard=bb,
        agent_registry={},
        tool_registry={},
    )


def _eligible_item(item_id: str, short_id: int, summary: str = "Test"):
    return {
        "metadata": {
            "item_id": item_id,
            "short_id": short_id,
            "state": "new",
            "source_type": "email",
            "summary": summary,
        }
    }


class TestTriageSpawnGuardNode:

    def test_admit_decision_produces_admitted_artifact(self):
        bb = FakeBlackboard({
            "eligible_items_now": [_eligible_item("email:e1", 1, "Important email")],
            "artifact_decisions": [
                {"artifact_id": "1", "decision": "ADMIT", "reason": "Relevant"},
            ],
            "auto_admitted_artifacts": [],
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        admitted = bb.get_state_value("admitted_artifacts")
        assert isinstance(admitted, list)
        assert len(admitted) >= 1
        # The admitted artifact should have state=artifact
        assert admitted[0]["metadata"]["state"] == "artifact"

    def test_reject_decision_not_admitted(self):
        bb = FakeBlackboard({
            "eligible_items_now": [_eligible_item("email:e2", 2, "Spam")],
            "artifact_decisions": [
                {"artifact_id": "2", "decision": "REJECT_POLICY", "reason": "Spam"},
            ],
            "auto_admitted_artifacts": [],
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        admitted = bb.get_state_value("admitted_artifacts")
        assert isinstance(admitted, list)
        assert len(admitted) == 0

    def test_auto_admitted_merged_in(self):
        """Auto-admitted items should be merged even when there are eligible items."""
        auto = [{
            "id": "cal:auto1",
            "metadata": {
                "item_id": "cal:auto1",
                "state": "artifact",
                "source_type": "calendar",
                "summary": "Auto-admitted calendar event",
            },
        }]
        # Must have at least one eligible item + decision, otherwise the
        # guard node early-returns before checking auto_admitted_artifacts.
        bb = FakeBlackboard({
            "eligible_items_now": [_eligible_item("email:e3", 3, "Some email")],
            "artifact_decisions": [
                {"artifact_id": "3", "decision": "ADMIT", "reason": "Ok"},
            ],
            "auto_admitted_artifacts": auto,
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        admitted = bb.get_state_value("admitted_artifacts")
        ids = [a["metadata"]["item_id"] for a in admitted]
        assert "cal:auto1" in ids
        assert "email:e3" in ids

    def test_auto_admitted_preserved_when_no_eligible_items(self):
        """Auto-admitted artifacts must survive even when eligible_items_now is empty."""
        auto = [{
            "id": "cal:auto_kept",
            "metadata": {
                "item_id": "cal:auto_kept",
                "state": "artifact",
                "source_type": "calendar",
                "summary": "This should not be lost",
            },
        }]
        bb = FakeBlackboard({
            "eligible_items_now": [],
            "artifact_decisions": [],
            "auto_admitted_artifacts": auto,
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        admitted = bb.get_state_value("admitted_artifacts")
        assert len(admitted) == 1
        assert admitted[0]["metadata"]["item_id"] == "cal:auto_kept"

    def test_empty_decisions_is_safe(self):
        bb = FakeBlackboard({
            "eligible_items_now": [],
            "artifact_decisions": [],
            "auto_admitted_artifacts": [],
            "short_id_mapping": {},
        })

        node = _make_node(bb)
        node.action_handler(message=None)

        admitted = bb.get_state_value("admitted_artifacts")
        assert admitted == [] or admitted is not None
