"""Compile-time transform: a compiled task's step list -> a work-object TEMPLATE, and the
instantiation of a template into a durable INSTANCE.

This is Option B's core — the compiler emits a work object DIRECTLY (task_compile_post_node calls
`build_template` on its normalized steps; `task_ir_v1` is never persisted). Pure, deterministic, no
dayflow, no runtime translator.

Node-kind mapping (Phase 2 = straight-through tasks: action / tool_sequence / end):
  action        -> an `action` node (payload.executor + instruction), run by its manager via the substrate.
  tool_sequence -> a `tool` node (payload.tools), run by the deterministic tool executor.
  end           -> an is_end `tool` node depending on the graph's SINKS (reach-end completion).
Data flow: each data_binding -> a `depends_on` edge (consumer depends_on producer). Parallelism falls
out — independent producers have no deps, so the runner fans them out in one wave. preloaded_task_state
-> seeded evidence facts.

Waits / gates / decisions / loops are Phase 4 (raise NotImplementedError here so nothing silently drops).
"""
from __future__ import annotations

from typing import Any

from work_objects.model import new_id

_STRAIGHT_THROUGH_KINDS = {"action", "tool_sequence", "end"}


def build_template(compiled_task: dict) -> dict:
    """Deterministically map a compiled task (normalized steps + data_bindings + preloaded_task_state)
    to a work-object template dict. Raises NotImplementedError on step kinds not yet supported."""
    task_id = str(compiled_task.get("task_id") or "").strip() or "task"
    steps = compiled_task.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("compiled_task.steps must be a non-empty list")
    data_bindings = compiled_task.get("data_bindings") or []
    preloaded = compiled_task.get("preloaded_task_state") or {}

    nodes: list[dict[str, Any]] = []
    end_ids: list[str] = []
    for step in steps:
        kind = str(step.get("kind") or "").strip()
        sid = str(step.get("id") or "").strip()
        if not sid:
            raise ValueError("compiled step missing id")
        if kind not in _STRAIGHT_THROUGH_KINDS:
            raise NotImplementedError(
                f"step kind {kind!r} (step {sid!r}) is Phase 4 (wait/gate/decision/loop) — not yet supported")
        title = str(step.get("title") or sid)
        produces = [str(p) for p in (step.get("produces_data_ids") or [])]
        if kind == "action":
            nodes.append({"id": sid, "type": "action", "title": title,
                          "content": str(step.get("instruction") or ""),
                          "payload": {"executor": str(step.get("executor") or "").strip(),
                                      "instruction": str(step.get("instruction") or ""),
                                      "produces": produces}})
        elif kind == "tool_sequence":
            nodes.append({"id": sid, "type": "tool", "title": title,
                          "payload": {"tools": step.get("tools") or [], "produces": produces}})
        else:  # end
            end_ids.append(sid)
            nodes.append({"id": sid, "type": "tool", "title": title, "payload": {"is_end": True}})

    # data_bindings -> depends_on edges (consumer depends_on producer); track producers with consumers.
    edges: list[dict[str, str]] = []
    producers_with_consumers: set[str] = set()
    for b in data_bindings:
        producer = str(b.get("producer_step_id") or "").strip()
        for consumer in (b.get("consumer_step_ids") or []):
            c = str(consumer).strip()
            if producer and c:
                edges.append({"src": producer, "dst": c, "relation": "depends_on"})
                producers_with_consumers.add(producer)

    # the end node(s) depend on all SINKS (non-end steps nothing consumes) so they run last.
    real_ids = [str(s.get("id") or "").strip() for s in steps if str(s.get("kind") or "").strip() != "end"]
    sinks = [sid for sid in real_ids if sid and sid not in producers_with_consumers]
    for end_id in end_ids:
        for sink in sinks:
            edges.append({"src": sink, "dst": end_id, "relation": "depends_on"})

    preloaded_facts: list[dict[str, Any]] = []
    for bucket in ("facts", "artifacts"):
        values = preloaded.get(bucket)
        if isinstance(values, dict):
            for data_id, value in values.items():
                preloaded_facts.append({"data_id": str(data_id), "value": value})

    return {
        "task_id": task_id,
        "title": task_id.replace("_", " ").strip(),
        "goal_content": str(compiled_task.get("source_task") or task_id),
        "driver": "task_runner",
        "nodes": nodes,
        "edges": edges,
        "preloaded_facts": preloaded_facts,
    }


def instantiate_template(store, template: dict, *, created_by: str = "task_runner") -> str:
    """Write a template into `store` as a fresh work-object INSTANCE (constraints.driver='task_runner').
    Returns the new work_id. `store` is a TASK-owned work store (a work_objects WorkStore), never dayflow's."""
    wo = store.apply("create_work_object", {
        "title": template.get("title", ""),
        "goal_content": template.get("goal_content", ""),
        "created_by": created_by,
        "constraints": {"driver": template.get("driver", "task_runner"), "task_id": template.get("task_id")},
    }, actor=created_by)
    wid, goal = wo.id, wo.goal_node_id

    for n in template.get("nodes", []):
        node = {"work_id": wid, "id": n["id"], "type": n["type"], "parent_id": goal,
                "title": n.get("title", n["id"]), "payload": n.get("payload", {})}
        for field in ("content", "wake_kind", "wake_at", "wake_ref", "satisfied_when_kind",
                      "satisfied_when_ref", "side_effect", "requires_approval", "authority", "status"):
            if n.get(field) is not None:
                node[field] = n[field]
        store.apply("add_node", node, actor=created_by)
    for e in template.get("edges", []):
        store.apply("add_edge", {"work_id": wid, "src": e["src"], "dst": e["dst"],
                                 "relation": e.get("relation", "depends_on")}, actor=created_by)
    for f in template.get("preloaded_facts", []):
        store.apply("add_node", {"work_id": wid, "id": new_id("ev"), "type": "evidence", "parent_id": goal,
                                 "status": "assumed", "title": str(f["data_id"]),
                                 "content": str(f.get("value", "")),
                                 "payload": {"data_id": str(f["data_id"]), "value": f.get("value")}},
                    actor=created_by)
    return wid
