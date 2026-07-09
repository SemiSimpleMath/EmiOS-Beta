"""Status-file writer discipline (routine audit R4, 2026-07-08).

resource_routine_status.json had two read-modify-write writers under
DIFFERENT locks (RoutineManager._state_lock vs routines_admin's
_CONFIG_LOCK) — interleaving lost one side's write. And the admin toggle
left auto_disabled_reason behind on re-enable, so the next run finalized
as a recovery PROBE: its failure path silently bumped a timestamp
instead of counting toward auto-disable and ticketing.

Now every status write routes through RoutineManager._mutate_status_file
(one lock), and set_routine_enabled(True) clears the auto-disable
residue.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from app.assistant.routine_manager.routine_manager import RoutineManager


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.assistant.routine_manager.routine_manager.get_resources_dir",
        lambda: tmp_path,
    )
    # Keep test toggles/disables out of the real data/routine_decisions log.
    monkeypatch.setattr(
        "app.assistant.routine_manager.decision_log.record",
        lambda **kw: None,
    )
    m = RoutineManager.__new__(RoutineManager)
    m._state_lock = threading.Lock()
    return m


def _status(tmp_path) -> dict:
    return json.loads((tmp_path / "resource_routine_status.json").read_text(encoding="utf-8"))


def test_reenable_clears_auto_disable_residue(mgr, tmp_path):
    mgr._disable_routine_in_status("fetch_weather", reason="3 straight timeouts")
    entry = _status(tmp_path)["routines"]["fetch_weather"]
    assert entry["enabled"] is False
    assert entry["auto_disabled_reason"] == "3 straight timeouts"
    assert entry["auto_disabled_at_utc"]

    mgr.set_routine_enabled("fetch_weather", True)
    entry = _status(tmp_path)["routines"]["fetch_weather"]
    assert entry["enabled"] is True
    assert entry["auto_disabled_reason"] is None      # no stale probe classification
    assert entry["auto_disabled_at_utc"] is None
    assert entry["consecutive_failures"] == 0          # fresh failure budget
    assert entry["next_attempt_after_utc"] is None     # no backoff residue


def test_disable_toggle_keeps_run_state_fields(mgr, tmp_path):
    (tmp_path / "resource_routine_status.json").write_text(
        json.dumps({"routines": {"r1": {"last_status": "success", "run_count": 7}}}),
        encoding="utf-8",
    )
    mgr.set_routine_enabled("r1", False)
    entry = _status(tmp_path)["routines"]["r1"]
    assert entry["enabled"] is False
    assert entry["run_count"] == 7                     # merge, not clobber
    assert "auto_disabled_reason" not in entry         # user disable ≠ auto disable


def test_probe_bump_only_moves_timestamp(mgr, tmp_path):
    mgr._disable_routine_in_status("r2", reason="boom")
    before = _status(tmp_path)["routines"]["r2"]["auto_disabled_at_utc"]

    later = datetime(2027, 1, 1, tzinfo=timezone.utc)
    mgr._bump_probe_timestamp_in_status("r2", later)
    entry = _status(tmp_path)["routines"]["r2"]
    assert entry["auto_disabled_at_utc"] == later.isoformat() != before
    assert entry["enabled"] is False
    assert entry["auto_disabled_reason"] == "boom"


def test_probe_success_recovery_reenables(mgr, tmp_path):
    mgr._disable_routine_in_status("r3", reason="boom")
    mgr._clear_auto_disable_in_status("r3", run_id="abc123")
    entry = _status(tmp_path)["routines"]["r3"]
    assert entry["enabled"] is True
    assert entry["auto_disabled_reason"] is None
    assert entry["consecutive_failures"] == 0
