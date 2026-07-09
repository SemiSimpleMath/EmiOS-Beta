"""Fix pair from routine audit R5+R6 (2026-07-08).

R5: the admin save loop rewrote EVERY routine's config file on any
single-routine policy patch — one UI edit re-serialized ~50 tracked
files (the 36-modified-files git churn). Saving now writes only the
patched routine's file.

R6: the weekly policy's dynamic skip reason ("not the scheduled day
(today=Wednesday)") slipped past the boring-skip frozenset — 7.7k
decision-log rows/day of pure noise.
"""
from __future__ import annotations

import json

from app.assistant.routine_manager.decision_log import _is_interesting_skip
from app.routes.routines_admin import _save_routine_entry


# ── R6: boring-skip filter ────────────────────────────────────────


def test_weekly_day_mismatch_is_boring():
    assert _is_interesting_skip("not the scheduled day (today=Wednesday)") is False
    assert _is_interesting_skip("Not the scheduled day (today=Sunday)") is False


def test_actionable_skips_stay_interesting():
    assert _is_interesting_skip("backoff after failure (10s remaining, attempt 2)") is True
    assert _is_interesting_skip("user potentially afk") is True
    assert _is_interesting_skip("feature 'weather' disabled or missing keys") is True


def test_exact_boring_reasons_still_boring():
    assert _is_interesting_skip("already succeeded today") is False
    assert _is_interesting_skip("interval not reached") is False


# ── R5: single-file save ──────────────────────────────────────────


def test_save_writes_only_the_patched_routines_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routes.routines_admin.get_configs_dir", lambda: tmp_path)
    public = tmp_path / "routines" / "public"
    public.mkdir(parents=True)
    a = public / "alpha.json"
    b = public / "beta.json"
    a.write_text(json.dumps({"id": "alpha", "run_policy": {"type": "interval"}}), encoding="utf-8")
    b.write_text("UNTOUCHED-SENTINEL", encoding="utf-8")

    entry = {
        "id": "alpha",
        "run_policy": {"type": "interval", "min_interval_seconds": 300},
        "_source_file": str(a),
    }
    _save_routine_entry(entry)

    saved = json.loads(a.read_text(encoding="utf-8"))
    assert saved["run_policy"]["min_interval_seconds"] == 300
    assert "_source_file" not in saved
    assert b.read_text(encoding="utf-8") == "UNTOUCHED-SENTINEL"  # sibling not re-serialized


def test_save_without_source_file_goes_private(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routes.routines_admin.get_configs_dir", lambda: tmp_path)
    _save_routine_entry({"id": "newbie", "runner": "function"})
    written = tmp_path / "routines" / "private" / "newbie.json"
    assert json.loads(written.read_text(encoding="utf-8"))["id"] == "newbie"
