"""#2: the noticer can move a concern active -> addressing (already handled by dayflow but not
yet resolved), so it stops nagging the planner while staying tracked. Uses a scratch concerns
store — never touches the real concerns table."""
from __future__ import annotations

from app.assistant.subconscious.persist import apply_noticer_output
from app.assistant.tests.concern_store_helpers import ScratchRegister


def _register(tmp_path, concern_id="c-ac", bucket="active") -> ScratchRegister:
    reg = {"schema_version": 1, "active": [], "addressing": [], "resolved": [], "dormant": []}
    reg[bucket].append({
        "concern_id": concern_id, "title": "AC service should be handled",
        "subject": "household", "kind": "anticipated_need", "domain_tags": ["home"],
        "severity": "high", "horizon": "this_month", "evidence": [],
        "addressable_by": ["dayflow_orchestrator"], "notes": "no scheduling visible",
        "first_observed": "2026-06-08T04:00:00+00:00",
        "last_reinforced_utc": "2026-06-08T04:00:00+00:00",
    })
    return ScratchRegister(tmp_path).write(reg)


def _ids(reg, bucket):
    return [c["concern_id"] for c in reg.get(bucket, [])]


def test_addressing_moves_active_to_addressing(tmp_path):
    store = _register(tmp_path, "c-ac")
    summary = apply_noticer_output(
        {"addressing_concerns": [
            {"concern_id": "c-ac", "notes": "dayflow already researched providers", "evidence": []}
        ]},
        connect=store.connect, tick_log_path=store.tick_log,
    )
    reg = store.read()
    assert "c-ac" not in _ids(reg, "active")          # left active → stops nagging the planner
    assert "c-ac" in _ids(reg, "addressing")          # tracked as being handled
    assert reg["addressing"][0].get("addressing_since_utc")
    assert "c-ac" not in _ids(reg, "resolved")        # NOT resolved — need still stands
    assert summary["addressing_count"] == 1


def test_addressing_unknown_concern_is_ignored(tmp_path):
    store = _register(tmp_path, "c-ac")
    apply_noticer_output(
        {"addressing_concerns": [{"concern_id": "nope", "evidence": []}]},
        connect=store.connect, tick_log_path=store.tick_log,
    )
    reg = store.read()
    assert _ids(reg, "active") == ["c-ac"]            # untouched
    assert _ids(reg, "addressing") == []
