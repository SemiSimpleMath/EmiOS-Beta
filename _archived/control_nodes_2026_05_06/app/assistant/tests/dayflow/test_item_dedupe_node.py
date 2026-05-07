from app.assistant.control_nodes.item_dedupe_node import ItemDedupeNode


class _FakeBlackboard:
    def __init__(self, state):
        self._state = dict(state)

    def get_state_value(self, key, default=None):
        return self._state.get(key, default)

    def update_state_value(self, key, value):
        self._state[key] = value


def _item(item_id: str, state: str, event_type: str = "calendar_structured_event"):
    return {
        "metadata": {
            "item_id": item_id,
            "state": state,
            "event_type": event_type,
            "source_type": "calendar",
            "summary": item_id,
        }
    }


def test_item_dedupe_drops_non_new_items_from_intake_lane():
    blackboard = _FakeBlackboard(
        {
            "intake_items_new": [
                _item("calendar:new", "new"),
                _item("calendar:closed", "closed"),
                _item("calendar:watching", "watching"),
            ],
            "existing_dayflow_items": [],
        }
    )
    node = ItemDedupeNode(
        name="item_dedupe_node",
        blackboard=blackboard,
        agent_registry={},
        tool_registry={},
    )

    node.action_handler(message=None)

    deduped = blackboard.get_state_value("intake_items_deduped")
    stats = blackboard.get_state_value("intake_dedupe_stats")
    assert isinstance(deduped, list)
    assert len(deduped) == 1
    assert deduped[0]["metadata"]["item_id"] == "calendar:new"
    assert stats["dropped_non_new_state"] == 2


def test_item_dedupe_drops_items_already_triaged_in_db():
    blackboard = _FakeBlackboard(
        {
            "intake_items_new": [_item("calendar:already_triaged", "new")],
            "existing_dayflow_items": [_item("calendar:already_triaged", "important_open")],
        }
    )
    node = ItemDedupeNode(
        name="item_dedupe_node",
        blackboard=blackboard,
        agent_registry={},
        tool_registry={},
    )

    node.action_handler(message=None)

    deduped = blackboard.get_state_value("intake_items_deduped")
    stats = blackboard.get_state_value("intake_dedupe_stats")
    assert deduped == []
    assert stats["dropped_already_triaged"] == 1
