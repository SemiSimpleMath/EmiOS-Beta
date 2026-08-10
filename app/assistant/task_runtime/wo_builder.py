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

import re
from typing import Any

from work_objects.model import new_id

_SUPPORTED_KINDS = {"action", "tool_sequence", "end", "wait_for_event", "wait_gate", "decision"}


def _flatten(expr: str) -> str:
    """Rewrite task-IR condition paths (task_state.facts.X / task_state.artifacts.X) to the flat fact
    keys the work-object facts_context uses (events_observed, fact_1, artifact_1, ...)."""
    e = re.sub(r"task_state\.facts\.", "", str(expr or ""))
    return re.sub(r"task_state\.artifacts\.", "", e)


def _subscriptions(step: dict) -> list:
    subs = step.get("subscriptions")
    if isinstance(subs, list) and subs:
        return [str(s) for s in subs]
    ev = str(step.get("event_name") or "").strip()
    return [ev] if ev else []


def _wait_guard(step: dict) -> str:
    """The release condition for a wait node, flattened. wait_gate -> its release_condition;
    wait_for_event -> the single event being observed."""
    if str(step.get("kind") or "").strip() == "wait_gate":
        rc = str(step.get("release_condition") or "").strip()
        if rc:
            return _flatten(rc)
    subs = _subscriptions(step)
    ev = str(step.get("event_name") or "").strip() or (subs[0] if subs else "")
    if not ev:
        raise ValueError(f"wait step {step.get('id')!r} has no event to wait on")
    return f'"{ev}" in events_observed'


