"""dayflow_orchestrator.work_repair_apply — apply the work_repair adjudicator's disposition to a stuck
work object's failed node(s). Surgical + in-place: re-open the failed top-level subtask. No new nodes and
no re-wiring — dependents stay pointed at the same node and unblock when it finally completes, and subtasks
satisfy by their own `done` status, so leftover failed descendants don't block.

  escalate     -> re-issue the failed step as a user_reply ASK (the notify path asks the user; the worker
                  then completes it from the reply via the worker-uses-reply fix). status failed->proposed
                  + wake_kind=user_reply + wake_ref=the ask.
  retry        -> re-run it as-is — transient failure, or the needed info is now present (e.g. a reply
                  already on the node). status failed->proposed.
  abandon_goal -> the goal is unachievable or no longer wanted; abandon the whole WO (set_work_status,
                  which propagates: work_execution skips terminal WOs, so the worker stops).
"""
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

DISPOSITIONS = {"escalate", "retry", "abandon_goal"}


def find_failed_targets(wo):
    """The logical failures to act on: failed subtasks directly under the goal; else any failed node."""
    gid = wo.goal_node_id
    top = [n for n in wo.nodes.values() if n.parent_id == gid and n.status == "failed"]
    return top or [n for n in wo.nodes.values() if n.status == "failed"]


def apply_adjudication(store, work_id, disposition, ask="", actor="work_repair"):
    """Apply ONE disposition to work_id's failed node(s). Returns {work_id, disposition, targets}."""
    disposition = (disposition or "").strip().lower()
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r}")

    if disposition == "abandon_goal":
        store.apply("set_work_status", {"work_id": work_id, "status": "abandoned"}, actor=actor)
        logger.info("work_repair[%s]: abandon_goal", work_id)
        return {"work_id": work_id, "disposition": disposition, "targets": []}

    wo = store.load(work_id)
    targets = find_failed_targets(wo)
    done = []
    for n in targets:
        if disposition == "retry":
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "proposed"}, actor=actor)
        else:  # escalate
            content = (n.content or "").rstrip()
            note = f"{content}\n\n[Re-issued as an ask to the user: {ask}]" if ask else content
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "proposed",
                                       "content": note}, actor=actor)
            store.apply("defer_node", {"work_id": work_id, "node_id": n.id, "wake_kind": "user_reply",
                                       "wake_at": None, "wake_ref": ask or n.title}, actor=actor)
        done.append(n.id)
    logger.info("work_repair[%s]: %s -> %s", work_id, disposition, done)
    return {"work_id": work_id, "disposition": disposition, "targets": done}
