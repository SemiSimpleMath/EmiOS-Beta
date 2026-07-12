"""Phase 2 web-free validation of the compiled-task -> work-object transform (task_runtime.wo_builder).

Deterministic — no LLMs, no tools, no live run. Loads the REAL morning_briefing.compiled.json,
builds a work-object template, and asserts the transform preserved the task's structure and, crucially,
its PARALLELISM (the four independent gather steps end up with no deps, so the runner fans them out;
the synthesis step depends on all four; save depends on synthesis; the end node depends on the sink).

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\wo_builder_test.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402  bootstrap DI before project imports

from work_objects.store import WorkStore
from app.assistant.task_runtime.wo_builder import build_template, instantiate_template

_COMPILED = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "configs", "routines", "public", "morning_briefing.compiled.json")


def test_morning_briefing_transform():
    compiled = json.loads(open(_COMPILED, encoding="utf-8").read())
    template = build_template(compiled)

    by_id = {n["id"]: n for n in template["nodes"]}
    # step_1 = action (playwright); step_2/3/calendar/4/5 = tool; step_6 = is_end tool
    assert by_id["step_1"]["type"] == "action", by_id["step_1"]["type"]
    assert by_id["step_1"]["payload"]["executor"] == "playwright_manager"
    for sid in ("step_2", "step_3", "step_calendar", "step_4", "step_5"):
        assert by_id[sid]["type"] == "tool", (sid, by_id[sid]["type"])
    assert by_id["step_4"]["payload"]["tools"], "step_4 must carry its invoke_agent tool spec"
    end_nodes = [n for n in template["nodes"] if n["payload"].get("is_end")]
    assert len(end_nodes) == 1 and end_nodes[0]["id"] == "step_6"
    assert template["driver"] == "task_runner"

    # instantiate into a TASK-owned store (NOT dayflow) and check the graph
    store = WorkStore(path=os.path.join(tempfile.mkdtemp(prefix="wobuild_"), "task.db"))
    wid = instantiate_template(store, template)
    wo = store.load(wid)
    assert wo.constraints.get("driver") == "task_runner"

    # node ids are namespaced PER INSTANCE (tid--<wid6>) so a template can run more than once
    def _iid(tid):
        ms = [n.id for n in wo.nodes.values() if n.id.split("--")[0] == tid]
        assert len(ms) == 1, (tid, ms)
        return ms[0]

    deps = lambda sid: set(wo.deps_of(_iid(sid)))
    # the four gather steps are INDEPENDENT -> no deps -> parallel first wave
    for sid in ("step_1", "step_2", "step_3", "step_calendar"):
        assert deps(sid) == set(), f"{sid} should have no deps (parallel), got {deps(sid)}"
    # synthesis depends on all four gather outputs; save on synthesis; end on the sink (save)
    assert deps("step_4") == {_iid(s) for s in ("step_1", "step_2", "step_3", "step_calendar")}, deps("step_4")
    assert deps("step_5") == {_iid("step_4")}, deps("step_5")
    assert deps("step_6") == {_iid("step_5")}, deps("step_6")

    # re-run: the SAME template instantiates again (ids namespaced per instance — run-once was a bug)
    wid2 = instantiate_template(store, template)
    assert wid2 != wid and store.load(wid2).constraints.get("driver") == "task_runner"

    print(f"  nodes={len(wo.nodes)} edges={len(wo.edges)} "
          f"first-wave(parallel)={sorted(n.id for n in wo.nodes.values() if n.type in ('tool','action') and not wo.deps_of(n.id))}")
    print("  test_morning_briefing_transform: PASS")


def test_unsupported_kind_raises():
    # an unknown step kind must fail loud, never silently drop (wait/gate/decision are now supported;
    # a decision back-edge loop is covered in task_wait_decision_test)
    compiled = {"task_id": "x", "steps": [{"id": "s1", "kind": "quantum_gate"}]}
    try:
        build_template(compiled)
    except NotImplementedError:
        print("  test_unsupported_kind_raises: PASS")
        return
    raise AssertionError("expected NotImplementedError for an unknown step kind")


if __name__ == "__main__":
    test_morning_briefing_transform()
    test_unsupported_kind_raises()
    print("PHASE 2 WO-BUILDER: ALL PASS")
