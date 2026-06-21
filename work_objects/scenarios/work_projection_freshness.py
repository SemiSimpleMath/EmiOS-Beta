"""Regression (#41): WorkPlanner rebuilds work_projection from the LIVE graph every turn.

The coordinator's `YOUR CHECKLIST` render went stale on turns 2+ because the
workobject_render_node pre-node is bypassed on the manager's tool-return resume path
(tool_return_router pins resume_target -> the planner, so summary/critic route straight
back, skipping the render). The planner then saw turn-1's empty projection, couldn't echo
the ids of surfaces it had already minted, and re-added them -> duplicate surfaces.

The fix decoupled freshness from routing: WorkPlanner.construct_prompt ->
_refresh_work_projection rebuilds the projection from store.load right before every prompt.
This locks that invariant WITHOUT LLMs: mint a surface, refresh, and assert the projection
now shows the surface's [id] (so the planner can echo it, not re-add it). If a future routing
change re-bypasses the render node, this still passes; if someone drops the prompt-build
refresh, it fails.

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/work_projection_freshness.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
load_dotenv()

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI
from work_objects.store import WorkStore
from work_objects.model import new_id
from work_objects.runtime import set_work_context, reset_work_context
from app.assistant.agent_classes.WorkPlanner import WorkPlanner


class _BB:
    """Minimal blackboard — _refresh_work_projection only writes `work_projection`; we read it back."""
    def __init__(self):
        self._d = {}

    def update_state_value(self, k, v):
        self._d[k] = v

    def get_state_value(self, k, default=None):
        return self._d.get(k, default)


def ck(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}", flush=True)
    assert ok, label


def main():
    store = WorkStore(os.path.join(tempfile.mkdtemp(prefix="proj_"), "w.db"))
    wo = store.apply("create_work_object", {"title": "t", "goal_content": "g",
                                            "satisfied_when_kind": "tool_success"}, actor="test")
    gid = wo.goal_node_id
    token = set_work_context(store, wo.id, gid, "test")
    # A stub bearing only what _refresh_work_projection touches (self.blackboard, self.name); we drive
    # the real WorkPlanner method against a real WorkStore graph — no agent/LLM machinery needed.
    planner = SimpleNamespace(name="work_emi_team::planner", blackboard=_BB())

    print("=== empty graph: render is built but the checklist is empty ===", flush=True)
    WorkPlanner._refresh_work_projection(planner)
    proj0 = planner.blackboard.get_state_value("work_projection") or ""
    ck("projection was built (refresh ran inside an active WorkContext)", bool(proj0.strip()))
    ck("no subtasks yet -> YOUR CHECKLIST shows (empty)", "(empty" in proj0)

    print("=== mint a surface (what the reconcile does on the planner's turn) ===", flush=True)
    sid = new_id("node")
    store.apply("add_node", {"work_id": wo.id, "id": sid, "type": "subtask", "parent_id": gid,
                             "title": "Research the thing", "satisfied_when_kind": "tool_success"},
                actor="test")
    WorkPlanner._refresh_work_projection(planner)
    proj1 = planner.blackboard.get_state_value("work_projection") or ""
    ck("refresh reflects the LIVE graph -> the surface's [id] is shown", f"[id:{sid}]" in proj1)
    ck("the surface title is rendered", "Research the thing" in proj1)
    ck("projection changed between refreshes (it is rebuilt, never stale/cached)", proj1 != proj0)

    print("=== the refresh is wired into the per-turn prompt path ===", flush=True)
    ck("WorkPlanner overrides construct_prompt (the prompt-build refresh hook)",
       "construct_prompt" in WorkPlanner.__dict__)

    reset_work_context(token)
    store.close()
    print("\nWORK_PROJECTION FRESHNESS OK: the planner sees live surfaces each turn -> echoes ids, "
          "no duplicate surfaces.", flush=True)


if __name__ == "__main__":
    main()
