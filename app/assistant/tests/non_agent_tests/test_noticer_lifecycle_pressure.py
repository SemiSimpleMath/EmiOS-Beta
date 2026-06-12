"""Lifecycle pressure on the concerns register (2026-06-11).

Concerns must not become eternal reinforcement sinks: evidence and the
notes journal are capped, an explicit reinforcement_count feeds a
deterministic pressure rule, and the noticer's forced dispositions
(accept_chronic / re_escalate / keep_active) move concerns through the
lifecycle instead of letting them grow forever.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.subconscious.persist import (
    ADDRESSING_STALE_DAYS,
    DISPOSITION_REINFORCEMENT_THRESHOLD,
    apply_noticer_output,
    compute_pressure,
)


def _concern(cid: str, **over):
    base = {
        "concern_id": cid,
        "title": f"Concern {cid}",
        "subject": "household",
        "kind": "pattern_drift",
        "domain_tags": ["test"],
        "severity": "medium",
        "horizon": "this_week",
        "evidence": [{"kind": "chat_msg", "ref": "r0", "snippet": "origin"}],
        "addressable_by": ["dayflow_orchestrator"],
        "notes": "test concern",
        "first_observed": "2026-06-01T00:00:00+00:00",
    }
    base.update(over)
    return base


def _register(tmp_path, *, active=(), addressing=()):
    register = {
        "schema_version": 1,
        "active": list(active),
        "addressing": list(addressing),
        "resolved": [],
        "dormant": [],
    }
    path = tmp_path / "register.json"
    path.write_text(json.dumps(register), encoding="utf-8")
    return path


def _apply(tmp_path, register_path, output):
    return apply_noticer_output(
        output,
        register_path=register_path,
        tick_log_path=tmp_path / "ticks.jsonl",
    )


def _load(register_path):
    return json.loads(register_path.read_text(encoding="utf-8"))


def _reinforce(cid, n=1):
    return {
        "reinforced_concerns": [
            {
                "concern_id": cid,
                "new_evidence": [{"kind": "chat_msg", "ref": f"r{i}", "snippet": f"s{i}"}],
                "severity_change": None,
                "notes": f"note {i}",
            }
            for i in range(n)
        ]
    }


def test_reinforcement_count_and_evidence_cap(tmp_path):
    path = _register(tmp_path, active=[_concern("c1")])
    # 20 reinforcement entries in one tick — evidence must cap, counter must count.
    _apply(tmp_path, path, _reinforce("c1", 20))

    reg = _load(path)
    c = reg["active"][0]
    assert c["reinforcement_count"] == 20
    assert len(c["evidence"]) == 12  # 3 head + 9 tail
    assert c["evidence"][0]["snippet"] == "origin"  # founding evidence kept
    assert c["evidence_archived_count"] == 9  # 21 total - 12 kept
    # Journal capped to 10 entries + the archived marker.
    journal_lines = [ln for ln in c["reinforcement_notes"].splitlines() if ln.strip()]
    assert len(journal_lines) == 11
    assert "earlier notes archived" in journal_lines[0]


def test_pressure_fires_at_threshold_and_resets_on_keep_active(tmp_path):
    path = _register(tmp_path, active=[_concern("c1")])
    _apply(tmp_path, path, _reinforce("c1", DISPOSITION_REINFORCEMENT_THRESHOLD))

    reg = _load(path)
    pressure = compute_pressure(reg)
    assert [c["concern_id"] for c in pressure["needs_disposition"]] == ["c1"]

    # keep_active resets the window…
    _apply(tmp_path, path, {
        "concern_dispositions": [
            {"concern_id": "c1", "action": "keep_active", "reason": "resolution expected this week"}
        ]
    })
    reg = _load(path)
    assert compute_pressure(reg)["needs_disposition"] == []

    # …until another full window of reinforcements accrues.
    _apply(tmp_path, path, _reinforce("c1", DISPOSITION_REINFORCEMENT_THRESHOLD))
    reg = _load(path)
    assert [c["concern_id"] for c in compute_pressure(reg)["needs_disposition"]] == ["c1"]


def test_accept_chronic_moves_to_dormant_with_compact_evidence(tmp_path):
    path = _register(tmp_path, active=[_concern("c1")])
    _apply(tmp_path, path, _reinforce("c1", 10))
    _apply(tmp_path, path, {
        "concern_dispositions": [
            {"concern_id": "c1", "action": "accept_chronic", "reason": "long-term pattern, user aware"}
        ]
    })

    reg = _load(path)
    assert reg["active"] == []
    assert len(reg["dormant"]) == 1
    c = reg["dormant"][0]
    assert c["chronic"] is True
    assert c["dormant_reason"] == "long-term pattern, user aware"
    assert len(c["evidence"]) <= 4
    assert "disposition=accept_chronic" in c["reinforcement_notes"]


def test_re_escalate_returns_addressing_to_active_with_escalation(tmp_path):
    stale_since = (datetime.now(timezone.utc) - timedelta(days=ADDRESSING_STALE_DAYS + 1)).isoformat()
    path = _register(tmp_path, addressing=[_concern("c1", addressing_since_utc=stale_since)])

    reg = _load(path)
    pressure = compute_pressure(reg)
    assert [c["concern_id"] for c in pressure["addressing_stale"]] == ["c1"]

    _apply(tmp_path, path, {
        "concern_dispositions": [
            {"concern_id": "c1", "action": "re_escalate", "reason": "service window closing"}
        ]
    })
    reg = _load(path)
    assert reg["addressing"] == []
    assert len(reg["active"]) == 1
    c = reg["active"][0]
    assert c["escalation"]["urgency"] == "high"
    assert c["escalation"]["reason"] == "service window closing"
    assert "addressing_since_utc" not in c


def test_fresh_addressing_is_not_stale(tmp_path):
    recent = datetime.now(timezone.utc).isoformat()
    path = _register(tmp_path, addressing=[_concern("c1", addressing_since_utc=recent)])
    reg = _load(path)
    assert compute_pressure(reg)["addressing_stale"] == []


def test_legacy_concern_backfills_count_from_journal(tmp_path):
    # Concerns that predate reinforcement_count carry only the notes journal.
    journal = "".join(f"\n[2026-06-0{i}T00:00:00+00:00] old note {i}" for i in range(1, 8))
    path = _register(tmp_path, active=[_concern("c1", reinforcement_notes=journal)])

    _apply(tmp_path, path, _reinforce("c1", 1))
    reg = _load(path)
    assert reg["active"][0]["reinforcement_count"] == 8  # 7 journal entries + 1 new
    assert [c["concern_id"] for c in compute_pressure(reg)["needs_disposition"]] == ["c1"]
