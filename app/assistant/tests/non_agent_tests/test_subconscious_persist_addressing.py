"""#2: the noticer can move a concern active -> addressing (already handled by dayflow but not
yet resolved), so it stops nagging the planner while staying tracked. Uses a temp register —
never touches the real resource_concerns_register.json."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.assistant.subconscious.persist import apply_noticer_output


def _temp_register(concern_id="c-ac", bucket="active") -> Path:
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd)
    reg = {"schema_version": 1, "active": [], "addressing": [], "resolved": [], "dormant": []}
    reg[bucket].append({
        "concern_id": concern_id, "title": "AC service should be handled",
        "subject": "household", "kind": "anticipated_need", "domain_tags": ["home"],
        "severity": "high", "horizon": "this_month", "evidence": [],
        "addressable_by": ["dayflow_orchestrator"], "notes": "no scheduling visible",
        "first_observed": "2026-06-08T04:00:00+00:00",
        "last_reinforced_utc": "2026-06-08T04:00:00+00:00",
    })
    Path(path).write_text(json.dumps(reg), encoding="utf-8")
    return Path(path)


def _ids(reg, bucket):
    return [c["concern_id"] for c in reg.get(bucket, [])]


def _tick():
    fd, p = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
    return Path(p)


def test_addressing_moves_active_to_addressing():
    reg_path, tick = _temp_register("c-ac"), _tick()
    summary = apply_noticer_output(
        {"addressing_concerns": [
            {"concern_id": "c-ac", "notes": "dayflow already researched providers", "evidence": []}
        ]},
        register_path=reg_path, tick_log_path=tick,
    )
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    assert "c-ac" not in _ids(reg, "active")          # left active → stops nagging the planner
    assert "c-ac" in _ids(reg, "addressing")          # tracked as being handled
    assert reg["addressing"][0].get("addressing_since_utc")
    assert "c-ac" not in _ids(reg, "resolved")        # NOT resolved — need still stands
    assert summary["addressing_count"] == 1
    os.remove(reg_path); os.remove(tick)


def test_addressing_unknown_concern_is_ignored():
    reg_path, tick = _temp_register("c-ac"), _tick()
    apply_noticer_output(
        {"addressing_concerns": [{"concern_id": "nope", "evidence": []}]},
        register_path=reg_path, tick_log_path=tick,
    )
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    assert _ids(reg, "active") == ["c-ac"]            # untouched
    assert _ids(reg, "addressing") == []
    os.remove(reg_path); os.remove(tick)
