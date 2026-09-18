"""The ONE place a tool result becomes graph state.

Everything the dayflow switchboard dispatches is a work node, and every dispatch is a tool call —
a manager is a tool, a ticket to the user is a tool. Each returns a ``ToolResult``, and this module
is what happens next: the result is attached to the node as evidence and the node leaves
``dispatched``. Nothing else writes a node's outcome, and no dispatch path interprets one.

WHY IT IS ITS OWN MODULE. Before 2026-09-16 each lane did its own version. ``discharge_node``
inlined the write for manager results — and skipped it entirely when the worker had already set its
own status, so the caller recorded nothing, not even the result. The ticket lane recorded nothing at
all: a reply was reconstructed later by a scan in the materializer, seven stops into the tick and
behind the steward, which is why a user's decline never reached the graph and a concern they had
explicitly closed was minted into fresh work the next morning, four days running.

WHAT THIS DOES NOT DO. It does not judge. `done` here means exactly what the status glossary says —
a result exists and the work_finalizer has not ruled on it. The only distinction made is the one the
ToolResult itself declares: a tool that reports an error or an aborted run leaves the node `failed`
rather than `done`. Both land with the finalizer — since work_repair retired (2026-09-16) it judges
failed nodes too, and routes them. Meaning is read from the result TEXT by the finalizer, which is
why outcome nuance belongs in that text and never in a new status.
"""
from __future__ import annotations

from typing import Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Statuses a node may be in when a result arrives for it. A node outside this set has already
# ended some other way (cascade, repair, an earlier result) and its outcome is not overwritten.
_OPEN = {"dispatched", "proposed", "actionable", "waiting"}
# proposed/actionable cannot reach `done` directly (see store.TRANSITIONS); they hop through
# `dispatched` first — which is truthful, the call was in fact made.
_NEEDS_DISPATCH_HOP = {"proposed", "actionable"}


def _answer_text(result) -> str:
    """The agent-facing answer a tool returned. Prefers the structured final-answer fields; a
    manager's raw `content` can be agent JSON, so it is the last resort."""
    if result is None:
        return ""
    data = getattr(result, "data", None) or {}
    for key in ("final_answer_answer", "final_answer", "answer"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return (getattr(result, "content", "") or "").strip()


def _is_failure(result) -> bool:
    """Did the TOOL itself report that it could not run? This is the tool's own contract
    (result_type/aborted/exit_state), never an interpretation of what the result means."""
    if result is None:
        return False
    data = getattr(result, "data", None) or {}
    if str(getattr(result, "result_type", "") or "").lower() == "error":
        return True
    return bool(data.get("aborted")) or data.get("exit_state") == "error_exit"


def record_tool_result(store, work_id: str, node_id: str, result, *, actor: str,
                       expected_epoch: Optional[int] = None,
                       evidence_title: Optional[str] = None) -> bool:
    """Attach a dispatched node's tool result and take the node out of flight.

    Returns True when the node was updated. False means the node had already ended — the result is
    not lost (it is still the caller's return value), but an outcome already on the graph is never
    overwritten.

    ``expected_epoch`` fences a zombie: if the node was re-dispatched to a successor incarnation
    while this call was in flight (the sweeper fails a stuck node, the architect re-plans it), the
    stale incarnation's result is refused rather than written over the live one.
    """
    from work_objects.model import new_id

    node = store.load(work_id).nodes.get(node_id)
    if node is None:
        logger.warning("[result_recorder] %s::%s is gone — result not recorded", work_id, node_id)
        return False
    if node.status not in _OPEN:
        logger.info("[result_recorder] %s::%s is %r — its outcome is already recorded",
                    work_id, node_id, node.status)
        return False

    if node.status in _NEEDS_DISPATCH_HOP:
        store.apply("set_status", {"work_id": work_id, "node_id": node_id,
                                   "status": "dispatched"}, actor=actor)

    # A surfaced research pod IS the node's outcome — attach it before the status write so the
    # node is never briefly complete-without-its-deliverable.
    data = getattr(result, "data", None) or {}
    pod_id = next((str(r.get("pod_id")) for r in (data.get("pod_references") or [])
                   if isinstance(r, dict) and r.get("pod_id")), None)
    if pod_id and not node.pod_ref:
        store.apply("attach_pod", {"work_id": work_id, "node_id": node_id, "pod_ref": pod_id},
                    actor=actor)

    # A tool that returned NOTHING is closer to "it did not run" than to "here is the outcome",
    # so it fails rather than quietly completing. And it fails WITH a stated reason: a blocked goal
    # whose WHY/RESULT renders blank is the worst of both — the finalizer and the steward see that a
    # node failed and nothing about why.
    answer = _answer_text(result)
    failed = _is_failure(result) or not answer
    if not answer:
        answer = ("The tool returned no result — nothing was recorded about what it did, "
                  "produced, or why it stopped.")

    status_write = {"work_id": work_id, "node_id": node_id,
                    "status": "failed" if failed else "done"}
    if expected_epoch is not None:
        status_write["expected_dispatch_epoch"] = int(expected_epoch)
    try:
        store.apply("set_status", status_write, actor=actor)
    except ValueError as e:
        # Stale incarnation: repair re-dispatched this node while we worked. Refusing is the
        # point — writing would clobber the successor's outcome.
        logger.error("[result_recorder] %s::%s result DISCARDED — %s", work_id, node_id, e)
        return False

    store.apply("add_node", {
        "work_id": work_id, "id": new_id("result"), "type": "evidence",
        "parent_id": node_id, "status": "assumed", "created_by": actor,
        "title": evidence_title or ("tool failure (why)" if failed else "tool result"),
        "content": answer,
    }, actor=actor)
    logger.info("[result_recorder] %s::%s -> %s", work_id, node_id,
                "failed" if failed else "done")
    return True
