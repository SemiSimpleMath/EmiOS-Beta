"""
work_objects.runtime — the bridge between the live tool runtime and a WorkObject.

The graph tools are registered as normal BaseTools, so the tool_caller invokes
them like any tool: `execute(ToolMessage) -> ToolResult`. But a graph tool needs
to know WHICH node it acts on and WHICH store to write — context the ToolMessage
doesn't carry. The WorkManager sets that here right before it runs an agent on a
node; the tool bodies read it. A contextvar (not a plain global) so parallel
managers later get their own binding per thread/task.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from work_objects.store import WorkStore

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("work_context", default=None)


@dataclass
class WorkContext:
    store: "WorkStore"
    work_id: str
    node_id: str            # the node the running agent owns
    actor: str


def set_work_context(store, work_id: str, node_id: str, actor: str) -> contextvars.Token:
    return _CURRENT.set(WorkContext(store=store, work_id=work_id, node_id=node_id, actor=actor))


def get_work_context() -> WorkContext:
    ctx = _CURRENT.get()
    if ctx is None:
        raise RuntimeError("no active WorkContext — set_work_context() must run before a graph tool fires")
    return ctx


def reset_work_context(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def active_attribution_node(store, work_id: str, owned_node_id: str) -> str:
    """The node a planner's work attributes to: the single in-progress (status 'dispatched') checklist
    subtask under the node it owns, else the owned node itself. So a planner's tool results (evidence)
    and the managers it delegates (child nodes) nest UNDER the checklist item it is currently working —
    goal -> checklist item -> this delegation — not the parent it owns. Read identically by the
    WorkPlanner reconcile and the node-handoff, so every work_* manager behaves the same. Ambiguous
    (0 or >1 in-flight children) -> the owned node."""
    try:
        wo = store.load(work_id)
    except Exception:
        return owned_node_id
    inflight = [n.id for n in wo.nodes.values()
                if n.parent_id == owned_node_id and n.type == "subtask" and n.status == "dispatched"]
    return inflight[0] if len(inflight) == 1 else owned_node_id
