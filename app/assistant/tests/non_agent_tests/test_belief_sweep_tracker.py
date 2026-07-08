"""Per-domain full-sweep stamps — an interrupted multi-domain run keeps its completed domains.

The stamp file holds a `domains` map plus the legacy global `last_full_sweep_at`, which a
domain without its own entry reads (so the first per-domain run after deploy respects the
pre-existing global cadence instead of treating every domain as bootstrap).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import belief_engine.state.sweep_tracker as S


def _point_at(monkeypatch, tmp_path):
    state = tmp_path / "belief_engine_state.json"
    monkeypatch.setattr(S, "_state_path", lambda: state)
    return state


def test_bootstrap_domain_is_full(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    assert S.decide_mode(domain="routine") == "full"


def test_stamp_is_per_domain(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    S.mark_full_sweep_completed("routine")
    assert S.decide_mode(domain="routine") == "new_only"
    assert S.decide_mode(domain="health") == "full"      # untouched domain still bootstraps


def test_legacy_global_stamp_is_read_by_unstamped_domains(monkeypatch, tmp_path):
    state = _point_at(monkeypatch, tmp_path)
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    state.write_text(json.dumps({"last_full_sweep_at": recent.isoformat()}), encoding="utf-8")
    assert S.decide_mode(domain="routine") == "new_only"   # 2 days < 7: global stamp honored

    old = datetime.now(timezone.utc) - timedelta(days=10)
    state.write_text(json.dumps({"last_full_sweep_at": old.isoformat()}), encoding="utf-8")
    assert S.decide_mode(domain="routine") == "full"


def test_domain_stamp_wins_over_legacy_and_others_survive(monkeypatch, tmp_path):
    state = _point_at(monkeypatch, tmp_path)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    state.write_text(json.dumps({"last_full_sweep_at": old.isoformat()}), encoding="utf-8")

    S.mark_full_sweep_completed("routine")
    S.mark_full_sweep_completed("health")
    data = json.loads(state.read_text(encoding="utf-8"))
    assert set(data["domains"]) == {"routine", "health"}
    assert data["last_full_sweep_at"] == old.isoformat()   # legacy preserved

    assert S.decide_mode(domain="routine") == "new_only"
    assert S.decide_mode(domain="food") == "full"          # unstamped: legacy 30d -> full
