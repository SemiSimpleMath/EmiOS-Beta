"""dayflow_orchestrator.work_repair_apply — apply the work_repair adjudicator's disposition to a stuck
work object's failed node(s). Surgical + in-place: re-open the failed top-level subtask. No new nodes and
no re-wiring — dependents stay pointed at the same node and unblock when it finally completes, and subtasks
satisfy by their own `done` status, so leftover failed descendants don't block.

  escalate     -> re-issue the failed step as a user_reply ASK (the dispatch asks the user; the reply is
                  recorded as the node's result). status failed->proposed + wake_kind=user_reply
                  + wake_ref=the ask.
  retry        -> re-run it as-is — transient failure, or the needed info is now present (e.g. a reply
                  already on the node). status failed->proposed.
  abandon_goal -> the goal is unachievable or no longer wanted; abandon the whole WO (set_work_status,
                  which propagates: the dispatch skips terminal WOs, so the worker stops).
"""
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

DISPOSITIONS = {"escalate", "retry", "abandon_goal"}

# ASK CEILING (owner ruling 2026-08-19): an ask that has timed out unanswered
# this many times stops being re-surfaced — the user's silence IS the answer,
# and the evaluator judges what it means at goal level. Without this, the
# timeout -> repair -> re-ask loop ran hourly forever (ten work objects deep
# on 2026-08-19).
_MAX_ASK_TIMEOUTS = 3


def _ask_timeout_count(node) -> int:
    """How many times this node's ask has expired unanswered — counted from the
    sweeper's append-only status notes (deterministic; no wording judgment)."""
    notes = (node.payload or {}).get("status_notes") or []
    return sum(1 for x in notes if "ask timed out" in str(x.get("note", "")).lower())


def find_failed_targets(wo):
    """The logical failures to act on: failed subtasks directly under the goal; else any failed node."""
    gid = wo.goal_node_id
    top = [n for n in wo.nodes.values() if n.parent_id == gid and n.status == "failed"]
    return top or [n for n in wo.nodes.values() if n.status == "failed"]


def apply_adjudication(store, work_id, disposition, ask="", actor="work_repair", reason=""):
    """Apply ONE disposition to work_id's failed node(s). Returns {work_id, disposition, targets}."""
    disposition = (disposition or "").strip().lower()
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r}")

    if disposition == "abandon_goal":
        from app.assistant.subconscious.concern_feedback import propagate_work_outcome
        store.apply("set_work_status", {"work_id": work_id, "status": "abandoned",
                                        "reason": f"work_repair abandon_goal: {reason or 'unachievable or no longer wanted'}"},
                    actor=actor)
        propagate_work_outcome(store, work_id, "abandoned")
        logger.info("work_repair[%s]: abandon_goal", work_id)
        return {"work_id": work_id, "disposition": disposition, "targets": []}

    wo = store.load(work_id)
    targets = find_failed_targets(wo)
    done = []
    for n in targets:
        # Ceiling before any re-surface: a repeatedly-unanswered ask is not
        # re-asked again. The node ends abandoned with its epitaph; the
        # finalizer/evaluator read the silence at goal level.
        timeouts = _ask_timeout_count(n)
        if disposition in ("retry", "escalate") and timeouts >= _MAX_ASK_TIMEOUTS:
            store.apply("set_status", {
                "work_id": work_id, "node_id": n.id, "status": "abandoned",
                "verdict": "ask_unanswered_ceiling",
                "reason": (f"asked the user {timeouts} times with no answer — "
                           f"re-asking again is noise; the user's silence is the result"),
            }, actor=actor)
            logger.warning("work_repair[%s]: ask %s hit the %d-timeout ceiling — abandoned, not re-asked",
                           work_id, n.id, timeouts)
            done.append(n.id)
            continue
        if disposition == "retry":
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "proposed"}, actor=actor)
        else:  # escalate
            # The ask text rides wake_ref ONLY. It must never be written into
            # node.content — that is the node's immutable directive, and
            # appending repair-authored prose there is how internal confusion
            # ("please provide a way to deliver this") reached the user's
            # screen verbatim on 2026-08-19.
            store.apply("set_status", {"work_id": work_id, "node_id": n.id, "status": "proposed"},
                        actor=actor)
            store.apply("defer_node", {"work_id": work_id, "node_id": n.id, "wake_kind": "user_reply",
                                       "wake_at": None, "wake_ref": ask or n.title}, actor=actor)
        done.append(n.id)
    logger.info("work_repair[%s]: %s -> %s", work_id, disposition, done)
    return {"work_id": work_id, "disposition": disposition, "targets": done}
