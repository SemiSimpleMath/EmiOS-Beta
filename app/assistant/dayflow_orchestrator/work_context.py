"""Structured graph views. Agent-facing text belongs in shared/work Jinja templates."""
from app.assistant.utils.path_utils import get_repo_root
from collections import Counter
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from work_objects.model import utcnow

_TEMPLATE_ROOT = get_repo_root() / "app" / "assistant" / "agents"
_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATE_ROOT)), undefined=StrictUndefined,
                   keep_trailing_newline=True, finalize=lambda value: "" if value is None else value)

from app.assistant.utils.time_utils import local_time_text
_ENV.filters["local_time"] = local_time_text

def render_view(template, **data):
    return _ENV.get_template("shared/work/" + template + ".j2").render(**data)

def local_stamp(value):
    return local_time_text(value)

def record_data(record):
    content = record.content
    if not content and record.pod_ref:
        from work_objects.work_tools import pod_summary
        content = pod_summary(record.pod_ref)
    return {"id": record.id, "parent_id": record.parent_id, "type": record.type,
            "title": record.title, "status": record.status, "content": content,
            "pod_ref": record.pod_ref, "terminal": record.payload.get("terminal"),
            # The main task's attempt that wrote this evidence: a result carries `dispatch_epoch`, a
            # planner finding `finding_attempt`. Other nodes' dispatch_epoch is their own counter.
            "attempt": (record.payload.get("finding_attempt") or record.payload.get("dispatch_epoch"))
                       if record.type == "evidence" else None}

def source_context(wo):
    sources = [dict(s) for s in wo.constraints.get("source_intake") or []]
    # Source context survives replanning without exposing worker execution history to planners.
    for n in wo.nodes.values():
        source = n.payload.get("external_source")
        if source:
            sources.append({**source, "matched_condition": n.payload.get("matched_condition"),
                            "interpretation": n.content, "task_id": n.parent_id,
                            "for_attempt": n.payload.get("for_attempt")})
    return sources


def watch_data(watch):
    """A standing watch (store `watch_node`) as its readers see it, in local times; None for a task that runs once."""
    if not watch:
        return None
    return {"condition": watch["condition"], "until": local_stamp(watch["until"]),
            "waiting_since": local_stamp(watch["waiting_since"]), "ended": local_stamp(watch.get("ended_at")),
            "runs": [{"attempt": r["attempt"], "at": local_stamp(r["at"]), "outcome": r["outcome"]}
                     for r in watch.get("runs") or []]}


def task_data(wo, node, now=None):
    epoch = int(node.payload.get("dispatch_epoch") or 0)
    return {"id": node.id, "title": node.title, "directive": node.content or node.title,
            "status": node.status, "epoch": epoch, "success_kind": node.satisfied_when_kind,
            "failure_count": int(node.payload.get("failure_count") or 0),
            "finalizer": node.payload.get("finalizer"), "terminal": node.payload.get("terminal"),
            "result_error_code": node.payload.get("result_error_code"),
            "awaiting_judgment": wo.needs_finalization(node),
            "wake_kind": node.wake_kind if node.status in {"proposed", "actionable", "waiting", "dispatched"} else None,
            "wake_at": local_stamp(node.wake_at), "wake_ref": node.wake_ref,
            "watch": watch_data(node.payload.get("watch")),
            "dependencies": [{"id": nid, "status": wo.nodes[nid].status if nid in wo.nodes else "missing",
                              "satisfied": nid in wo.nodes and wo.is_satisfied(wo.nodes[nid])}
                             for nid in wo.deps_of(node.id)]}

def concern_context(wo):
    """The concerns this work object serves, each with its record (subconscious/concern_view.py)."""
    refs = wo.constraints.get("concern_refs") or []
    if not refs:
        return []
    from app.assistant.subconscious.concern_view import concerns_for_refs
    return concerns_for_refs(refs)


def work_data(wo, now=None):
    now = now or utcnow()
    goal = wo.nodes.get(wo.goal_node_id)
    tasks = [n for n in wo.nodes.values() if wo.is_work_unit(n)]
    pending = any(isinstance(n.payload.get("finalizer"), dict) and n.payload["finalizer"].get("next_step")
                  and not n.payload["finalizer"].get("consumed_at") for n in tasks)
    contacts = Counter((a.channel, (a.target or "").lower()) for a in wo.actions)
    return {"id": wo.id, "status": wo.status, "objective": (goal.content or goal.title) if goal else wo.title,
            "success_criteria": wo.constraints.get("success_criteria", ""),
            "failure_count": int((goal.payload if goal else {}).get("goal_unmet_attempts") or 0),
            "failure_episode_count": int((goal.payload if goal else {}).get("goal_unmet_since_progress") or 0),
            "tasks": [task_data(wo, n, now) for n in tasks], "pending_replan": pending,
            "completed": sum(wo.is_satisfied(n) for n in tasks),
            "unrun": sum(n.status in {"proposed", "actionable", "waiting", "dispatched"} for n in tasks),
            "repeated_contacts": [{"channel": c, "target": t, "count": count} for (c, t), count in contacts.items() if count >= 3],
            "sources": source_context(wo),
            "concerns": concern_context(wo),
            "execution": wo._execution,
            "actions": [{"when": local_stamp(a.ts), "channel": a.channel, "target": a.target,
                         "summary": a.summary, "outcome": a.outcome} for a in wo.actions]}

