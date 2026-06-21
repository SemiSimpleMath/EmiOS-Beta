"""
Sleep-cooling WorkObject — the v1 proving-ground graph, built in-memory.

Goal "stop overnight cooling at 6 AM"
  └─expands_into─ Tool   "set Nest setpoint" (wake_on time=06:00, side_effect=mutate)
                  Artifact "Nest confirmation"   depends_on Tool
                  Evidence "cooling actually stopped" depends_on Artifact

This is a SMOKE TEST of model.py — no app imports, no DB. It asserts the three
core scheduling mechanics hold:
  1. wake-gating   — the Tool is NOT ready before 06:00, IS ready after.
  2. dep-gating    — the Artifact is blocked until the Tool is done.
  3. satisfaction  — the Goal (all_children_done) only satisfies once the whole
                     subtree is satisfied.

Run from repo root:  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/sleep_cooling_smoke.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from work_objects.model import WorkNode, WorkObject

PDT = timezone(timedelta(hours=-7))


def build() -> WorkObject:
    w = WorkObject(title="stop overnight cooling at 6 AM")

    goal = w.add_node(WorkNode(
        work_id=w.id, type="goal", title="stop overnight cooling at 6 AM",
        status="active",  # already decomposed, now waiting on its children
        satisfied_when_kind="all_owned_children_done",
        content="At 6:00 AM stop active cooling so the AC doesn't keep running into the morning.",
    ))
    w.goal_node_id = goal.id  # root goal: parent_id stays None

    tool = w.add_node(WorkNode(
        work_id=w.id, type="tool", title="set Nest setpoint (stop active cooling)",
        parent_id=goal.id, owner_agent="home_automation",
        satisfied_when_kind="tool_success", side_effect="mutate",
        wake_kind="time", wake_at=datetime(2026, 6, 18, 6, 0, tzinfo=PDT),
        payload={"tool": "nest_set_mode", "args": {"mode": "eco", "target_f": 75}},
    ))
    artifact = w.add_node(WorkNode(
        work_id=w.id, type="artifact", title="Nest confirmation",
        parent_id=goal.id, pod_ref="datapod:nest_confirmation:pending",
    ))
    evidence = w.add_node(WorkNode(
        work_id=w.id, type="evidence", title="cooling actually stopped",
        parent_id=goal.id,
    ))

    # ownership = parent_id (set above). edges = the dependency / production DAG only.
    w.add_edge(tool.id, artifact.id, "produces")
    w.add_edge(tool.id, artifact.id, "depends_on")       # artifact waits on the tool
    w.add_edge(artifact.id, evidence.id, "depends_on")    # evidence waits on the artifact
    w.validate()
    return w


def _ids(nodes) -> set[str]:
    return {n.title for n in nodes}


def main() -> None:
    w = build()
    tool = next(n for n in w.nodes.values() if n.type == "tool")
    artifact = next(n for n in w.nodes.values() if n.type == "artifact")
    evidence = next(n for n in w.nodes.values() if n.type == "evidence")
    goal = w.nodes[w.goal_node_id]

    before = datetime(2026, 6, 18, 5, 30, tzinfo=PDT)
    after = datetime(2026, 6, 18, 6, 30, tzinfo=PDT)

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    print("=== sleep-cooling WorkObject — scheduling mechanics ===")

    # 1. wake-gating
    ck("Tool NOT ready at 05:30 (wake_at=06:00)", not w.is_ready(tool, before))
    ck("Tool ready at 06:30", w.is_ready(tool, after))
    ck("ready set at 05:30 is empty", _ids(w.ready_nodes(before)) == set())

    # 2. dep-gating — Artifact blocked until Tool done
    ck("Artifact blocked while Tool open", not w.is_ready(artifact, after))
    tool.status = "done"
    ck("Artifact ready once Tool done", w.is_ready(artifact, after))
    ck("Evidence still blocked (Artifact not done)", not w.is_ready(evidence, after))

    # 3. satisfaction propagation up to the Goal
    ck("Goal NOT satisfied yet", not w.is_satisfied(goal))
    artifact.status = "done"
    evidence.status = "verified"
    ck("Goal satisfied once whole subtree satisfied", w.is_satisfied(goal))

    print("\nall mechanics hold.")


if __name__ == "__main__":
    main()
