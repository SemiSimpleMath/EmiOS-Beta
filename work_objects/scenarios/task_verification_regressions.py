"""Regression pins for the 2026-07-11 adversarial-verification findings (task-runtime core).

Each test reproduces a probe-confirmed defect and asserts the fixed behavior:
  1. multi-wait: the first delivered event must NOT abandon the other pending waits
     (waits' conditions are RELEASE conditions, not branch guards) — was: w2=abandoned,
     work=done after one event.
  2. AND-gate wait: promoted on the first of two required events, the gate re-parks
     (released only when the whole condition holds) — was: abandoned.
  3. run-twice: the same template starts twice in one store (instance-namespaced node
     ids) — was: ValueError id collision + a leaked goal-only active WO.
  4. unrunnable guard: a malformed guard fails the NODE loudly; the drive completes with
     'failed' — was: the whole drive crashed on every attempt including resume.

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_verification_regressions.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402  bootstrap DI before project imports

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore
from app.assistant.task_runtime.entry import start_task_run, resume_task_run
from app.assistant.task_runtime.task_store import build_task_scope


class _FakeTool:
    def execute(self, msg):
        args = (getattr(msg, "tool_data", {}) or {}).get("arguments", {}) or {}
        return ToolResult(content=str(args.get("emit", "ok")), result_type="success", data={})


def _install():
    orig_cls, orig_cfg = DI.tool_registry.get_tool_class, DI.tool_registry.get_tool
    DI.tool_registry.get_tool_class = lambda n: _FakeTool if str(n).startswith("fake_") else orig_cls(n)
    DI.tool_registry.get_tool = lambda n: ({"tool_contract": {"metadata": {"min_authority": 50}}}
                                           if str(n).startswith("fake_") else orig_cfg(n))


def _store(prefix):
    return WorkStore(path=os.path.join(tempfile.mkdtemp(prefix=prefix), "t.db"))


def _iid(wo, tid):
    ms = [n.id for n in wo.nodes.values() if n.id.split("--")[0] == tid]
    assert len(ms) == 1, (tid, ms)
    return ms[0]


def _wait(tid, event):
    return {"id": tid, "type": "tool", "title": tid, "wake_kind": "event",
            "payload": {"is_wait": True, "guard": f'"{event}" in events_observed',
                        "subscriptions": [event]}}


def test_second_event_does_not_abandon_other_waits():
    # act -> wait(E1) -> wait(E2) -> end (sequential). The verification probe showed that after
    # E1 fired, wait(E2)'s guard evaluated provably False -> abandoned -> the task completed
    # WITHOUT waiting for E2. Fixed: is_wait nodes are exempt from branch-closing.
    store, scope = _store("vreg1_"), build_task_scope("vreg1")
    template = {
        "task_id": "vreg1", "title": "vreg1", "goal_content": "two waits", "driver": "task_runner",
        "nodes": [
            {"id": "act", "type": "tool", "title": "act",
             "payload": {"tools": [{"tool": "fake_g", "args": {"emit": "x"}}]}},
            _wait("w1", "E1"),
            _wait("w2", "E2"),
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "act", "dst": "w1", "relation": "depends_on"},
                  {"src": "w1", "dst": "w2", "relation": "depends_on"},
                  {"src": "w2", "dst": "end", "relation": "depends_on"}],
        "preloaded_facts": [],
    }
    r = start_task_run(template, store=store, scope=scope)
    assert r["status"] == "parked", r
    wid = r["work_id"]

    r1 = resume_task_run(wid, observed_event="E1", store=store, scope=scope)
    wo = store.load(wid)
    assert wo.nodes[_iid(wo, "w1")].status == "closed", "w1 should release on E1"
    assert wo.nodes[_iid(wo, "w2")].status != "abandoned", \
        "REGRESSION: the first event abandoned the other pending wait"
    assert r1["status"] == "parked" and wo.status != "done", \
        "REGRESSION: task completed without waiting for E2"

    r2 = resume_task_run(wid, observed_event="E2", store=store, scope=scope)
    wo = store.load(wid)
    assert r2["status"] == "done" and wo.nodes[_iid(wo, "w2")].status == "closed", (r2, wo.status)
    print("  test_second_event_does_not_abandon_other_waits: PASS")


def test_and_gate_wait_reparks_until_whole_condition_holds():
    # One wait_gate requiring BOTH events: promoted on the first, its release condition is still
    # False -> it must re-park (stay claimable-later), never be branch-closed.
    store, scope = _store("vreg2_"), build_task_scope("vreg2")
    template = {
        "task_id": "vreg2", "title": "vreg2", "goal_content": "and gate", "driver": "task_runner",
        "nodes": [
            {"id": "gate", "type": "tool", "title": "gate", "wake_kind": "event",
             "payload": {"is_wait": True,
                         "guard": '"A" in events_observed and "B" in events_observed',
                         "subscriptions": ["A", "B"]}},
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "gate", "dst": "end", "relation": "depends_on"}],
        "preloaded_facts": [],
    }
    r = start_task_run(template, store=store, scope=scope)
    assert r["status"] == "parked", r
    wid = r["work_id"]

    rA = resume_task_run(wid, observed_event="A", store=store, scope=scope)
    wo = store.load(wid)
    gate_status = wo.nodes[_iid(wo, "gate")].status
    assert rA["status"] == "parked" and gate_status not in ("abandoned", "closed"), \
        f"REGRESSION: AND gate {gate_status} after only one of two events"

    rB = resume_task_run(wid, observed_event="B", store=store, scope=scope)
    wo = store.load(wid)
    assert rB["status"] == "done" and wo.nodes[_iid(wo, "gate")].status == "closed", (rB, wo.status)
    print("  test_and_gate_wait_reparks_until_whole_condition_holds: PASS")


def test_same_template_runs_twice():
    # Node ids are namespaced per instance — the second run of the SAME template must succeed
    # (was: ValueError 'id already belongs to work object', task runnable exactly once).
    store, scope = _store("vreg3_"), build_task_scope("vreg3")
    template = {
        "task_id": "vreg3", "title": "vreg3", "goal_content": "run twice", "driver": "task_runner",
        "nodes": [
            {"id": "step_1", "type": "tool", "title": "s1",
             "payload": {"tools": [{"tool": "fake_g", "args": {"emit": "x"}}], "produces": ["d1"]}},
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "step_1", "dst": "end", "relation": "depends_on"}],
        "preloaded_facts": [],
    }
    r1 = start_task_run(template, store=store, scope=scope)
    r2 = start_task_run(template, store=store, scope=scope)
    assert r1["status"] == "done" and r2["status"] == "done", (r1, r2)
    assert r1["work_id"] != r2["work_id"]
    print("  test_same_template_runs_twice: PASS")


def test_unrunnable_guard_fails_the_node_not_the_drive():
    # A malformed guard (would never parse) must fail the NODE loudly and let the drive
    # finish with 'failed' — not crash the whole drive on every attempt.
    store, scope = _store("vreg4_"), build_task_scope("vreg4")
    template = {
        "task_id": "vreg4", "title": "vreg4", "goal_content": "bad guard", "driver": "task_runner",
        "nodes": [
            {"id": "bad", "type": "tool", "title": "bad",
             "payload": {"tools": [{"tool": "fake_g", "args": {}}], "guard": "this is === not python"}},
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "bad", "dst": "end", "relation": "depends_on"}],
        "preloaded_facts": [],
    }
    r = start_task_run(template, store=store, scope=scope)   # must not raise
    wo = store.load(r["work_id"])
    assert wo.nodes[_iid(wo, "bad")].status == "failed", wo.nodes[_iid(wo, "bad")].status
    assert r["status"] == "failed", r
    # a dead run is TERMINAL (abandoned) — it must not stay active for boot re-arm to
    # rescan forever (2026-07-12 incident: three stuck-active morning_briefing husks)
    assert wo.status == "abandoned", wo.status
    print("  test_unrunnable_guard_fails_the_node_not_the_drive: PASS")


if __name__ == "__main__":
    _install()
    test_second_event_does_not_abandon_other_waits()
    test_and_gate_wait_reparks_until_whole_condition_holds()
    test_same_template_runs_twice()
    test_unrunnable_guard_fails_the_node_not_the_drive()
    print("VERIFICATION REGRESSIONS: ALL PASS")
