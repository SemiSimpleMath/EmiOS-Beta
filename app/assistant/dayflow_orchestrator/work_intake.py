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
            "thread_id": str(meta.get("email_thread_id") or ""),
            # A concern handed to the steward (concern_handoff, retired 2026-10-10): work made from
            # this item serves that concern.
            "concern_id": str(meta.get("concern_id") or ""),
            # A placement of the day handed to the steward (belief_occurrences, retired 2026-10-10): work
            # made from it serves that belief, and its outcome returns to the belief.
            "belief_key": str(meta.get("belief_key") or "")}


def concern_refs_of(sources):
    """The concern refs work made from these intake sources must carry."""
    return [f"concern:{s['concern_id']}" for s in sources if s.get("concern_id")]


def belief_refs_of(sources):
    """The belief keys work made from these intake sources must carry."""
    return [s["belief_key"] for s in sources if s.get("belief_key")]


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
    constraints["concern_refs"] = list(dict.fromkeys([*(constraints.get("concern_refs") or []),
                                                      *concern_refs_of(sources)]))
    constraints["belief_refs"] = list(dict.fromkeys([*(constraints.get("belief_refs") or []),
                                                     *belief_refs_of(sources)]))
    constraints["objective"] = original if objective is None else objective
    if success_criteria is not None:
        constraints["success_criteria"] = success_criteria
    content = render_view("goal_content", objective=constraints["objective"],
                          sources=constraints["source_intake"],
                          success_criteria=constraints.get("success_criteria", ""))
    return {"work_id": wo.id, "expected_updated_at": wo.updated_at,
            "objective": constraints["objective"], "content": content, "constraints": constraints}

