"""Prose resolution closes a cluster only when the manager completed it (2026-10-05).

Until then the route marked the lead and every sibling executed whatever happened: a crashed, aborted or report-less
manager run, or a partial one with open questions, cleared the whole cluster from the dashboard. Pins: the resolver
raises on a missing lead, a failed or aborted manager run and a run with no report; the route answers 500 and closes
nothing then; a partial resolution closes nothing; a completed one closes the lead and cascades.

Uses the kg conftest (isolated test DB). Fake manager; invented data only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

from app.assistant.kg_resolution import resolve_with_prose as R
from app.assistant.utils.pydantic_classes import ToolResult


class _Mgr:
    def __init__(self, report):
        msgs = [] if report is None else [SimpleNamespace(sender="kg_resolution::final_answer", data=report)]
        self.blackboard = SimpleNamespace(get_messages=lambda: msgs)


@pytest.fixture
def manager(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    state = {"mgr": _Mgr({"completed": True, "summary": "done"}), "result": ToolResult(result_type="final_answer",
                                                                                      content="ok")}
    monkeypatch.setattr(R, "_build_brief", lambda **k: {"task": "t", "information": "i"})
    monkeypatch.setattr(R, "_resolution_scope", lambda: None)
    monkeypatch.setattr(DI.multi_agent_manager_factory, "create_manager", lambda name: state["mgr"])

    def invoke(mgr, msg):
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]
    monkeypatch.setattr(DI.manager_invoker, "invoke", invoke)
    return state


def test_the_resolver_raises_on_a_failed_aborted_or_reportless_run(manager, monkeypatch):
    manager["result"] = RuntimeError("Connection error.")
    with pytest.raises(RuntimeError, match="Connection error"):
        R.resolve_cluster_with_prose(lead_finding_id="L", prose="p")
    manager["result"] = ToolResult(result_type="manager_aborted", content="budget exhausted")
    with pytest.raises(RuntimeError, match="aborted"):
        R.resolve_cluster_with_prose(lead_finding_id="L", prose="p")
    manager["result"] = ToolResult(result_type="final_answer", content="ok")
    manager["mgr"] = _Mgr(None)
    with pytest.raises(RuntimeError, match="without a report"):
        R.resolve_cluster_with_prose(lead_finding_id="L", prose="p")
    monkeypatch.setattr(R, "_build_brief", lambda **k: None)
    with pytest.raises(LookupError):
        R.resolve_cluster_with_prose(lead_finding_id="L", prose="p")


def _post(monkeypatch, outcome):
    from app.routes import kg_maintenance as route
    closed = []
    monkeypatch.setattr(route, "get_finding", lambda fid: {"id": fid, "superseded_by": None})
    monkeypatch.setattr(route, "set_status", lambda fid, status, **k: closed.append((fid, status, k)) or 3)

    class _S:
        def query(self, *a):
            return self

        def filter_by(self, **k):
            return self

        def first(self):
            return SimpleNamespace(evidence_json={})

        def commit(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr("app.models.base.get_session", lambda: _S())
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a: None)

    def resolve(**k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(R, "resolve_cluster_with_prose", resolve)
    app = Flask(__name__)
    app.register_blueprint(route.kg_maintenance_bp)
    resp = app.test_client().post("/kg-maintenance/api/finding/L/resolve_with_prose", json={"prose": "p"})
    return resp, closed


def test_a_failed_resolution_closes_nothing(monkeypatch):
    resp, closed = _post(monkeypatch, RuntimeError("Connection error."))
    assert resp.status_code == 500 and "nothing was closed" in resp.get_json()["error"] and closed == []


def test_a_partial_resolution_closes_nothing_and_a_completed_one_cascades(monkeypatch):
    resp, closed = _post(monkeypatch, {"status": "partial", "lead_id": "L", "report": {"open_questions": ["?"]}})
    assert resp.status_code == 200 and closed == [] and resp.get_json()["siblings_cascaded"] == 0
    resp, closed = _post(monkeypatch, {"status": "completed", "lead_id": "L", "report": {"completed": True}})
    assert [(f, s) for f, s, _ in closed] == [("L", "executed")] and closed[0][2]["cascade_to_siblings"] is True
