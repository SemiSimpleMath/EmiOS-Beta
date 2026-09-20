"""Structured graph views. Agent-facing text belongs in shared/work Jinja templates."""
from app.assistant.utils.path_utils import get_repo_root
from collections import Counter
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from work_objects.model import utcnow

_TEMPLATE_ROOT = get_repo_root() / "app" / "assistant" / "agents"
_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATE_ROOT)), undefined=StrictUndefined,
                   keep_trailing_newline=True, finalize=lambda value: "" if value is None else value)

def render_view(template, **data):
    return _ENV.get_template("shared/work/" + template + ".j2").render(**data)

def local_stamp(value):
    if value is None:
        return ""
    from app.assistant.utils.time_utils import utc_to_local
    return utc_to_local(value).strftime("%a %m-%d %I:%M %p")

def record_data(record):
    content = record.content
    if not content and record.pod_ref:
        from work_objects.work_tools import pod_summary
        content = pod_summary(record.pod_ref)
    return {"id": record.id, "parent_id": record.parent_id, "type": record.type,
            "title": record.title, "status": record.status, "content": content,
            "pod_ref": record.pod_ref, "terminal": record.payload.get("terminal"),
            "epoch": record.payload.get("dispatch_epoch")}

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
            "dependencies": [{"id": nid, "status": wo.nodes[nid].status if nid in wo.nodes else "missing",
                              "satisfied": nid in wo.nodes and wo.is_satisfied(wo.nodes[nid])}
                             for nid in wo.deps_of(node.id)]}

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
            "actions": [{"when": local_stamp(a.ts), "channel": a.channel, "target": a.target,
                         "summary": a.summary, "outcome": a.outcome} for a in wo.actions]}

def worker_data(wo, node_id):
    node = wo.nodes[node_id]
    records = wo.provenance_for(node_id)
    return {"work": work_data(wo), "task": task_data(wo, node),
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
