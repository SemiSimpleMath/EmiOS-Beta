"""
work_objects.work_runtime — the inner-loop driver for the work graph.

    run_node(store, work_id, node_id) -> the manager's ToolResult (status is read from the node).

The node managers (work_emi_team_manager, work_web_manager) and their agents are now ordinary
first-class configs under app/assistant/multi_agents/ and app/assistant/agents/, loaded by the
standard manager_registry + agent/manager factories — NOT registered here. This module only DRIVES a
node: it sets the work contextvar, invokes the chosen manager, harvests a surfaced research pod, and
closes the node. The graph is written by the planner's WorkPlanner reconcile hook (class-level), not
by tools. Replaces the old agent_runner.py.
"""
from __future__ import annotations

import uuid

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message, ToolResult

from work_objects.runtime import reset_work_context, set_work_context
from work_objects.runtime_setup import ensure_manager_services
from work_objects.scope import orchestrator_scope
from work_objects.work_tools import register_work_tools

_registered = False


def _ensure_registered() -> None:
    """Make the work runtime's shared services available before driving a node: the manager-runtime
    services, the static manager configs (loaded from multi_agents/ like any manager — including the
    node managers work_emi_team_manager / work_web_manager), and the work_* graph tools. The managers
    and their agents are ordinary first-class configs now; nothing is dynamically constructed here."""
    global _registered
    if _registered:
        return
    ensure_manager_services()
    DI.manager_registry.preload_all()      # loads multi_agents/*/config.yaml (incl. the node managers)
    register_work_tools(DI.tool_registry)
    _registered = True


def run_node(store, work_id: str, node_id: str,
             manager_name: str = "work_emi_team_manager") -> "ToolResult | None":
    """Drive ONE node to a yield/finish through the standard manager loop. Returns the manager's
    ToolResult — EXACTLY what a normal manager-as-tool call returns — so a node handoff can surface the
    SAME agent-facing answer as any manager call (None only if the manager produced no result). The
    node's resulting STATUS is a graph property: read it via store.load(work_id).nodes[node_id].status.

    manager_name selects the worker stack; the manager's `node_input` config ('task' vs 'render')
    determines how the node is handed to it — no manager-name branching here."""
    _ensure_registered()
    cur = store.load(work_id).nodes[node_id]
    # On pickup, flip the node to `dispatched` = in-flight. `actionable` is included because the
    # state_mover gate now dispatches nodes from `actionable` (not `proposed`); without this an in-flight node
    # stays `actionable` — the action_selector could re-dispatch it, and the close below (->done) is illegal.
    if cur.status in {"proposed", "waiting", "actionable"}:
        store.apply("set_status", {"work_id": work_id, "node_id": node_id, "status": "dispatched"}, actor="manager")

    # The manager's `node_input` config decides how its node is handed to it (no manager-name check):
    #   "task"   -> feed the node content as the task (+ deps as information), web_manager-style; the
    #               planner researches it and its reconcile mirrors checklist/findings into the graph.
    #   "render" -> the manager's render node loads the node into its projection; just tell it to advance.
    config = DI.manager_registry.get(manager_name) or {}
    node_input = config.get("node_input", "render")
    actor = manager_name   # provenance for graph writes = the manager discharging the node
    manager = DI.multi_agent_manager_factory.create_manager(
        manager_name, name=f"{manager_name}_{uuid.uuid4().hex[:8]}")
    token = set_work_context(store, work_id, node_id, actor=actor)
    try:
        if node_input == "task":
            cur = store.load(work_id).nodes[node_id]
            msg = Message(scope_context=orchestrator_scope(work_id=work_id), task=(cur.content or cur.title),
                          information=_render_dependencies(store.load(work_id), node_id),
                          request_id=f"work::{uuid.uuid4()}")
        else:
            cur = store.load(work_id).nodes[node_id]
            # Render mode hands the node's DETAIL via work_projection (the render node) and keeps the
            # message task generic. But if that side-channel comes back empty (missing work context /
            # a render failure), "advance the node you own" with no detail makes the worker INVENT a
            # task and persist the hallucination as the node's [done] result (the 06-23 "blockchain
            # validator" contamination). Carry the node's real goal in the message so the worker stays
            # grounded even when the projection is degraded.
            goal_txt = (cur.content or cur.title or "").strip()
            task = f"Advance the node you own: {goal_txt}" if goal_txt else "Advance the node you own."
            msg = Message(scope_context=orchestrator_scope(work_id=work_id), task=task,
                          request_id=f"work::{uuid.uuid4()}")
        result = DI.manager_invoker.invoke(manager, msg)
    finally:
        reset_work_context(token)

    # The manager's AGENT-FACING final answer — exactly what a normal manager-as-tool call returns to
    # its caller (final_answer's synthesis). run_node only returns a status, so without this the answer
    # is lost and the node-handoff would surface a raw pod dump instead of a clean "here is the result"
    # — the caller then can't tell the work is done. Persist it as the node's content (its result) so
    # the handoff returns it and a later peek shows it.
    # Prefer the final_answer's structured field (the CLEAN answer). `content` may be the agent's raw
    # JSON ({"final_answer_answer": "..."}), so use it only when no structured field is present.
    answer = ""
    if result is not None:
        rdata = getattr(result, "data", {}) or {}
        for _k in ("final_answer_answer", "final_answer", "answer"):
            _v = rdata.get(_k)
            if _v:
                answer = str(_v).strip()
                break
        if not answer:
            answer = (getattr(result, "content", "") or "").strip()

    # If the manager surfaced a research pod (via final_answer), attach it as the node's OUTCOME.
    # Unconditional: a no-op for managers that don't surface pod_references.
    data = getattr(result, "data", None) or {}
    pod_refs = data.get("pod_references") or []
    pod_id = next((str(r.get("pod_id")) for r in pod_refs
                   if isinstance(r, dict) and r.get("pod_id")), None)
    if pod_id and not store.load(work_id).nodes[node_id].pod_ref:
        store.apply("attach_pod", {"work_id": work_id, "node_id": node_id, "pod_ref": pod_id}, actor=actor)

    # Close the owned node. The node managers reuse the PRODUCTION final_answer (a plain Agent that does
    # NOT mint a graph verdict), so this is the PRIMARY status-setter: clean exit -> done; aborted /
    # errored -> failed. The node's `content` is its DIRECTIVE — its identity — and is NEVER overwritten;
    # the manager's final answer (the RESULT on success, the WHY on failure) is recorded as an EVIDENCE node
    # the spine node produced (a graph row under it), matching how the worker mints its findings.
    final = store.load(work_id).nodes[node_id]
    if final.status not in {"done", "failed", "abandoned", "verified", "passed"}:
        failed = bool(data.get("aborted")) or data.get("exit_state") == "error_exit"
        store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                                   "status": "failed" if failed else "done"}, actor=actor)
        if answer:
            from work_objects.model import new_id
            store.apply("add_node",
                        {"work_id": work_id, "id": new_id("result"), "type": "evidence",
                         "parent_id": node_id, "status": "assumed", "created_by": actor,
                         "title": "manager result" if not failed else "manager failure (why)",
                         "content": answer},
                        actor=actor)

    # Hand back the manager's ToolResult VERBATIM — the node handoff returns it as-is, so calling a
    # node-manager is no different from calling any manager. (Status is a graph property, read above.)
    return result


