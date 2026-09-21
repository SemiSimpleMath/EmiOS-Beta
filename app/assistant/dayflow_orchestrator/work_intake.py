"""Prepare durable source data and render goal content through Jinja."""
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.work_context import render_view


def intake_source(item):
    """Copy attribution from intake, never from a model's wake explanation."""
    meta = get_meta(item)
    return {"item_id": str(meta.get("item_id") or item.get("id") or "").strip(),
            "source_type": str(meta.get("source_type") or ""),
            "summary": str(meta.get("email_summary") or meta.get("summary") or item.get("content") or ""),
            "excerpt": str(meta.get("email_body_excerpt") or ""),
            "pod_id": str(meta.get("pod_id") or ""),
            "sender": str(meta.get("email_sender") or ""),
            "subject": str(meta.get("email_subject") or ""),
            "created_at": str(meta.get("created_at") or item.get("timestamp") or ""),
            "message_id": str(meta.get("linked_email_unified_id") or ""),
            "thread_id": str(meta.get("email_thread_id") or "")}


def source_records(items, references):
    refs = {str(ref).strip() for ref in references}
    records = {}
    for item in items:
        meta = get_meta(item)
        source = intake_source(item)
        item_id = source["item_id"]
        if item_id and ({item_id, str(meta.get("short_id") or "")} & refs):
            records[item_id] = source
    return list(records.values())


def goal_update(wo, *, objective=None, sources=(), success_criteria=None):
    constraints = dict(getattr(wo, "constraints", {}) or {})
    goal = wo.nodes[wo.goal_node_id]
    prior = list(constraints.get("source_intake") or [])
    original = constraints.get("objective") or goal.content or goal.title
    if objective is not None and not constraints.get("objective") and goal.content:
        # Preserve pre-structured goal details when an explicit change occurs.
        prior.append({"item_id": "", "summary": goal.content, "pod_id": ""})
    merged = {s.get("item_id") or s["summary"]: s for s in [*prior, *sources]}
    constraints["source_intake"] = list(merged.values())
    constraints["objective"] = original if objective is None else objective
    if success_criteria is not None:
        constraints["success_criteria"] = success_criteria
    content = render_view("goal_content", objective=constraints["objective"],
                          sources=constraints["source_intake"],
                          success_criteria=constraints.get("success_criteria", ""))
    return {"work_id": wo.id, "expected_updated_at": wo.updated_at,
            "objective": constraints["objective"], "content": content, "constraints": constraints}


def reconcile_transferred_intake(store, items):
    """Retry source acknowledgment when a previous tick saved a goal then stopped."""
    if not items:
        return []
    pending = {str(get_meta(item).get("item_id") or item.get("id") or ""): item for item in items}
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
    for summary in store.list_work_objects():
        wo = store.load(summary["id"])
        for source in wo.constraints.get("source_intake") or []:
            item_id = str(source.get("item_id") or "")
            if item_id in pending:
                write_dayflow_item(item_id, state="closed", reason=f"converted_to_work_object:{wo.id}", caller="steward_handoff_recovery",
                                   updates={"evaluator_pending": False, "evaluator_review": {"outcome": "transferred", "work_id": wo.id}})
                pending.pop(item_id)
        if not pending:
            break
    return list(pending.values())
