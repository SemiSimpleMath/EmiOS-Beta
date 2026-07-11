"""Phase 1 web-free validation of the task pipeline runner (app/assistant/task_runtime/).

Tool nodes only (deterministic fake tools — no managers/LLMs). Template:

    goal
     ├─ g1, g2, g3   parallel gather (no deps) -> facts d1,d2,d3      [PARALLEL WAVE]
     ├─ decide        -> fact branch='dry'
     ├─ X  guard branch=='rain'   -> abandoned (branch close)
     ├─ Y  guard branch=='dry'    -> taken
     ├─ synth  depends_on g1,g2,g3,Y; arg "report from ${d1}"        [dep ordering + ${} subst]
     ├─ wait   wake_at = now+1h                                       [PARK here]
     └─ end    is_end; depends_on synth, wait

Proves: the authority GATE (fake tool floor 50 vs scope authority 98 -> pass; a floor-100 fake ->
deny -> node failed), a PARALLEL gather wave, guard branch + abandon, dependency ordering,
${data_id} substitution, PARK on a future wake, and RESUME across a simulated RESTART (reload the
store from disk) to completion.

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_runner_core.py
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402  bootstrap DI before project imports

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ScopeContext, ScopeApprovalPolicy, ToolResult
from work_objects.store import WorkStore
from work_objects.model import new_id, utcnow
from app.assistant.task_runtime.entry import run_task_workobject, resume_task_workobject


# --- deterministic fake tools (monkeypatched into the registry for fake_* names) ---

class _FakeTool:
    def execute(self, msg):
        args = (getattr(msg, "tool_data", {}) or {}).get("arguments", {}) or {}
        return ToolResult(content=str(args.get("emit", "ok")), result_type="success", data={})


def _install_fake_tools():
    orig_cls = DI.tool_registry.get_tool_class
    orig_cfg = DI.tool_registry.get_tool

    def get_tool_class(name):
        return _FakeTool if str(name).startswith("fake_") else orig_cls(name)

    def get_tool(name):
        if str(name).startswith("fake_"):
            floor = 100 if str(name) == "fake_denied" else 50
            return {"tool_contract": {"metadata": {"min_authority": floor}}}
        return orig_cfg(name)

    DI.tool_registry.get_tool_class = get_tool_class
    DI.tool_registry.get_tool = get_tool


def _scope(authority=98):
    return ScopeContext(scope_id="scope::task::test", owner_id="PRINCIPAL_USER",
                        actor_id="task_runner", surface="routine",
                        approval=ScopeApprovalPolicy(authority_level=authority))


def _tool(store, wid, goal, nid, *, emit=None, produces=None, guard=None, deps=(), wake_at=None, is_end=False):
    payload = {}
    if emit is not None:
        payload["tools"] = [{"tool": "fake_gather", "args": {"emit": emit}}]
    if produces:
        payload["produces"] = list(produces)
    if guard:
        payload["guard"] = guard
    if is_end:
        payload["is_end"] = True
    node = {"work_id": wid, "id": nid, "type": "tool", "parent_id": goal, "title": nid, "payload": payload}
    if wake_at is not None:
        node["wake_kind"] = "time"
        node["wake_at"] = wake_at
    store.apply("add_node", node, actor="test")
    for d in deps:
        store.apply("add_edge", {"work_id": wid, "src": d, "dst": nid, "relation": "depends_on"}, actor="test")


def test_pipeline():
    tmp = tempfile.mkdtemp(prefix="taskrun_")
    db = os.path.join(tmp, "work.db")
    store = WorkStore(path=db)
    scope = _scope()

    wo = store.apply("create_work_object", {"title": "pipeline", "goal_content": "gather + synth"}, actor="test")
    wid, goal = wo.id, wo.goal_node_id

    ids = {k: new_id(k) for k in ("g1", "g2", "g3", "decide", "X", "Y", "synth", "wait", "end")}
    _tool(store, wid, goal, ids["g1"], emit="g1v", produces=["d1"])
    _tool(store, wid, goal, ids["g2"], emit="g2v", produces=["d2"])
    _tool(store, wid, goal, ids["g3"], emit="g3v", produces=["d3"])
    _tool(store, wid, goal, ids["decide"], emit="dry", produces=["branch"])
    _tool(store, wid, goal, ids["X"], emit="rainwork", guard="branch == 'rain'", deps=[ids["decide"]])
    _tool(store, wid, goal, ids["Y"], emit="drywork", guard="branch == 'dry'", deps=[ids["decide"]])
    # synth references ${d1} to exercise substitution; depends on the whole gather + the taken branch
    store.apply("add_node", {"work_id": wid, "id": ids["synth"], "type": "tool", "parent_id": goal,
                             "title": "synth", "payload": {"tools": [{"tool": "fake_synth",
                             "args": {"emit": "report from ${d1}"}}], "produces": ["report"]}}, actor="test")
    for d in ("g1", "g2", "g3", "Y"):
        store.apply("add_edge", {"work_id": wid, "src": ids[d], "dst": ids["synth"], "relation": "depends_on"}, actor="test")
    _tool(store, wid, goal, ids["wait"], wake_at=utcnow() + timedelta(hours=1))   # pure wait (no tools)
    _tool(store, wid, goal, ids["end"], is_end=True, deps=[ids["synth"], ids["wait"]])

    status = run_task_workobject(store, wid, scope=scope)
    wo = store.load(wid)
    st = lambda k: wo.nodes[ids[k]].status
    print(f"  [drive]  status={status}  X={st('X')} Y={st('Y')} synth={st('synth')} wait={st('wait')} end={st('end')}")
    assert status == "parked", f"expected parked, got {status}"
    assert st("X") == "abandoned", f"X (guard false) should be abandoned, got {st('X')}"
    for k in ("g1", "g2", "g3", "decide", "Y", "synth"):
        assert st(k) == "closed", f"{k} should be closed, got {st(k)}"
    assert st("end") not in ("closed", "done"), "end must not run before the wait resolves"
    facts = {n.payload.get("data_id"): n.payload.get("value") for n in wo.nodes.values() if n.type == "evidence"}
    assert facts.get("report") == "report from g1v", f"${{d1}} substitution failed: {facts.get('report')!r}"

    # simulate the wake firing + a process RESTART (fresh store from the same durable db)
    store.apply("defer_node", {"work_id": wid, "node_id": ids["wait"], "wake_kind": "time",
                               "wake_at": utcnow() - timedelta(seconds=1)}, actor="test")
    store2 = WorkStore(path=db)
    status2 = resume_task_workobject(store2, wid, scope=scope)
    wo2 = store2.load(wid)
    print(f"  [resume] status={status2}  wait={wo2.nodes[ids['wait']].status} end={wo2.nodes[ids['end']].status} WO={wo2.status}")
    assert status2 == "done", f"expected done after resume, got {status2}"
    assert wo2.nodes[ids["wait"]].status == "closed"
    assert wo2.nodes[ids["end"]].status == "closed"
    assert wo2.status == "done"
    print("  test_pipeline: PASS")


def test_gate_denies():
    tmp = tempfile.mkdtemp(prefix="taskgate_")
    store = WorkStore(path=os.path.join(tmp, "work.db"))
    scope = _scope(authority=98)   # 98 < the fake_denied floor of 100
    wo = store.apply("create_work_object", {"title": "gate", "goal_content": "deny"}, actor="test")
    wid, goal = wo.id, wo.goal_node_id
    n = new_id("denied")
    store.apply("add_node", {"work_id": wid, "id": n, "type": "tool", "parent_id": goal, "title": "denied",
                             "payload": {"tools": [{"tool": "fake_denied", "args": {}}]}}, actor="test")
    status = run_task_workobject(store, wid, scope=scope)
    wo = store.load(wid)
    print(f"  [gate]   status={status}  node={wo.nodes[n].status}")
    assert status == "failed", f"a floor-100 tool at authority 98 must not complete, got {status}"
    assert wo.nodes[n].status == "failed", f"denied tool node should be failed, got {wo.nodes[n].status}"
    assert wo.status != "done", "WO must not be done when a node was denied"
    print("  test_gate_denies: PASS")


if __name__ == "__main__":
    _install_fake_tools()
    test_pipeline()
    test_gate_denies()
    print("PHASE 1 TASK RUNNER: ALL PASS")
