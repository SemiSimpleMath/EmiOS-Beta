"""
work_objects.discharge — the ONE primitive that runs a work node.

Contract (work-session rewrite, 2026-08-04):

  discharge_node(store, work_id, node_id, *, scope_context, manager_name,
                 session_id=None) -> ToolResult | None

  * ``scope_context`` is REQUIRED. Authority is always DERIVED by the caller —
    the dayflow WorkSession passes the orchestrator room's scope, the task
    runner passes its run scope, scenarios pass what they declare. Nothing at
    this layer mints a scope; a missing scope raises (fail-loud doctrine).
    The 2026-08-03 forward-email flounder came from this layer self-minting a
    scope whose pod policy had drifted from the room's.
  * ``session_id`` (when given) is stamped on an already-claimed node
    (``payload.session_id``) and inherited by every node grown under it, so
    ownership is a graph fact rather than a live thread. NOTE: nothing reads it
    today — the supervisor it was written for (sweep_stuck_work_nodes) was
    rewritten to a pure subtree-idle rule and consults no session at all.
  * Recording preserves the node's ``content`` directive; the
    manager's final answer lands as an EVIDENCE child (the result), any
    surfaced research pod is attached. The result status, pod attachment and evidence are committed atomically
    after the dispatch-epoch check. This layer does not finalize.

The node managers (work_emi_team_manager, work_web_manager) are ordinary
first-class configs; this module only drives one node through the standard
manager runtime. Replaces work_runtime.py (run_node/work_on), which replaced
agent_runner.py.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolResult

logger = get_logger(__name__)

from work_objects.runtime import reset_work_context, set_work_context
from work_objects.runtime_setup import ensure_manager_services
from work_objects.work_tools import register_work_tools

_registered = False


def _ensure_registered() -> None:
    """Make the shared services available before driving a node: manager-runtime services, the static
    manager configs (incl. the node managers), and the work_* graph tools."""
    global _registered
    if _registered:
        return
    ensure_manager_services()
    DI.manager_registry.preload_all()
    register_work_tools(DI.tool_registry)
    _registered = True


def discharge_node(store, work_id: str, node_id: str, *, scope_context,
                   manager_name: str = "work_emi_team_manager",
                   session_id: str | None = None) -> "ToolResult | None":
    from app.assistant.manager_runtime.execution import current_owner, Owner, REGISTRY
    if current_owner() is not None:
        return _discharge_node_body(store, work_id, node_id, manager_name=manager_name,
                                    scope_context=scope_context, session_id=session_id)
    node = store.load(work_id).nodes[node_id]
    owner = Owner(store, work_id, node_id, int(node.payload.get("dispatch_epoch") or 0))
    store.start_execution(owner)
    try:
        with REGISTRY.span("attempt", "discharge_node", owner=owner, attribution=node_id):
            return _discharge_node_body(store, work_id, node_id, manager_name=manager_name,
                                        scope_context=scope_context, session_id=session_id)
    finally:
        REGISTRY.finish_owner(owner)


def _discharge_node_body(store, work_id: str, node_id: str, *, scope_context,
                   manager_name: str = "work_emi_team_manager",
                   session_id: str | None = None) -> "ToolResult | None":
    """Drive ONE node to a yield/finish through the standard manager loop. Returns the manager's
    ToolResult — exactly what a normal manager-as-tool call returns. The node's resulting STATUS is a
    graph property: read it via store.load(work_id).nodes[node_id].status.

    manager_name selects the worker stack; the manager's `node_input` config ('task' vs 'render')
    determines how the node is handed to it — no manager-name branching here."""
    if scope_context is None:
        raise ValueError(
            f"discharge_node({work_id}::{node_id}): scope_context is required — authority derives "
            "from the caller (room / task run / scenario); this layer never mints a scope.")
    _ensure_registered()
    cur = store.load(work_id).nodes[node_id]
    # THE GATE CLAIMS, NOT US. work_node_dispatch_node marks the node `dispatched` before calling
    # any tool, so by the time we run it is already in flight and every other consumer already sees
    # that. We only stamp session ownership on it — retained metadata. A node that
    # is somehow NOT claimed is a dispatch bug, and it is loud rather than silently re-claimed.
    if cur.status != "dispatched":
        raise ValueError(
            f"discharge_node({work_id}::{node_id}): node is {cur.status!r}, expected 'dispatched' — "
            f"the dispatch gate claims a node before any tool is called.")
    if session_id and cur.payload.get("session_id") != session_id:
        store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                                   "status": cur.status, "session_id": session_id}, actor="manager")
    # ONE post-claim snapshot serves the epoch capture and the prompt build.
    snapshot = store.load(work_id)
    cur = snapshot.nodes[node_id]
    # Capture MY incarnation — the fenced close at the tail rejects a stale epoch (audit W2).
    my_epoch = int(cur.payload.get("dispatch_epoch") or 0)

    # The manager's `node_input` config decides how the node is handed to it:
    #   "task"   -> node content as the task (+ deps' results as information), web_manager-style.
    #   "render" -> the manager's render node loads the graph projection; the message still carries
    #               the node's real goal so a degraded projection can't make the worker invent a task
    #               (the 06-23 contamination).
    config = DI.manager_registry.get(manager_name) or {}
    node_input = config.get("node_input", "render")
    actor = manager_name
    # THE STANDARD MANAGER-AS-TOOL CALL. This used to hand-roll create_manager +
    # manager_invoker.invoke with a Message of its own shape, which meant the work lane was
    # the one caller in the system that skipped ScopeAdapter.for_sub_manager — the single
    # seam where a sub-manager's scope is constructed — and turned a manager failure into a
    # raised exception instead of the structured tool error every other caller receives.
    # ManagerInterface.invoke_on is that call, shared: same scope construction, same
    # task_request message, same error contract.
    iface = ManagerInterface(manager_name)
    token = set_work_context(store, work_id, node_id, actor=actor)
    try:
        if node_input == "task":
            result = iface.invoke_on(task=(cur.content or cur.title),
                                     information=_render_dependencies(snapshot, node_id),
                                     scope_context=scope_context)
        else:
            goal_txt = (cur.content or cur.title or "").strip()
            from app.assistant.dayflow_orchestrator.work_context import render_view
            result = iface.invoke_on(task=render_view("discharge_task", directive=goal_txt),
                                     information="", scope_context=scope_context)
    finally:
        reset_work_context(token)

    # ONE CHOKE POINT. What the result MEANS is the finalizer's; turning it into graph state is
    # result_recorder's, for every dispatch lane alike. This used to be inlined here — and was
    # skipped entirely whenever the worker had already set its own status, so the caller recorded
    # nothing at all, not even the result evidence.
    from work_objects.result_recorder import record_tool_result
    record_tool_result(store, work_id, node_id, result, actor=actor, expected_epoch=my_epoch,
                       evidence_title="manager result")
    return result


def drive_work(store, work_id: str, *, scope_context, node_id: str | None = None,
               manager_name: str = "work_emi_team_manager", now=None,
               max_passes: int = 200) -> str:
    """Bounded standalone scenario runner using normal claim/result/finalizer APIs.

    Operates only on main tasks. Stops for time/event gates, pending revisions or
    a judged failure; it does not implement architect replanning or automatic
    retries. An explicit node may be ready, already dispatched, or awaiting
    finalization. Caller owns the store and scope; no global Dayflow writes.
    """
    from work_objects.model import utcnow
    from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
    if scope_context is None:
        raise ValueError("drive_work requires caller-provided scope")
    if max_passes < 1:
        raise ValueError("max_passes must be positive")
    finalizer = WorkFinalizerNode("scenario_finalizer", None, None, None)
    for _ in range(max_passes):
        wo = store.load(work_id)
        if wo.status in {"done", "abandoned"}:
            return wo.nodes[node_id].status if node_id is not None else wo.status
        clock_now = now or utcnow()
        if node_id is not None:
            node = wo.nodes[node_id]
            if not wo.is_work_unit(node):
                raise ValueError("drive_work only dispatches main tasks")
        else:
            pending = [n for n in wo.nodes.values() if wo.needs_finalization(n)]
            ready = [n for n in wo.ready_nodes(clock_now) if wo.is_work_unit(n)]
            node = next(iter(pending or ready), None)
            if node is None:
                future = any(n.wake_at is not None and n.wake_at > clock_now
                             for n in wo.nodes.values() if wo.is_work_unit(n)
                             and n.status in {"proposed", "waiting", "actionable"})
                return "parked" if future else wo.status
        if not wo.needs_finalization(node):
            if node.status != "dispatched":
                if not wo.is_ready(node, clock_now):
                    return node.status if node_id is not None else wo.status
                if node.status != "actionable":
                    store.apply("set_status", {"work_id":work_id, "node_id":node.id,
                                               "status":"actionable"}, actor="scenario")
                # The shared atomic claim rechecks real current gates and ownership.
                store.apply("claim_task", {"work_id":work_id, "node_id":node.id}, actor="dispatch_gate")
            discharge_node(store, work_id, node.id, scope_context=scope_context,
                           manager_name=manager_name)
        finalizer.finalize_recorded(store, work_id, node.id, scope_context=scope_context)
        updated = store.load(work_id)
        if node_id is not None:
            return updated.nodes[node_id].status
        if updated.nodes[node.id].status != "closed" or updated.has_pending_revision():
            return updated.status
    return store.load(work_id).status


def _render_dependencies(wo, node_id: str) -> str:
    """Upstream (depends_on) nodes' RESULTS as `information` for the planner. A node's result is the
    EVIDENCE it produced (evidence/artifact children or produces-linked) — never its `content`, which
    is the node's directive/identity."""
    dep_ids = [e.src for e in wo.edges if e.dst == node_id and e.relation == "depends_on"]
    if not dep_ids:
        return ""
    dependencies = []
    for did in dep_ids:
        d = wo.nodes.get(did)
        if d is None:
            continue
        produced = {e.dst for e in wo.edges if e.src == did and e.relation == "produces"}
        parts = [(m.content or m.pod_ref or "").strip() for m in wo.nodes.values()
                 if (m.parent_id == did or m.id in produced)
                 and getattr(m, "type", "") in ("evidence", "artifact") and (m.content or m.pod_ref)]
        if parts:
            dependencies.append({"title": d.title, "results": parts})
    from app.assistant.dayflow_orchestrator.work_context import render_view
    return render_view("discharge_dependencies", dependencies=dependencies).strip()



def render_graph_view(wo) -> str:
    """Compact text rendering of a WorkObject: goal + subtask statuses + findings. Scenario display."""
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
