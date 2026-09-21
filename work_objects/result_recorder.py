"""The ONE place a tool result becomes graph state.

Everything the dayflow switchboard dispatches is a work node, and every dispatch is a tool call —
a manager is a tool, a ticket to the user is a tool. Each returns a ``ToolResult``, and this module
is what happens next: the result is attached to the node as evidence and the node leaves
``dispatched``. The finalizer judges the outcome; store, UI and failure paths can also
change status. Recording commits the attempt fence, evidence, saved-output reference and status atomically.

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
                       evidence_title: Optional[str] = None, idle_before=None) -> bool:
    """Commit status, pod and evidence atomically; stale/duplicate results change nothing."""
    from work_objects.model import new_id
    from work_objects.store import StaleResult
    data = getattr(result, "data", None) or {}
    pod_id = next((str(r.get("pod_id")) for r in (data.get("pod_references") or [])
                   if isinstance(r, dict) and r.get("pod_id")), None)
    # Only the ticket tool / its recovery path may attribute a tool result to the user.
    # Never infer this from the result's prose or from an arbitrary manager's fields.
    user_reply = None
    if (actor in {"create_dayflow_ticket", "ask"}
            and getattr(result, "result_type", "") == "ticket_response"
            and data.get("ticket_id") and data.get("action") not in {"timeout", "created", "pending"}
            and (data.get("user_text") or data.get("response_details"))):
        from copy import deepcopy
        user_reply = deepcopy({"ticket_id": data["ticket_id"],
            "question": data.get("question") or data.get("title") or "",
            "action": data.get("action") or "", "user_text": data.get("user_text") or "",
            "responded_at": data.get("responded_at") or "",
            "response_details": data.get("response_details") or {}})
    answer = _answer_text(result)
    failed = _is_failure(result) or not answer
    if not answer:
        answer = "The tool returned no result; its outcome is unknown."
    try:
        store.apply("record_result", {
            "work_id": work_id, "node_id": node_id,
            "expected_dispatch_epoch": expected_epoch, "idle_before": idle_before,
            "evidence_id": new_id("result"), "answer": answer, "user_reply": user_reply,
            "status": "failed" if failed else "done", "pod_ref": pod_id,
            "abort_policy": data.get("abort_policy"), "error_code": data.get("error_code"),
            "title": evidence_title or ("tool failure (why)" if failed else "tool result"),
        }, actor=actor)
    except StaleResult as exc:
        logger.info("[result_recorder] %s::%s rejected: %s", work_id, node_id, exc)
        return False
    return True
