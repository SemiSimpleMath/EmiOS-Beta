"""dayflow_orchestrator.work_persist — apply the steward's output to the dayflow WorkObject store.

The strategic_planner_wo evaluator emits goals + complete/abandon directives (no tasks). This module
mints/updates/closes the corresponding work objects; creation just mints the GOAL node.

Decomposition is not this module's job and not the worker's: `work_architect_node` runs immediately
after the persist node in the same tick and lays the goal's DAG. (`advance_work_ids` and the
"decomposed by the worker when first advanced" cold-start both went with the pre-architect design —
there is no advance directive any more, and dispatch runs every ready node.)

The steward does not end work objects: the architect owns each from creation to end
(docs/design/dayflow_goal_ownership_2026-09-30.md). An `end_requests` entry is recorded on the work
object (`instruct_goal`) for the architect to act on.

`based_on` entries prefixed `concern:` are lifted into `constraints.concern_refs` at creation: the
work is attached to those concerns, which then hear of each judgment and of its ending
(concern_feedback.propagate_work_outcome, called here after creation, revision and closure). Work made from an intake item the brain handed over carries
that item's concern whatever the steward cites (work_intake.concern_refs_of).
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def persist_steward_output(store, output: Dict[str, Any], *, admitted_artifacts=()) -> Dict[str, Any]:
    """Mint new work objects from `new_or_changed`, update changed objectives, and record each end
    request for the architect. Returns a summary {created, changed, changed_records, end_requested}."""
    from app.assistant.dayflow_orchestrator.work_intake import concern_refs_of, source_records, goal_update
    from app.assistant.dayflow_orchestrator.work_context import render_view
    created: List[Dict[str, str]] = []
    changed_records: List[Dict[str, Any]] = []
    changed: List[str] = []
    for spec in output.get("new_or_changed", []) or []:
        if not isinstance(spec, dict):
            continue
        work_id = str(spec.get("work_id") or "").strip()
        objective = str(spec.get("objective") or "").strip()
        rationale = str(spec.get("rationale") or "").strip()
        if not objective:
            continue
        sources = source_records(admitted_artifacts, spec.get("based_on") or [])
        if not work_id:
            # CREATE just the goal; the architect decomposes it in the planning pipeline.
            # Concern provenance rides constraints so closure can back-propagate the
            # outcome to the register (concern_feedback.propagate_work_outcome).
            concern_refs = list(dict.fromkeys(
                [str(b).strip() for b in (spec.get("based_on") or []) if str(b).strip().startswith("concern:")]
                + concern_refs_of(sources)))
            # Belief provenance rides constraints for the same reason: a belief that caused
            # this work wants the outcome back. Stored as the belief_key, not the row id —
            # a merge deprecates the losing id but the surviving key still resolves.
            belief_refs = [str(b).strip()[len("belief:"):].strip()
                           for b in (spec.get("based_on") or [])
                           if str(b).strip().startswith("belief:")
                           and str(b).strip()[len("belief:"):].strip()]
            wo = store.apply("create_work_object", {
                "title": objective[:80],
                "goal_content": render_view("goal_content", objective=objective, sources=sources,
                                            success_criteria=str(spec.get("success_criteria") or "").strip()),
                "satisfied_when_kind": "all_owned_children_done",
                "constraints": {"concern_refs": concern_refs, "belief_refs": belief_refs,
                                "objective": objective,
                                "source_intake": sources, "rationale": rationale,
                                "success_criteria": str(spec.get("success_criteria") or "").strip()},
            }, actor="steward")
            store.apply("set_status", {
                "work_id": wo.id, "node_id": wo.goal_node_id, "status": "dispatched",
            }, actor="steward")
            from app.assistant.subconscious.concern_feedback import propagate_work_outcome
            propagate_work_outcome(store, wo.id, "attached")
            created.append({"objective": objective, "work_id": wo.id, "rationale": rationale,
                            "based_on": list(spec.get("based_on") or [])})
        else:
            wo = store.load(work_id)
            update = goal_update(wo, objective=objective, sources=sources,
                                 success_criteria=spec.get("success_criteria"))
            store.apply("revise_goal", update, actor="steward")
            from app.assistant.subconscious.concern_feedback import propagate_work_outcome
            propagate_work_outcome(store, work_id, "attached")
            changed.append(work_id)
            changed_records.append({"work_id": work_id, "based_on": list(spec.get("based_on") or [])})

    end_requested: List[str] = []
    for request in output.get("end_requests", []) or []:
        wid = str((request or {}).get("work_id") or "").strip()
        reason = str((request or {}).get("reason") or "").strip()
        if not wid or not reason:
            raise ValueError(f"persist: end request needs a work_id and a reason: {request!r}")
        store.apply("instruct_goal", {"work_id": wid, "kind": "end", "reason": reason}, actor="steward")
        end_requested.append(wid)

    return {"created": created, "changed": changed, "changed_records": changed_records,
            "end_requested": end_requested}
