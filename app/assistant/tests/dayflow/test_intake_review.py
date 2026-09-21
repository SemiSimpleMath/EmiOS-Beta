"""Evaluator dispositions preserve source history and retire all source types equally."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jinja2 import Environment

from app.assistant.tests.dayflow.conftest import FakeBlackboard, make_dayflow_message, seed_items, load_item_by_id, get_meta
from app.assistant.dayflow_orchestrator.state_store import load_admitted_intake
from app.assistant.dayflow_orchestrator.intake_review import prepare_reviews, review_due, wake_intake_eligible
from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_intake_reviews, write_dayflow_item
from app.assistant.control_nodes.strategic_planner_wo_persist_node import StrategicPlannerWoPersistNode


def seed(source="pod", item_id="source:1", short_id=1):
    seed_items([make_dayflow_message(item_id=item_id, short_id=short_id, state="artifact",
        source_type=source, summary="Already handled", extra_meta={"evaluator_pending": True,
        "pod_id": "datapod:source", "email_body_excerpt": "Original source detail"})])
    return next(i for i in load_admitted_intake() if i["metadata"]["item_id"] == item_id)


def review(item="1", outcome="no_action", **kwargs):
    return {"item_id": item, "outcome": outcome, "reason": "Already handled", **kwargs}


def board(items, reviews=None, specs=None):
    return FakeBlackboard({"admitted_artifacts": items, "intake_reviews": reviews or [],
        "new_or_changed": specs or [],
        "intake_review_snapshots": {i["metadata"]["item_id"]: deepcopy(i["metadata"]) for i in items}})


def persist(bb):
    StrategicPlannerWoPersistNode(name="persist", blackboard=bb, agent_registry={}, tool_registry={}).action_handler(None)


@pytest.mark.parametrize("source", ["email", "pod", "chat", "user_request", "delegation", "calendar", "future_source"])
def test_no_action_retires_every_source_without_deleting_content(source):
    item = seed(source)
    persist(board([item], [review()]))
    meta = get_meta(load_item_by_id("source:1"))
    assert meta["state"] == "closed" and meta["evaluator_pending"] is False
    assert meta["evaluator_review"]["reason"] == "Already handled"
    for key in ("pod_id", "email_body_excerpt", "summary", "created_at", "source_type"):
        assert meta[key] == item["metadata"][key]
    assert load_admitted_intake() == []
    assert not wake_intake_eligible(meta)


def test_deferred_camera_pod_returns_when_due_then_can_close():
    item = seed()
    due = datetime.now(timezone.utc) + timedelta(hours=1)
    persist(board([item], [review(outcome="defer", reason="Scheduled status expected", reconsider_at=due.isoformat())]))
    assert load_admitted_intake() == []
    assert not wake_intake_eligible(get_meta(load_item_by_id("source:1")))
    returned = load_admitted_intake(now_utc=due)
    assert len(returned) == 1
    assert wake_intake_eligible(returned[0]["metadata"], due)
    persist(board(returned, [review()]))
    assert load_admitted_intake(now_utc=due) == []


@pytest.mark.parametrize("reviews", [[], [review("unseen")], [review(), review()],
    [review(reason=" ")], [review(outcome="other")], [review(reconsider_at="2027-01-01T00:00:00Z")],
    [review(outcome="defer")], [review(outcome="defer", reconsider_at="2027-01-01")],
    [review(outcome="defer", reconsider_at="2020-01-01T00:00:00Z")]])
def test_invalid_or_missing_decisions_do_not_discard_intake(reviews):
    item = seed()
    with pytest.raises(ValueError):
        persist(board([item], reviews))
    assert len(load_admitted_intake()) == 1


def test_conflicting_handoff_rejected_before_work_creation(monkeypatch):
    from app.assistant.dayflow_orchestrator import work_persist
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid review reached work persistence")
    monkeypatch.setattr(work_persist, "persist_steward_output", forbidden)
    item = seed()
    with pytest.raises(ValueError):
        persist(board([item], [review()], [{"objective": "Act", "based_on": ["1"]}]))


def test_failed_handoff_preserves_pending_source(monkeypatch):
    from app.assistant.dayflow_orchestrator import work_persist
    def fail(*args, **kwargs):
        raise OSError("Store unavailable")
    monkeypatch.setattr(work_persist, "persist_steward_output", fail)
    item = seed()
    with pytest.raises(OSError):
        persist(board([item], specs=[{"objective": "Act", "based_on": ["1"]}]))
    assert len(load_admitted_intake()) == 1


def test_review_batch_rolls_back_if_later_source_changed():
    items = [seed(), seed(item_id="source:2", short_id=2)]
    snapshots = {i["metadata"]["item_id"]: deepcopy(i["metadata"]) for i in items}
    reviews = prepare_reviews(items, [], [review(), review("2")])
    write_dayflow_item("source:2", updates={"summary": "New urgent information"}, caller="test")
    with pytest.raises(ValueError, match="changed since preparation"):
        write_intake_reviews(reviews, snapshots)
    assert len(load_admitted_intake()) == 2
    assert get_meta(load_item_by_id("source:2"))["summary"] == "New urgent information"


def test_handoff_still_preserves_source_and_is_available_to_event_matcher():
    item = seed()
    bb = board([item], specs=[{"objective": "Inspect current situation", "based_on": ["1"]}])
    persist(bb)
    meta = get_meta(load_item_by_id("source:1"))
    assert meta["evaluator_review"]["outcome"] == "transferred"
    assert not meta["evaluator_pending"] and wake_intake_eligible(meta)
    assert load_admitted_intake() == []
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    wo = get_dayflow_work_store().load(bb.get_state_value("steward_persist_result")["created"][0]["work_id"])
    assert wo.constraints["source_intake"][0]["pod_id"] == "datapod:source"


def test_prep_does_not_resurrect_closed_or_deferred_tick_memory(monkeypatch):
    from app.assistant.control_nodes.strategic_planner_wo_prep_node import StrategicPlannerWoPrepNode
    closed = seed()
    deferred = seed(item_id="source:2", short_id=2)
    pending = seed(item_id="source:3", short_id=3)
    persist(board([closed, deferred], [review(), review("2", outcome="defer",
        reconsider_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())]))
    pending["metadata"]["enrichment"] = "Useful current-cycle detail"
    bb = board([closed, deferred, pending])
    monkeypatch.setattr(StrategicPlannerWoPrepNode, "_build_situational_context", lambda self: None)
    StrategicPlannerWoPrepNode(name="prep", blackboard=bb, agent_registry={}, tool_registry={}).action_handler(None)
    remaining = bb.get_state_value("admitted_artifacts")
    assert [i["metadata"]["item_id"] for i in remaining] == ["source:3"]
    assert remaining[0]["metadata"]["enrichment"] == "Useful current-cycle detail"
    assert "enrichment" not in bb.get_state_value("intake_review_snapshots")["source:3"]


def test_malformed_deferral_stays_visible_and_old_unreviewed_is_not_dropped():
    assert review_due({"evaluator_review": {"outcome": "defer", "reconsider_at": "bad"}})
    seed_items([make_dayflow_message(item_id="old", state="artifact", source_type="pod",
        created_at=datetime.now(timezone.utc) - timedelta(days=90), extra_meta={"evaluator_pending": True})])
    assert len(load_admitted_intake()) == 1


def test_prompt_shows_full_camera_source_and_previous_deferral():
    path = Path(__file__).resolve().parents[2] / "agents/dayflow_orchestrator/strategic_planner_wo/prompts/user.j2"
    rendered = Environment().from_string(path.read_text(encoding="utf-8")).render(admitted_artifacts=[{
        "metadata": {"source_type": "pod", "pod_id": "datapod:camera", "summary": "Motion",
            "evaluator_review": {"outcome": "defer", "reason": "Expected status update", "reconsider_at": "2026-09-20T22:00:00Z"}}}])
    assert "datapod:camera" in rendered and "Expected status update" in rendered
    assert "reconsideration due: 2026-09-20T22:00:00Z" in rendered


def test_state_mover_excludes_retired_and_held_sources_but_keeps_transfers():
    from app.assistant.tests.dayflow.test_external_source_context import scenario, prepare, email
    _, wid = scenario()
    items = [email(name, "Event evidence") for name in ("retired", "held", "transferred", "fresh")]
    items[0]["metadata"]["evaluator_review"] = {"outcome": "no_action"}
    items[1]["metadata"]["evaluator_review"] = {"outcome": "defer",
        "reconsider_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    items[2]["metadata"]["evaluator_review"] = {"outcome": "transferred"}
    bb = prepare(wid, items)
    assert [s["item_id"] for s in bb.get_state_value("work_wait_intake")] == ["transferred", "fresh"]


def test_empty_work_objective_cannot_count_as_transfer():
    item = seed()
    with pytest.raises(ValueError, match="nonempty work objective"):
        persist(board([item], specs=[{"objective": " ", "based_on": ["1"]}]))
    assert len(load_admitted_intake()) == 1