def task_uses(wo, task_id):
    """What the planner said a task must read (relation `uses`, the architect since 2026-10-08), each with the id a
    worker opens it by: an earlier task (work_graph_peek), a record or the email the work came from (pod_fetch), other
    work. Until then the architect wrote the slug or the pod id into the task's text."""
    from app.assistant.subconscious import relations
    sources = {s.get("item_id"): s for s in source_context(wo)}
    out = []
    for kind, obj_id in relations.targets(("task", task_id), "uses"):
        if kind == "task":
            node = wo.nodes.get(obj_id)
            out.append({"kind": "task", "id": obj_id, "what": f'the task "{node.title}"' if node else "a task",
                        "how": "work_graph_peek, then work_artifact_fetch"})
        elif kind == "pod":
            out.append({"kind": "record", "id": obj_id, "what": "a stored record", "how": "pod_fetch"})
        elif kind == "source_item":
            s = sources.get(obj_id) or {}
            out.append({"kind": "source", "id": s.get("pod_id") or obj_id,
                        "what": f"the source: {s.get('subject') or s.get('summary') or s.get('source_type') or ''}",
                        "how": "pod_fetch" if s.get("pod_id") else ""})
        else:
            out.append({"kind": kind, "id": obj_id, "what": f"a {kind}", "how": ""})
    return out


def worker_data(wo, node_id):
    node = wo.nodes[node_id]
    records = wo.provenance_for(node_id)
    owner = node
    seen = set()
    while not wo.is_work_unit(owner) and owner.parent_id in wo.nodes and owner.id not in seen:
        seen.add(owner.id)
        owner = wo.nodes[owner.parent_id]
    owning_task = None
    if owner.id != node.id and wo.is_work_unit(owner):
        owning_task = {"task": task_data(wo, owner),
                       "records": [record_data(n) for n in wo.provenance_for(owner.id)]}
    work = work_data(wo)
    # A worker and its delegated helpers are already executing this main-task
    # epoch. Their own live receipts are not competing dispatches. Preserve
    # older epochs, other tasks, cancellation and uncertain external outcomes.
    epoch = int(owner.payload.get("dispatch_epoch") or 0)
    execution = work["execution"] or {}
    def own_current(row):
        return row.get("node_id") == owner.id and int(row.get("epoch") or 0) == epoch
    cancelled = any(own_current(row) and row.get("revoked")
                    for row in execution.get("attempts", []))
    work["execution"] = {
        **execution,
        "attempts": [row for row in execution.get("attempts", [])
                     if not (own_current(row) and not row.get("revoked")
                             and row.get("state") in {"reserved", "running"})],
        "calls": [row for row in execution.get("calls", [])
                  if not (own_current(row) and not cancelled and row.get("state") == "in_flight")],
    }
    # What the main task's earlier attempts' calls returned (receipts are keyed to the main task).
    earlier_receipts = [row for row in execution.get("recent_results", [])
                        if row.get("node_id") == owner.id and int(row.get("epoch") or 0) < epoch]
    return {"owning_task": owning_task, "work": work, "task": task_data(wo, node), "uses": task_uses(wo, owner.id),
            "earlier_receipts": earlier_receipts,
            "records": [record_data(n) for n in records],
            "checklist": [record_data(n) for n in records if n.parent_id == node_id and n.type == "subtask"],
            "facts": [record_data(wo.nodes[e.src]) for e in wo.edges
                      if e.relation == "informs" and e.dst in {node_id, wo.goal_node_id} and e.src in wo.nodes],
            "dependencies": [{"task": task_data(wo, wo.nodes[nid]),
                              "records": [record_data(n) for n in wo.nodes.values()
                                          if n.id in {r.id for r in wo.provenance_for(nid)}
                                          or any(e.relation == "produces" and e.src == nid and e.dst == n.id for e in wo.edges)]}
                             for nid in wo.deps_of(node_id) if nid in wo.nodes]}


def state_mover_candidate(wo, node):
    """Structured timing context shared by ordinary ticks and targeted wakes."""
    goal = wo.nodes.get(wo.goal_node_id)
    return {"task_id": f"{wo.id}::{node.id}", "context": node.title or "",
            "kind": node.type, "objective": (goal.content or goal.title) if goal else wo.title,
            "task": task_data(wo, node)}
