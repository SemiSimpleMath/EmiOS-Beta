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


def apply_architect_dag(store, work_id: str, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Add the architect's nodes as subtasks under the goal, wire depends_on edges, set wake-gates.
    Idempotent on node_id (a slug already present is skipped). Returns {added, edges, waits}."""
    wo = store.load(work_id)
    goal_id = wo.goal_node_id
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

    # 3) wake-gates
    waits = 0
    for spec in specs:
        nid = str(spec.get("node_id") or "").strip()
        wk = spec.get("wake_kind")
        if nid in existing and wk:
            store.apply("defer_node", {
                "work_id": work_id, "node_id": nid, "wake_kind": wk,
                "wake_at": _to_dt(spec.get("wake_at")), "wake_ref": spec.get("wake_ref"),
            }, actor="architect")
            waits += 1

    logger.info("apply_architect_dag(%s): +%d nodes, +%d deps, +%d waits", work_id, len(added), edges, waits)
    return {"added": added, "edges": edges, "waits": waits}