def work_on(store, work_id: str, node_id: str | None = None,
            manager_name: str = "work_emi_team_manager", now=None, max_passes: int = 200) -> str:
    """Standalone execution arm — advance a WorkObject (or one node of it) WITHOUT the
    parallel orchestrator. The light, single-manager driver (tier 2):

      - node_id given -> run THAT node via run_node (the dayflow / hand-off-a-ready-node
        case: dayflow schedules, work_on executes one node).
      - node_id None  -> drive the ready top-level task nodes (parent == goal) one at a
        time via run_node until the goal is satisfied or nothing is runnable NOW; returns
        "parked" if only future-wake nodes remain (the caller re-invokes when due — no
        time fast-forward, so a "wait N days" node is never run early).

    The work_emi_team_manager discharges each node; results land on the graph. Returns
    the node's (or WorkObject's) final status."""
    from work_objects.model import utcnow
    _ensure_registered()

    if node_id is not None:
        run_node(store, work_id, node_id, manager_name=manager_name)
        n = store.load(work_id).nodes.get(node_id)
        return n.status if n else "missing"

    now = now or utcnow()
    done_ids: set[str] = set()
    for _ in range(max_passes):
        wo = store.load(work_id)
        if wo.status == "done":
            return "done"
        goal = wo.goal_node_id
        ready = [n for n in wo.ready_nodes(now)
                 if n.id != goal and n.parent_id == goal and n.id not in done_ids]
        if not ready:
            has_future = any(n.wake_at is not None and n.wake_at > now
                             for n in wo.nodes.values() if n.status in {"proposed", "waiting"})
            return "parked" if has_future else store.load(work_id).status
        for n in ready:
            done_ids.add(n.id)
            run_node(store, work_id, n.id, manager_name=manager_name)
    return store.load(work_id).status


def _render_dependencies(wo, node_id: str) -> str:
    """Upstream (depends_on) nodes' produced content, rendered as `information` for the planner."""
    dep_ids = [e.src for e in wo.edges if e.dst == node_id and e.relation == "depends_on"]
    if not dep_ids:
        return ""
    lines = ["You can build directly on these already-completed upstream results:"]
    for did in dep_ids:
        d = wo.nodes.get(did)
        if d is None:
            continue
        for e in wo.edges:
            if e.src == did and e.relation == "produces" and e.dst in wo.nodes:
                p = wo.nodes[e.dst]
                detail = (p.content or p.pod_ref or "").strip()
                if detail:
                    lines.append(f"- {d.title}: {detail}")
    return "\n".join(lines) if len(lines) > 1 else ""


def render_graph_view(wo) -> str:
    """A compact text rendering of a WorkObject: the goal + subtask statuses + every
    Evidence/Artifact finding gathered. Used by scenarios to show graph state at a glance."""
    goal = wo.nodes.get(wo.goal_node_id or "")
    L = [f"GOAL: {goal.title if goal else ''}"]
    if goal and goal.content:
        L.append(goal.content)
    L.append("\nNODES:")
    for n in wo.nodes.values():
        if n.type == "subtask":
            L.append(f"- [{n.status}] {n.title}")
    L.append("\nEVIDENCE / FINDINGS gathered:")
    evs = [n for n in wo.nodes.values() if n.type in {"evidence", "artifact"}]
    for ev in evs:
        detail = (ev.content or ev.pod_ref or ev.title or "").strip()
        L.append(f"- {detail}")
    if not evs:
        L.append("(none yet)")
    return "\n".join(L)
