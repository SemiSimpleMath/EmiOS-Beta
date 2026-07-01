"""dayflow_orchestrator.work_architect_apply — lay a work_architect DAG onto a work object's graph.

The architect emits node specs (node_id slug, title, detail, depends_on, wake_kind/wake_at/wake_ref).
This projects them into the graph in three deterministic passes: add_node (id = the slug, under the
goal) -> add_edge depends_on -> defer_node for wait-gates. The LLM reasoning already happened in the
agent; this is pure mechanics. wake_at is parsed to a datetime (the substrate stores it typed and
is_ready compares it to now).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _to_dt(s):
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    txt = str(s).strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(txt)
    except Exception:
        logger.warning("apply_architect_dag: unparseable wake_at %r — node will not be time-gated", s)
        return None


_ABANDON_SKIP = {"done", "closed", "abandoned", "superseded"}  # finished/finalized — left as a record


def apply_architect_dag(store, work_id: str, nodes: List[Dict[str, Any]],
                        abandon_node_ids: List[str] | None = None) -> Dict[str, Any]:
    """Apply an architect DELTA onto a work object's graph. On a fresh decompose it just adds nodes; on a
    RE-PLAN it can also PRUNE: abandon each `abandon_node_ids` node + its un-finished ownership subtree
    (the store does not cascade — dependent-pruning #54 — so we recurse children here, leaving done/closed
    nodes as a record). Then add new nodes, wire depends_on, set wake-gates. Idempotent on node_id.
    Returns {added, edges, waits, abandoned}."""
    wo = store.load(work_id)
    goal_id = wo.goal_node_id

    # 0) PRUNE pass (re-plan) — abandon the named moot nodes + their un-finished subtrees.
    abandoned: List[str] = []
    seen: set[str] = set()
    stack = [str(x).strip() for x in (abandon_node_ids or []) if str(x).strip()]
    while stack:
        nid = stack.pop()
        if nid in seen or nid == goal_id or nid not in wo.nodes:
            continue
        seen.add(nid)
        if wo.nodes[nid].status in _ABANDON_SKIP:
            continue   # finished/already-gone — preserve the record, don't recurse a done branch
        try:
            store.apply("set_status", {"work_id": work_id, "node_id": nid, "status": "abandoned"}, actor="architect")
            abandoned.append(nid)
        except Exception as e:
            logger.warning("apply_architect_dag: abandon %s failed: %s", nid, e)
            continue
        stack.extend(c for c in wo.children_of(nid) if c not in seen)

    existing = set(wo.nodes.keys())
    specs = [n for n in (nodes or []) if isinstance(n, dict)]

    # 1) nodes (id = the architect's slug, so depends_on can reference them directly)
    added: List[str] = []
    for spec in specs:
        nid = str(spec.get("node_id") or "").strip()
        title = str(spec.get("title") or "").strip()
        detail = str(spec.get("detail") or "").strip()
        if not nid or nid in existing or not (title or detail):
            continue
        store.apply("add_node", {
            "work_id": work_id, "id": nid, "type": "subtask", "parent_id": goal_id,
            "title": title or detail[:60], "content": detail,
        }, actor="architect")
        existing.add(nid)
        added.append(nid)

    # 2) dependency edges (both endpoints must exist now)
    edges = 0
    for spec in specs:
        nid = str(spec.get("node_id") or "").strip()
        if nid not in existing:
            continue
        for dep in spec.get("depends_on", []) or []:
            dep = str(dep).strip()
            if dep and dep in existing and dep != nid:
                store.apply("add_edge", {
                    "work_id": work_id, "src": dep, "dst": nid, "relation": "depends_on",
                }, actor="architect")
                edges += 1

    # 3) wake-gates — the architect authors two primitives, wake_at | wake_ref (+ an `ask` flag); DERIVE the
    #    substrate wake_kind here so the store / state_mover / dispatch are unchanged. wake_at -> a
    #    deterministic time wake; wake_ref -> a prose external-event wake the state_mover matches; ask ->
    #    the user_reply ask (wake_ref carries the question).
    waits = 0
    for spec in specs:
        nid = str(spec.get("node_id") or "").strip()
        if nid not in existing:
            continue
        wake_at = _to_dt(spec.get("wake_at"))
        wake_ref = spec.get("wake_ref")
        if wake_at:
            wk = "time"
        elif str(wake_ref or "").strip():
            wk = "event"
        else:
            wk = None
        if wk:
            store.apply("defer_node", {"work_id": work_id, "node_id": nid, "wake_kind": wk,
                                       "wake_at": wake_at, "wake_ref": wake_ref}, actor="architect")
            waits += 1

    logger.info("apply_architect_dag(%s): +%d nodes, +%d deps, +%d waits, -%d abandoned",
                work_id, len(added), edges, waits, len(abandoned))
    return {"added": added, "edges": edges, "waits": waits, "abandoned": abandoned}
