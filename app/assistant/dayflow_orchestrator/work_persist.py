"""dayflow_orchestrator.work_persist — apply the steward's output to the dayflow WorkObject store.

The strategic_planner_wo steward emits goals + advance/complete/abandon directives (no tasks). This
module mints/updates/closes the corresponding work objects. `advance_work_ids` is NOT handled here —
the execution phase consumes it (Phase 2: bounded work_on). The worker decomposes each new goal's graph
when it is first advanced (cold-start), so creation just mints the goal node.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def persist_steward_output(store, output: Dict[str, Any]) -> Dict[str, Any]:
    """Mint new work objects from `new_or_changed`, update changed objectives, and close
    complete/abandon ids. Returns a summary {created, changed, completed, abandoned}."""
    created: List[Dict[str, str]] = []
    changed: List[str] = []
    for spec in output.get("new_or_changed", []) or []:
        if not isinstance(spec, dict):
            continue
        work_id = str(spec.get("work_id") or "").strip()
        objective = str(spec.get("objective") or "").strip()
        rationale = str(spec.get("rationale") or "").strip()
        if not objective:
            continue
        if not work_id:
            # CREATE a new work object — just the goal; the worker decomposes it when advanced.
            # Concern provenance rides constraints so closure can back-propagate the
            # outcome to the register (concern_feedback.propagate_work_outcome).
            concern_refs = [str(b).strip() for b in (spec.get("based_on") or [])
                            if str(b).strip().startswith("concern:")]
            wo = store.apply("create_work_object", {
                "title": objective[:80],
                "goal_content": objective,
                "satisfied_when_kind": "all_owned_children_done",
                "constraints": {"concern_refs": concern_refs} if concern_refs else {},
            }, actor="steward")
            store.apply("set_status", {
                "work_id": wo.id, "node_id": wo.goal_node_id, "status": "dispatched",
            }, actor="steward")
            created.append({"objective": objective, "work_id": wo.id, "rationale": rationale,
                            "based_on": list(spec.get("based_on") or [])})
        else:
            # CHANGE — update the goal's objective text in place.
            try:
                wo = store.load(work_id)
                store.apply("set_status", {
                    "work_id": work_id, "node_id": wo.goal_node_id,
                    "status": wo.nodes[wo.goal_node_id].status, "content": objective,
                }, actor="steward")
                changed.append(work_id)
            except Exception as e:
                logger.warning("persist: could not change work object %s: %s", work_id, e)

    from app.assistant.subconscious.concern_feedback import propagate_work_outcome

    completed: List[str] = []
    for work_id in output.get("complete_work_ids", []) or []:
        work_id = str(work_id).strip()
        try:
            store.apply("set_work_status", {"work_id": work_id, "status": "done",
                                            "reason": "steward: objective judged complete against current context"},
                        actor="steward")
            completed.append(work_id)
            propagate_work_outcome(store, work_id, "done")
        except Exception as e:
            logger.warning("persist: could not complete work object %s: %s", work_id, e)

    abandoned: List[str] = []
    for work_id in output.get("abandon_work_ids", []) or []:
        work_id = str(work_id).strip()
        try:
            store.apply("set_work_status", {"work_id": work_id, "status": "abandoned",
                                            "reason": "steward: objective dropped (superseded, declined, or no longer relevant)"},
                        actor="steward")
            abandoned.append(work_id)
            propagate_work_outcome(store, work_id, "abandoned")
        except Exception as e:
            logger.warning("persist: could not abandon work object %s: %s", work_id, e)

    return {"created": created, "changed": changed, "completed": completed, "abandoned": abandoned}
