"""Release regressions for WO6 and WO9; graph writes use an in-memory store."""
from datetime import datetime, timezone

import pytest
from flask import Flask
from work_objects.store import WorkStore


@pytest.fixture
def store():
    instance = WorkStore(":memory:")
    yield instance
    instance.close()


@pytest.mark.parametrize("value", ["not-a-date", "", {"date": "2099-01-01"}])
def test_invalid_defer_leaves_graph_and_events_unchanged(store, value):
    wo = store.apply("create_work_object", {"title": "wait"})
    before = store.load(wo.id).model_dump(mode="json")
    events = store.events(wo.id)
    with pytest.raises(ValueError, match="wake_at"):
        store.apply("defer_node", {"work_id": wo.id, "node_id": wo.goal_node_id,
                                  "wake_kind": "time", "wake_at": value})
    assert store.load(wo.id).model_dump(mode="json") == before
    assert store.events(wo.id) == events


@pytest.mark.parametrize("value", ["2099-01-01T00:00:00Z", "2099-01-01T02:00:00+02:00",
                                   "2099-01-01T00:00:00", datetime(2099, 1, 1)])
def test_defer_round_trips_aware_timestamp_and_can_be_cleared(store, value):
    wo = store.apply("create_work_object", {"title": "wait"})
    data = {"work_id": wo.id, "node_id": wo.goal_node_id, "wake_kind": "time", "wake_at": value}
    store.apply("defer_node", data)
    loaded = store.load(wo.id)
    assert loaded.nodes[wo.goal_node_id].wake_at == datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert not loaded.is_ready(loaded.nodes[wo.goal_node_id])
    store.apply("defer_node", {**data, "wake_kind": None, "wake_at": None})
    assert store.load(wo.id).nodes[wo.goal_node_id].wake_at is None


def test_ui_abandon_records_reason_and_cascades_unstarted_work(store, monkeypatch):
    from work_objects.ui import blueprint
    wo = store.apply("create_work_object", {"title": "manual close"})
    store.apply("add_node", {"work_id": wo.id, "id": "child", "type": "subtask",
                             "parent_id": wo.goal_node_id})
    monkeypatch.setattr(blueprint, "_get_store", lambda: store)
    app = Flask(__name__)
    app.register_blueprint(blueprint.work_ui_bp)
    response = app.test_client().post(f"/api/work/{wo.id}/abandon")
    assert response.status_code == 200, response.get_json()
    loaded = store.load(wo.id)
    assert loaded.status == "abandoned"
    assert loaded.nodes["child"].status == "abandoned"
    assert loaded.constraints["terminal"]["reason"] == "owner abandoned via /work"
