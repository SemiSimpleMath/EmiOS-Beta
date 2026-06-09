"""Reliability spine R4: the local-only /api/system/health surface.

Asserts the gate (loopback only; non-loopback and tunneled refused) and the aggregation logic:
heartbeats + background-task liveness + db stats are reported, and status flips to "degraded" when a
heartbeat shows consecutive errors or a should-be-running task's thread is dead. The two heavy
accessors are faked for determinism; heartbeats are seeded for real (they're the unit under test).
"""
from __future__ import annotations

import pytest
from flask import Flask

import app.services.scheduler_heartbeat as hb

_BTM_PATH = "app.assistant.background_task_manager.background_task_manager.get_background_task_manager"
_DBM_PATH = "app.models.db_manager.get_db_manager"


class _FakeBTM:
    def __init__(self, tasks):
        self._tasks = tasks

    def get_status(self):
        return {"started": True, "task_count": len(self._tasks), "tasks": self._tasks}


class _FakeDBM:
    def stats(self):
        return {"writes_attempted": 5, "writes_succeeded": 5, "writes_rolled_back": 0}


@pytest.fixture
def client(monkeypatch):
    hb.reset()
    monkeypatch.setattr(_BTM_PATH, lambda: _FakeBTM(
        {"routine_runner": {"should_be_running": True, "thread_alive": True}}))
    monkeypatch.setattr(_DBM_PATH, lambda: _FakeDBM())
    from app.routes.health_check import health_check_bp
    app = Flask(__name__)
    app.register_blueprint(health_check_bp)
    return app.test_client()


def _get(client, **kw):
    env = {"REMOTE_ADDR": "127.0.0.1"}
    return client.get("/api/system/health", environ_overrides=env, **kw)


def test_loopback_ok_reports_all_sections(client):
    hb.record_tick("dayflow_scheduler", ok=True)
    r = _get(client)
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok"
    assert data["degraded_reasons"] == []
    assert "dayflow_scheduler" in data["heartbeats"]
    assert "db_writer" in data and "background_tasks" in data and "process" in data


def test_degraded_when_heartbeat_has_consecutive_errors(client):
    hb.record_tick("dayflow_scheduler", ok=False, error=RuntimeError("boom"))
    data = _get(client).get_json()
    assert data["status"] == "degraded"
    assert any("dayflow_scheduler" in reason for reason in data["degraded_reasons"])


def test_degraded_when_background_task_thread_dead(client, monkeypatch):
    monkeypatch.setattr(_BTM_PATH, lambda: _FakeBTM(
        {"routine_runner": {"should_be_running": True, "thread_alive": False}}))
    data = _get(client).get_json()
    assert data["status"] == "degraded"
    assert any("routine_runner" in reason for reason in data["degraded_reasons"])


def test_tunneled_request_blocked(client):
    r = _get(client, headers={"CF-Connecting-IP": "203.0.113.5"})
    assert r.status_code == 403


def test_non_loopback_blocked(client):
    r = client.get("/api/system/health", environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
    assert r.status_code == 403