def _collapse_loops(steps: list, step_order: dict) -> tuple:
    """Detect decision back-edges (loops) and collapse each loop body — a run of action/tool steps ending
    in a wait — into ONE re-arming loop node (a tool node with the body's actions as invoke_agent tools,
    the wait's subscriptions/release_condition, and done_when = the exit branch's condition). Returns
    (loop_nodes, consumed_step_ids, exit_edges, consumed_to_loop). Only the simple single-back-edge
    loop is supported: one back-edge decision whose body is action/tool steps + at most one wait
    (the auto-reply shape: handle -> wait for the next email or the cutoff -> loop).

    Body selection is STRUCTURAL: the branch whose `next_step` chain actually reaches the decision is
    the loop body. The old positional range [order(start), order(decision)) proved nothing about the
    graph — verification probes showed an unrelated step listed inside the range was silently folded
    into the loop (ran every iteration), and an exit handler at an earlier list position was misread
    as the back-edge, INVERTING the loop's meaning."""
    _ = step_order
    step_by_id = {str(s.get("id") or "").strip(): s for s in steps}

    def _chain_to(start_id: str, decision_id: str):
        """Follow next_step links from start_id; the chain (list of steps) if it reaches decision_id,
        else None. Bounded by the visited set — a cycle that never reaches the decision returns None."""
        chain, cur, seen = [], start_id, set()
        while cur and cur in step_by_id and cur not in seen:
            if cur == decision_id:
                return chain
            seen.add(cur)
            chain.append(step_by_id[cur])
            cur = str(step_by_id[cur].get("next_step") or "").strip()
        return None

    loop_nodes: list[dict[str, Any]] = []
    consumed: set[str] = set()
    consumed_to_loop: dict[str, str] = {}
    exit_edges: list[dict[str, str]] = []
    for d in steps:
        if str(d.get("kind") or "").strip() != "decision":
            continue
        did = str(d.get("id") or "").strip()
        cond = _flatten(str(d.get("condition") or "").strip())
        ot = str(d.get("on_true") or "").strip()
        of = str(d.get("on_false") or "").strip()
        chain_true = _chain_to(ot, did) if ot else None
        chain_false = _chain_to(of, did) if of else None
        if chain_true is not None and chain_false is not None:
            raise NotImplementedError(f"decision {did!r}: both branches chain back to it — not supported")
        if chain_true is not None:
            start, exit_target, done_when, body = ot, of, f"not ({cond})", chain_true
        elif chain_false is not None:
            start, exit_target, done_when, body = of, ot, cond, chain_false
        else:
            continue   # forward decision — absorbed into guards elsewhere
        if not exit_target:
            # end gets zero deps -> ready in wave 1 -> premature work-done while the loop is
            # stranded (verification probe P3). A loop must have an exit branch.
            raise ValueError(f"loop decision {did!r} has no exit branch — the loop could never "
                             "release the graph")
        body_ids = {str(bs.get("id") or "").strip() for bs in body}
        if body_ids & consumed:
            raise NotImplementedError(f"loop at {start!r} shares body steps with another loop — not supported")

        tools: list = []
        subs: list = []
        produces: list[str] = []
        watch_regs: list = []
        release_condition = None
        for bs in body:
            k = str(bs.get("kind") or "").strip()
            if k == "action":
                tools.append({"tool": "invoke_agent",
                              "args": {"agent_name": str(bs.get("executor") or "").strip(),
                                       "agent_input": {"task": str(bs.get("instruction") or "")}}})
            elif k == "tool_sequence":
                tools.extend(bs.get("tools") or [])
            elif k in ("wait_for_event", "wait_gate"):
                if release_condition is not None:
                    raise NotImplementedError(f"loop starting at {start!r} has more than one wait — not supported")
                subs, release_condition = _subscriptions(bs), _wait_guard(bs)
                if bs.get("watch_registration"):
                    watch_regs.append(bs["watch_registration"])
                if bs.get("watch_registrations"):
                    watch_regs.extend(bs["watch_registrations"])
            else:
                raise NotImplementedError(f"loop body step {bs.get('id')!r} of kind {k!r} not supported")
            # the loop node inherits the body's produced facts so its evidence is recorded and a
            # fact-based done_when is satisfiable (they were dropped before — a compiled loop could
            # never meet its own done condition)
            produces.extend(str(p) for p in (bs.get("produces_data_ids") or []))

        node: dict[str, Any] = {"id": start, "type": "tool", "title": f"loop {start}",
                                "payload": {"tools": tools, "is_loop": True, "done_when": _flatten(done_when)}}
        if produces:
            node["payload"]["produces"] = produces
        if watch_regs:
            node["payload"]["watch_registrations"] = watch_regs
        if release_condition is not None:
            # a loop with a wait -> park on its events between iterations (wake-promotion re-arm)
            node["payload"]["subscriptions"] = subs
            node["payload"]["release_condition"] = release_condition
            node["wake_kind"] = "event"
        else:
            # a state-predicate loop (no wait) re-runs IMMEDIATELY — its done_when must be satisfiable
            # by the loop's own recorded output, or it hot-spins to the iteration cap and fails.
            from app.assistant.task_runtime.guard_eval import expression_names
            names = expression_names(node["payload"]["done_when"])
            if not (names & set(produces)):
                raise ValueError(
                    f"no-wait loop at {start!r}: done_when ({done_when!r}) references none of the "
                    f"loop's produced facts {sorted(set(produces))} — it could never terminate")
        loop_nodes.append(node)
        consumed.update(body_ids)
        consumed.add(did)
        for cid in body_ids | {did}:
            consumed_to_loop[cid] = start
        exit_edges.append({"src": start, "dst": exit_target, "relation": "depends_on"})
    return loop_nodes, consumed, exit_edges, consumed_to_loop


