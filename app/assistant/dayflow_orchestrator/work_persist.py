"""dayflow_orchestrator.work_persist — apply the steward's output to the dayflow WorkObject store.

The strategic_planner_wo evaluator emits goals + complete/abandon directives (no tasks). This module
mints/updates/closes the corresponding work objects; creation just mints the GOAL node.

Decomposition is not this module's job and not the worker's: `work_architect_node` runs immediately
after the persist node in the same tick and lays the goal's DAG. (`advance_work_ids` and the
"decomposed by the worker when first advanced" cold-start both went with the pre-architect design —
there is no advance directive any more, and dispatch runs every ready node.)

`based_on` entries prefixed `concern:` are lifted into `constraints.concern_refs` at creation so a
terminal outcome can be back-propagated to the subconscious register; complete/abandon call
`propagate_work_outcome` here, directly.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def persist_steward_output(store, output: Dict[str, Any], *, admitted_artifacts=()) -> Dict[str, Any]:
    """Mint new work objects from `new_or_changed`, update changed objectives, and close
    complete/abandon ids. Returns a summary {created, changed, completed, abandoned}."""
    from app.assistant.dayflow_orchestrator.work_intake import source_records, goal_update
    from app.assistant.dayflow_orchestrator.work_context import render_view
    # Closure intent must survive a failed graph write or a lost tick blackboard.
    requests = []
    for key, status, reason in (
        ("complete_work_ids", "done", "steward: objective judged complete against current context"),
        ("abandon_work_ids", "abandoned", "steward: objective dropped (superseded, declined, or no longer relevant)"),
    ):
        for wid in dict.fromkeys(str(w).strip() for w in output.get(key, []) or []):
            requests.append({"work_id": wid, "status": status, "reason": reason})
    closing = {r["work_id"] for r in requests}
    closing_specs = [spec for spec in output.get("new_or_changed", []) or []
                     if isinstance(spec, dict) and str(spec.get("work_id") or "").strip() in closing
                     and str(spec.get("objective") or "").strip()]
    goal_updates = {}
    for spec in closing_specs:
        wid = str(spec["work_id"]).strip()
        if wid in goal_updates:
            raise ValueError("Duplicate objective update for closing work object")
        goal_updates[wid] = goal_update(store.load(wid), objective=str(spec["objective"]).strip(),
            sources=source_records(admitted_artifacts, spec.get("based_on") or []),
            success_criteria=spec.get("success_criteria"))
    store.queue_work_closures(requests, goal_updates=goal_updates)
    closures = recover_pending_work_closures(store)
    completed, abandoned = closures["completed"], closures["abandoned"]
    created: List[Dict[str, str]] = []
    changed_records = [{"work_id": str(spec["work_id"]).strip(), "based_on": list(spec.get("based_on") or [])}
                       for spec in closing_specs]
    changed: List[str] = list(goal_updates)
    for spec in output.get("new_or_changed", []) or []:
        if not isinstance(spec, dict):
            continue
        work_id = str(spec.get("work_id") or "").strip()
        if work_id in goal_updates:
            continue  # Source/objective update committed with its terminal intent.
        objective = str(spec.get("objective") or "").strip()
        rationale = str(spec.get("rationale") or "").strip()
        if not objective:
            continue
        sources = source_records(admitted_artifacts, spec.get("based_on") or [])
        if not work_id:
            # CREATE just the goal; the architect decomposes it in the planning pipeline.
            # Concern provenance rides constraints so closure can back-propagate the
            # outcome to the register (concern_feedback.propagate_work_outcome).
            concern_refs = [str(b).strip() for b in (spec.get("based_on") or [])
                            if str(b).strip().startswith("concern:")]
            wo = store.apply("create_work_object", {
                "title": objective[:80],
                "goal_content": render_view("goal_content", objective=objective, sources=sources,
                                            success_criteria=str(spec.get("success_criteria") or "").strip()),
                "satisfied_when_kind": "all_owned_children_done",
                "constraints": {"concern_refs": concern_refs, "objective": objective,
                                "source_intake": sources, "rationale": rationale,
                                "success_criteria": str(spec.get("success_criteria") or "").strip()},
            }, actor="steward")
            store.apply("set_status", {
                "work_id": wo.id, "node_id": wo.goal_node_id, "status": "dispatched",
            }, actor="steward")
            created.append({"objective": objective, "work_id": wo.id, "rationale": rationale,
                            "based_on": list(spec.get("based_on") or [])})
        else:
            wo = store.load(work_id)
            update = goal_update(wo, objective=objective, sources=sources,
                                 success_criteria=spec.get("success_criteria"))
            store.apply("revise_goal", update, actor="steward")
            changed.append(work_id)
            changed_records.append({"work_id": work_id, "based_on": list(spec.get("based_on") or [])})

    return {"created": created, "changed": changed, "changed_records": changed_records, "completed": completed, "abandoned": abandoned}


def recover_pending_work_closures(store):
    """Retry saved terminal decisions before new evaluation or work creation.

    Closure errors stop the pass. Concern feedback is a separate best-effort
    post-commit side effect; it cannot undo or mislabel a successful closure.
    """
    from app.assistant.subconscious.concern_feedback import propagate_work_outcome
    result = {"completed": [], "abandoned": []}
    for request in store.pending_work_closures():
        wid, status = request["work_id"], request["status"]
        try:
            store.apply("set_work_status", {"work_id": wid, "status": status,
                                           "reason": request["reason"]}, actor="steward")
        except Exception:
            logger.error("persist: closure failed for %s (%s); intent remains pending",
                         wid, status, exc_info=True)
            raise
        result["completed" if status == "done" else "abandoned"].append(wid)
        try:
            propagate_work_outcome(store, wid, status)
        except Exception:
            # The helper normally logs its own failures; preserve this boundary
            # even if an unexpected caller/dependency error escapes it.
            logger.error("persist: work %s committed %s; concern feedback failed",
                         wid, status, exc_info=True)
    return result
