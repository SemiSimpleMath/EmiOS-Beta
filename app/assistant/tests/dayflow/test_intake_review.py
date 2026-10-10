"""The steward's stored intake reviews still decide which sources can wake waiting work.

The steward (retired 2026-10-10) reviewed intake as no action or deferred; the reviews stay on the items. A source
reviewed as no action, or deferred to a time not yet come, is not a fresh event; a malformed deferral stays visible.
"""
from datetime import datetime, timedelta, timezone

from app.assistant.dayflow_orchestrator.intake_review import review_due


def test_a_malformed_deferral_stays_visible():
    assert review_due({"evaluator_review": {"outcome": "defer", "reconsider_at": "bad"}})
    assert not review_due({"evaluator_review": {"outcome": "defer",
        "reconsider_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}})


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

