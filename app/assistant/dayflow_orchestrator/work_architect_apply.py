"""dayflow_orchestrator.work_architect_apply — lay a work_architect DAG onto a work object's graph.

The architect emits node specs (node_id slug, title, detail, depends_on, wake_kind/wake_at/wake_ref).
This prepares nodes, dependency edges and wait gates, then commits them as one validated
revision. A rejected operation leaves the previous graph unchanged. The LLM reasoning already happened in the
agent; this is pure mechanics. wake_at is parsed to a datetime (the substrate stores it typed and
is_ready compares it to now).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _to_dt(s):
    if s is None:
        return None
    if isinstance(s, datetime):
        if s.tzinfo is not None:
            return s
        from app.assistant.utils.time_utils import get_local_timezone
        return s.replace(tzinfo=get_local_timezone())
    txt = str(s).strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError as exc:
        raise ValueError(f"architect: invalid wake_at {s!r}") from exc
    if dt.tzinfo is None:
        # The architect's prompt asks for an offset, but the LLM can omit one. A NAIVE datetime
        # in the store poisons every aware comparison downstream — the soonest-first wake sort
        # raised TypeError and armed ZERO wakes that tick (verification finding). The architect
        # reads and writes the user's LOCAL clock ("Current time" in its prompt is local), so a
        # naive value is a local one; taking it as UTC shifted every such wake by the offset.
        from app.assistant.utils.time_utils import get_local_timezone
        logger.warning("apply_architect_dag: wake_at %r has no offset — taking it as local time", s)
        dt = dt.replace(tzinfo=get_local_timezone())
    return dt


_ABANDON_SKIP = {"done", "closed", "abandoned", "superseded"}  # finished/finalized — left as a record


def _work_ns(work_id: str) -> str:
    """Short per-work-object suffix for namespacing architect slugs."""
    return work_id.split("_")[-1][:6]


def _dedupe_prunes(wo, duplicate_of: Dict[str, str] | None, goal_id: str,
                   abandon_node_ids=()) -> Dict[str, str]:
    """Validate the ENTIRE duplicate mapping before preparing a revision.

    Only existing unfinished main tasks may be consolidated. Each duplicate must
    name a distinct live survivor which this revision keeps. Semantic equivalence
    is the architect's judgment; identity, lifecycle and survival are checked here.
    Invalid entries reject the revision instead of disappearing from its result.
    """
    out: Dict[str, str] = {}
    for dup, keep in (duplicate_of or {}).items():
        dup, keep = str(dup or "").strip(), str(keep or "").strip()
        if not dup or not keep or dup == keep:
            raise ValueError("architect: duplicate mapping requires two distinct nonempty task IDs")
        if dup in out:
            raise ValueError(f"architect: duplicate task {dup!r} is mapped more than once")
        if dup == goal_id or keep == goal_id:
            raise ValueError("architect: cannot deduplicate the goal node")
        for nid in (dup, keep):
            node = wo.nodes.get(nid)
            if node is None or not wo.is_work_unit(node):
                raise ValueError(f"architect: duplicate mapping names unknown or non-main task {nid!r}")
            if node.status in _ABANDON_SKIP:
                raise ValueError(f"architect: duplicate mapping names finished task {nid!r} ({node.status})")
        out[dup] = keep
    dropping = set(out) | {str(nid).strip() for nid in (abandon_node_ids or [])}
    for dup, keep in out.items():
        if keep in dropping:
            raise ValueError(f"architect: duplicate {dup!r} retains {keep!r}, which this revision also drops")
    return out


def apply_architect_dag(store, work_id: str, nodes: List[Dict[str, Any]],
                        abandon_node_ids: List[str] | None = None,
                        abandon_reason: str = "",
                        licensed: bool = False,
                        duplicate_of: Dict[str, str] | None = None,
                        finalizer_instructions: list[dict] | None = None,
                        expected_updated_at=None) -> Dict[str, Any]:
    """Apply an architect DELTA onto a work object's graph. On a fresh decompose it just adds nodes; on a
    RE-PLAN it can also PRUNE: abandon each `abandon_node_ids` node + its un-finished ownership subtree
    (the store does not cascade — dependent-pruning #54 — so we recurse children here, leaving done/closed
    nodes as a record). Then add new nodes, wire depends_on, set wake-gates. Idempotent on node_id.

    `licensed` marks a replan backed by EVIDENCE — a finalizer amend or a user directive, computed
    deterministically by the CALLER from typed flags, never from wording. Only a licensed replan may
    abandon queued (`actionable`) or held (future-wake `waiting`) nodes; the store's churn fence
    refuses those prunes otherwise, aborting the entire revision.
    Returns {added, edges, waits, abandoned}."""
    wo = store.load(work_id)
    operations = []
    def queue(op, data, actor=None):
        operations.append({"op": op, "data": data})
    goal_id = wo.goal_node_id
    ns = _work_ns(work_id)

    # Architect references name assigned tasks, never a worker's provenance records.
    # Validate before any writes; recursive cleanup of a pruned main task is separate.
    refs = list(abandon_node_ids or []) + list((duplicate_of or {}).keys()) + list((duplicate_of or {}).values())
    for spec in nodes or []:
        if isinstance(spec, dict):
            refs.append(spec.get("node_id"))
            refs.extend(spec.get("depends_on") or [])
    for ref in refs:
        existing_node = wo.nodes.get(str(ref or "").strip())
        if existing_node is not None and not wo.is_work_unit(existing_node):
            raise ValueError("architect: references must name main tasks, not worker provenance or the goal")

    # 0) PRUNE pass (re-plan) — abandon the named moot nodes + their un-finished subtrees.
    #    Duplicates are pruned here too, but carry their OWN licence and their own epitaph: the
    #    node is abandoned rather than deleted, so the graph keeps the record of the duplication.
    dupes = _dedupe_prunes(wo, duplicate_of, goal_id, abandon_node_ids)
    abandoned: List[str] = []
    deduplicated: List[str] = []
    seen: set[str] = set()
    stack = [str(x).strip() for x in (abandon_node_ids or []) if str(x).strip()]
    stack.extend(d for d in dupes if d not in stack)
    while stack:
        nid = stack.pop()
        if nid in seen or nid == goal_id or nid not in wo.nodes:
            continue
        seen.add(nid)
        if wo.nodes[nid].status in _ABANDON_SKIP:
            continue   # finished/already-gone — preserve the record, don't recurse a done branch
        kept = dupes.get(nid)
        queue("set_status", {
            "work_id": work_id, "node_id": nid, "status": "abandoned",
            "verdict": "deduplicated" if kept else "pruned_by_replan",
            # A verified duplicate licenses its own prune (see _dedupe_prunes); everything
            # else still needs the caller's evidence-backed licence.
            "licensed": True if kept else licensed,
            "reason": (f"duplicate of {kept}, which is kept and will run — this copy was a "
                       f"second node for the same work" if kept else
                       (abandon_reason or "architect re-plan pruned this branch (no stated reason)")),
        }, actor="architect")
        abandoned.append(nid)
        if kept:
            deduplicated.append(nid)
        stack.extend(c for c in wo.children_of(nid) if c not in seen)

    existing = set(wo.nodes.keys())
    specs = [n for n in (nodes or []) if isinstance(n, dict)]

    # Node ids are GLOBALLY unique in the store (nodes.id is the table PK), but the
    # architect's slugs are meaningful and RECUR across work objects — every nightly
    # plan mints "book_ac_service" again. An un-namespaced re-mint INSERT OR REPLACEs
    # the row and steals it from the earlier graph (audit W1: 10 slugs re-minted
    # across 2-7 WOs, 53 dangling-parent nodes, and a still-active victim becomes
    # unwritable). Namespace each NEW slug with this WO's suffix; the remap is
    # batch-wide so intra-plan depends_on references keep working, and a reference
    # to an id the graph already carries (re-plan against the projection's real
    # ids) passes through untouched.
    def _real(slug: str) -> str:
        return slug if slug in existing else f"{slug}--{ns}"

    # 1) nodes (id = the architect's slug, namespaced per work object)
    added: List[str] = []
    for spec in specs:
        raw_nid = str(spec.get("node_id") or "").strip()
        title = str(spec.get("title") or "").strip()
        detail = str(spec.get("detail") or "").strip()
        if not raw_nid or not (title or detail):
            continue
        nid = _real(raw_nid)
        if nid in existing:
            continue
        queue("add_node", {
            "work_id": work_id, "id": nid, "type": "subtask", "parent_id": goal_id,
            "title": title or detail[:60], "content": detail,
        }, actor="architect")
        existing.add(nid)
        added.append(nid)

    # 2) dependency edges (both endpoints must exist now)
    edges = 0
    for spec in specs:
        raw_nid = str(spec.get("node_id") or "").strip()
        if not raw_nid:
            continue
        nid = _real(raw_nid)
        if nid not in existing:
            continue
        for dep in spec.get("depends_on", []) or []:
            dep = _real(str(dep).strip()) if str(dep).strip() else ""
            if not dep or dep not in existing or dep == nid:
                raise ValueError(f"architect: invalid dependency {dep!r} for {nid!r}")
            if dep and dep in existing and dep != nid:
                queue("add_edge", {
                    "work_id": work_id, "src": dep, "dst": nid, "relation": "depends_on",
                }, actor="architect")
                edges += 1

    # 3) wake-gates — the architect authors two primitives, wake_at | wake_ref (+ an `ask` flag); DERIVE the
    #    substrate wake_kind here so the store / state_mover / dispatch are unchanged. wake_at -> a
    #    deterministic time wake; wake_ref -> a prose external-event wake the state_mover matches; ask ->
    #    the user_reply ask (wake_ref carries the question).
    waits = 0
    for spec in specs:
        raw_nid = str(spec.get("node_id") or "").strip()
        if not raw_nid:
            continue
        nid = _real(raw_nid)
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
            queue("defer_node", {"work_id": work_id, "node_id": nid, "wake_kind": wk,
                                       "wake_at": wake_at, "wake_ref": wake_ref}, actor="architect")
            waits += 1

    if deduplicated:
        queue("redirect_dependencies", {"replacements": {nid: dupes[nid] for nid in deduplicated}})
    for entry in finalizer_instructions or []:
        queue("consume_finalizer_instruction", {"node_id": entry["node_id"],
              "expected_finalizer": {key: value for key, value in entry.items() if key != "node_id"}})
    if operations:
        store.apply("batch", {"work_id": work_id, "expected_updated_at": expected_updated_at or wo.updated_at,
                              "operations": operations}, actor="architect")

    logger.info("apply_architect_dag(%s): +%d nodes, +%d deps, +%d waits, -%d abandoned "
                "(%d de-duplicated)", work_id, len(added), edges, waits, len(abandoned),
                len(deduplicated))
    return {"added": added, "edges": edges, "waits": waits, "abandoned": abandoned,
            "deduplicated": deduplicated}