def build_template(compiled_task: dict) -> dict:
    """Deterministically map a compiled task (normalized steps + data_bindings + preloaded_task_state)
    to a work-object template dict. A forward `decision` is absorbed into mutually-exclusive guards on its
    branch targets; wait_for_event/wait_gate become event-gated wait nodes; a decision that branches BACK
    to an earlier step is a LOOP, collapsed into one re-arming loop node (see _collapse_loops)."""
    task_id = str(compiled_task.get("task_id") or "").strip() or "task"
    steps = compiled_task.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("compiled_task.steps must be a non-empty list")
    data_bindings = compiled_task.get("data_bindings") or []
    preloaded = compiled_task.get("preloaded_task_state") or {}

    step_order = {str(s.get("id") or "").strip(): i for i, s in enumerate(steps)}
    pred_of: dict[str, str] = {}
    for s in steps:
        nxt = str(s.get("next_step") or "").strip()
        if nxt:
            pred_of.setdefault(nxt, str(s.get("id") or "").strip())

    loop_nodes, consumed, loop_exit_edges, consumed_to_loop = _collapse_loops(steps, step_order)

    nodes: list[dict[str, Any]] = list(loop_nodes)
    node_by_id: dict[str, dict[str, Any]] = {n["id"]: n for n in loop_nodes}
    end_ids: list[str] = []
    wait_ids: list[str] = []
    decisions: list[dict[str, Any]] = []
    for step in steps:
        kind = str(step.get("kind") or "").strip()
        sid = str(step.get("id") or "").strip()
        if not sid:
            raise ValueError("compiled step missing id")
        # Contract features the runtime does not implement are REFUSED, never silently dropped
        # (verification finding: output_schema/gate_type/task_state_update/event_fact_bindings
        # vanished from the emitted work object with no error — a task compiled with a typed-output
        # contract lost it invisibly). Checked before the consumed-skip so loop bodies count too.
        for field in ("output_schema", "gate_type", "task_state_update", "event_fact_bindings"):
            if step.get(field):
                raise NotImplementedError(
                    f"step {sid!r} uses {field!r}, which the work-object runtime does not implement "
                    "— refusing to emit a task that would silently lose that contract")
        if sid in consumed:
            continue   # folded into a loop node by _collapse_loops
        if kind not in _SUPPORTED_KINDS:
            raise NotImplementedError(f"step kind {kind!r} (step {sid!r}) not supported")
        title = str(step.get("title") or sid)
        produces = [str(p) for p in (step.get("produces_data_ids") or [])]
        if kind == "action":
            node = {"id": sid, "type": "action", "title": title, "content": str(step.get("instruction") or ""),
                    "payload": {"executor": str(step.get("executor") or "").strip(),
                                "instruction": str(step.get("instruction") or ""), "produces": produces}}
        elif kind == "tool_sequence":
            node = {"id": sid, "type": "tool", "title": title,
                    "payload": {"tools": step.get("tools") or [], "produces": produces}}
        elif kind in ("wait_for_event", "wait_gate"):
            payload = {"is_wait": True, "guard": _wait_guard(step), "subscriptions": _subscriptions(step)}
            if step.get("watch_registration"):
                payload["watch_registration"] = step["watch_registration"]
            if step.get("watch_registrations"):
                payload["watch_registrations"] = step["watch_registrations"]
            node = {"id": sid, "type": "tool", "title": title, "payload": payload, "wake_kind": "event"}
            wait_ids.append(sid)
        elif kind == "end":
            end_ids.append(sid)
            node = {"id": sid, "type": "tool", "title": title, "payload": {"is_end": True}}
        else:  # decision -> absorbed into guards on its targets (below)
            decisions.append(step)
            continue
        nodes.append(node)
        node_by_id[sid] = node

    # data_bindings -> depends_on edges (consumer depends_on producer); track producers with consumers.
    edges: list[dict[str, str]] = list(loop_exit_edges)   # loop node -> its exit target
    producers_with_consumers: set[str] = set()
    for b in data_bindings:
        producer = str(b.get("producer_step_id") or "").strip()
        for consumer in (b.get("consumer_step_ids") or []):
            c = str(consumer).strip()
            if producer and c:
                edges.append({"src": producer, "dst": c, "relation": "depends_on"})
                producers_with_consumers.add(producer)

    # a wait node runs AFTER its sequential predecessor (handle, THEN wait). A predecessor that
    # was folded into a loop maps to its loop node (was: the edge silently vanished).
    for wid in wait_ids:
        pred = consumed_to_loop.get(pred_of.get(wid), pred_of.get(wid))
        if pred and pred in node_by_id and pred != wid:
            edges.append({"src": pred, "dst": wid, "relation": "depends_on"})

    # decisions: mutually-exclusive guards on the branch targets + depend on the decision's predecessor.
    targets_with_edge: set[str] = {e["dst"] for e in loop_exit_edges}   # loop exits already have an edge
    for d in decisions:
        did = str(d.get("id") or "").strip()
        cond = _flatten(str(d.get("condition") or "").strip())
        if not cond:
            raise ValueError(f"decision {did!r} missing condition")
        pred = consumed_to_loop.get(pred_of.get(did), pred_of.get(did))
        for target, guard in ((str(d.get("on_true") or "").strip(), cond),
                              (str(d.get("on_false") or "").strip(), f"not ({cond})")):
            if not target:
                continue
            if step_order.get(target, 10 ** 9) <= step_order.get(did, -1):
                raise NotImplementedError(
                    f"decision {did!r} branches back to earlier step {target!r} — that's a LOOP "
                    "(the Phase-4 loop-collapse is not built yet)")
            tnode = node_by_id.get(target)
            if tnode is None:
                raise ValueError(f"decision {did!r} target {target!r} is not a node")
            tnode.setdefault("payload", {})["guard"] = guard
            if pred and pred in node_by_id:
                edges.append({"src": pred, "dst": target, "relation": "depends_on"})
                targets_with_edge.add(target)

    # end nodes not already reached via a decision branch depend on the graph SINKS (reach-end).
    real_ids = [str(s.get("id") or "").strip() for s in steps
                if str(s.get("kind") or "").strip() not in ("end", "decision")
                and str(s.get("id") or "").strip() not in consumed]
    sinks = [sid for sid in real_ids if sid and sid not in producers_with_consumers]
    for end_id in end_ids:
        if end_id in targets_with_edge:
            continue
        for sink in sinks:
            edges.append({"src": sink, "dst": end_id, "relation": "depends_on"})

    preloaded_facts: list[dict[str, Any]] = []
    for bucket in ("facts", "artifacts"):
        values = preloaded.get(bucket)
        if isinstance(values, dict):
            for data_id, value in values.items():
                preloaded_facts.append({"data_id": str(data_id), "value": value})

    # Build-time expression check: every guard/done_when/release_condition must parse and stay
    # inside the safe-eval subset — an unrunnable condition fails the COMPILE loudly, not a live
    # run later (at runtime the same defect fails only the node).
    from app.assistant.task_runtime.guard_eval import validate_expression
    for n in nodes:
        p = n.get("payload") or {}
        for key in ("guard", "done_when", "release_condition"):
            if p.get(key):
                validate_expression(p[key])

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
    Returns the new work_id. `store` is a TASK-owned work store (a work_objects WorkStore), never dayflow's.

    Node ids are namespaced per instance (`<template_id>--<wid6>`): node ids are GLOBAL in the store
    (the cross-WO guard refuses reuse), so writing template ids verbatim made every task runnable
    exactly once — the second run collided on `step_1` (verification finding, probe-confirmed).
    Edges are mapped through the same namespace. A failed instantiation abandons the partial work
    object before re-raising, so no goal-only `active` husk is left for boot re-arm to chew on."""
    wo = store.apply("create_work_object", {
        "title": template.get("title", ""),
        "goal_content": template.get("goal_content", ""),
        "created_by": created_by,
        "constraints": {"driver": template.get("driver", "task_runner"), "task_id": template.get("task_id")},
    }, actor=created_by)
    wid, goal = wo.id, wo.goal_node_id
    suffix = wid[-6:]
    id_map = {str(n["id"]): f'{n["id"]}--{suffix}' for n in template.get("nodes", [])}

    try:
        for n in template.get("nodes", []):
            node = {"work_id": wid, "id": id_map[str(n["id"])], "type": n["type"], "parent_id": goal,
                    "title": n.get("title", n["id"]), "payload": n.get("payload", {})}
            for field in ("content", "wake_kind", "wake_at", "wake_ref", "satisfied_when_kind",
                          "satisfied_when_ref", "side_effect", "requires_approval", "authority", "status"):
                if n.get(field) is not None:
                    node[field] = n[field]
            store.apply("add_node", node, actor=created_by)
        for e in template.get("edges", []):
            src, dst = str(e["src"]), str(e["dst"])
            store.apply("add_edge", {"work_id": wid, "src": id_map.get(src, src),
                                     "dst": id_map.get(dst, dst),
                                     "relation": e.get("relation", "depends_on")}, actor=created_by)
        for f in template.get("preloaded_facts", []):
            store.apply("add_node", {"work_id": wid, "id": new_id("ev"), "type": "evidence", "parent_id": goal,
                                     "status": "assumed", "title": str(f["data_id"]),
                                     "content": str(f.get("value", "")),
                                     "payload": {"data_id": str(f["data_id"]), "value": f.get("value")}},
                        actor=created_by)
    except Exception:
        store.apply("set_work_status", {"work_id": wid, "status": "abandoned",
                                        "reason": "task builder: superseded/invalid build"}, actor=created_by)
        raise
    return wid
