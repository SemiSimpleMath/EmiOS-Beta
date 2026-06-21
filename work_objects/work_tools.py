"""
work_objects.work_tools — the WorkGraph ops as real, registered BaseTools.

These wrap the same logic as work_objects.tools.WorkGraphTools, but as live
tools the standard tool_caller dispatches. Each reads the active node from
runtime.get_work_context() (set by the WorkManager) and returns a ToolResult.
register_work_tools(DI.tool_registry) injects them at RUNTIME — nothing is added
under app/, so work_objects stays isolated while fully reusing the runtime.

This module imports app.*, so any caller must bootstrap DI first
(`import app.assistant.tests.test_setup`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from pydantic import BaseModel, create_model

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

from work_objects.runtime import get_work_context
from work_objects.tools import WorkGraphTools


def pod_summary(pod_ref: str) -> str:
    """A one-line summary of a pod — its one_liner (headline) + id. Use this ANYWHERE a pod surfaces
    (a handoff return, a graph peek, the render projection) so a viewer always sees WHAT the pod is,
    never a bare `datapod:...` id. Returns '' for no ref; the id alone if the pod has no one_liner."""
    if not pod_ref:
        return ""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        pod = PodStore().get(pod_ref)
        if pod is None:
            return f"{pod_ref} (missing)"
        ol = (getattr(pod, "one_liner", "") or "").strip()
        return f"{ol} [{pod_ref}]" if ol else str(pod_ref)
    except Exception as e:
        logger.debug("pod_summary(%s) failed: %s", pod_ref, e)
        return str(pod_ref)


def _bound() -> WorkGraphTools:
    c = get_work_context()
    return WorkGraphTools(c.store, c.work_id, c.node_id, c.actor)


# ----------------------------- arg schemas ----------------------------- #
class add_subtask_args(BaseModel):
    title: str
    content: str = ""
    satisfied_when_kind: Optional[str] = None
    owner_agent: Optional[str] = None
    depends_on: Optional[list[str]] = None


class add_dependency_args(BaseModel):
    on_node_id: str


class record_finding_args(BaseModel):
    claim: str
    confidence: Optional[float] = None
    pod_ref: Optional[str] = None


class produce_artifact_args(BaseModel):
    title: str
    pod_ref: str


class ask_question_args(BaseModel):
    text: str
    blocks: bool = False


class defer_args(BaseModel):
    wake_kind: str
    wake_at: Optional[str] = None
    wake_ref: Optional[str] = None
    reason: str = ""


class finish_args(BaseModel):
    status: str = "satisfied"          # satisfied | failed | abandoned
    reason: str = ""


class graph_search_args(BaseModel):
    query: str


class graph_peek_args(BaseModel):
    node_id: str


class graph_summary_args(BaseModel):
    subtree_only: bool = False


# ----------------------------- handlers ----------------------------- #
def _add_subtask(t, a):
    nid = t.add_subtask(a["title"], a.get("content", ""), a.get("satisfied_when_kind"),
                        a.get("owner_agent"), a.get("depends_on"))
    # Feed the running decomposition back so the agent (whose projection is frozen
    # at loop entry) sees what it already added — avoids duplicates + knows when done.
    kids = [f"[{s['id']}] {s['title']}" for s in t.graph_summary(subtree_only=True) if s["id"] != t.node_id]
    return ToolResult(
        content=(f"Added subtask '{a['title']}' (id {nid}). Your node now has {len(kids)} subtask(s): "
                 + "; ".join(kids) + ". Add another only if a distinct part remains, else return_control."),
        data={"node_id": nid, "subtask_count": len(kids)},
    )


def _add_dependency(t, a):
    t.add_dependency(a["on_node_id"])
    return ToolResult(content=f"now depends on {a['on_node_id']}", data={})


def _record_finding(t, a):
    nid = t.record_finding(a["claim"], a.get("confidence"), a.get("pod_ref"))
    return ToolResult(content=f"recorded finding {nid}", data={"node_id": nid})


def _produce_artifact(t, a):
    nid = t.produce_artifact(a["title"], a["pod_ref"])
    return ToolResult(
        content=(f"Produced artifact '{a['title']}' (id {nid}). The deliverable is recorded. "
                 "If this node's task is now complete, call work_finish(status=satisfied) — do NOT re-produce it."),
        data={"node_id": nid},
    )


def _ask_question(t, a):
    nid = t.ask_question(a["text"], a.get("blocks", False))
    return ToolResult(content=f"opened question {nid}", data={"node_id": nid})


def _defer(t, a):
    t.defer(a["wake_kind"], a.get("wake_at"), a.get("wake_ref"), a.get("reason", ""))
    return ToolResult(content="deferred", data={})


def _finish(t, a):
    status = a.get("status", "satisfied")
    if status == "satisfied":
        t.mark_satisfied()
    elif status == "failed":
        t.mark_failed(a.get("reason", ""))
    else:
        t.abandon(a.get("reason", ""))
    return ToolResult(content=f"finished: {status}", data={"status": status})


def _graph_search(t, a):
    hits = t.graph_search(a["query"])
    return ToolResult(content=f"{len(hits)} match(es)", data={"results": hits})


def _graph_peek(t, a):
    node = t.graph_peek(a["node_id"])
    # Surface the node's CONTENT in the result TEXT (not just the title) so the agent sees a
    # deliverable's body when it peeks — otherwise it re-peeks blind. If the result lives in a pod,
    # show the pod's SUMMARY (one_liner), never a bare datapod:... id.
    body = (node.get("content") or "").strip()
    if not body and node.get("pod_ref"):
        body = pod_summary(node["pod_ref"])
    summary = node.get("title", "")
    if body:
        summary = f"{summary} -> {body}"
    return ToolResult(content=summary, data=node)


def _graph_summary(t, a):
    rows = t.graph_summary(a.get("subtree_only", False))
    return ToolResult(content=f"{len(rows)} node(s)", data={"nodes": rows})


# ----------------------------- specs ----------------------------- #
@dataclass
class _Spec:
    name: str
    args_model: type
    handler: Callable[[WorkGraphTools, dict], ToolResult]
    description: str


_SPECS = [
    _Spec("work_add_subtask", add_subtask_args, _add_subtask, "Decompose: add a child subtask under my node (optionally depends_on others)."),
    _Spec("work_add_dependency", add_dependency_args, _add_dependency, "Declare my node depends on another node."),
    _Spec("work_record_finding", record_finding_args, _record_finding, "Record a noteworthy discovery as an Evidence node now."),
    _Spec("work_produce_artifact", produce_artifact_args, _produce_artifact, "Produce the deliverable as an Artifact node referencing a pod."),
    _Spec("work_ask_question", ask_question_args, _ask_question, "Open a Question (blocks=true makes my node depend on it)."),
    _Spec("work_defer", defer_args, _defer, "Park my node with a wake condition ('too hard now / wait for X')."),
    _Spec("work_finish", finish_args, _finish, "Close my node: satisfied | failed | abandoned."),
    _Spec("work_graph_search", graph_search_args, _graph_search, "Find nodes by substring of title/content."),
    _Spec("work_graph_peek", graph_peek_args, _graph_peek, "Read one node's full body."),
    _Spec("work_graph_summary", graph_summary_args, _graph_summary, "One-line index of nodes (drill down with peek)."),
]


WORK_TOOL_NAMES = [s.name for s in _SPECS]


def _make_tool_class(spec: _Spec) -> type:
    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        try:
            return spec.handler(_bound(), args)
        except Exception as e:
            # recoverable: surface to the agent (loud), don't kill the manager loop
            logger.error("work tool %s failed: %s", spec.name, e)
            return ToolResult(content=f"ERROR from {spec.name}: {e}. Fix your arguments and try again.",
                              data={"error": str(e)})
    return type(spec.name, (BaseTool,), {
        "__init__": lambda self, _n=spec.name: BaseTool.__init__(self, _n),
        "execute": execute,
    })


def _inputs_from_model(model: type) -> list[dict]:
    out = []
    for fname, field in model.model_fields.items():
        out.append({"name": fname, "type": "string", "required": field.is_required(), "description": ""})
    return out


def register_work_tools(tool_registry) -> list[str]:
    """Inject the WorkGraph tools into the live registry at runtime. Returns names."""
    names = []
    for spec in _SPECS:
        outer = create_model(f"{spec.name}_arguments", tool_name=(str, ...), arguments=(spec.args_model, ...))
        tool_registry.registry[spec.name] = {
            "tool_class": _make_tool_class(spec),
            "tool_args": {"args": spec.args_model, "arguments": outer},
            "prompts": {},
            "tool_contract": {
                "name": spec.name,
                "description": spec.description,
                "inputs": _inputs_from_model(spec.args_model),
                "outputs": [{"path": "data.node_id", "type": "string", "description": "affected node id"}],
                "metadata": {"domain": "work_graph", "actions": [spec.name],
                             "min_authority": 0, "approval_min_authority": 0},
            },
        }
        names.append(spec.name)
    return names


def register_manager_as_tool(tool_registry, manager_name: str, description: str, metadata: dict) -> None:
    """Expose a manager as a callable tool — the SAME manager-as-tool wrapper the static
    `app/.../tools/<name>/` dirs use (a thin BaseTool that defers to ManagerInterface;
    args task+information). Registered at runtime so node managers stay out of app/.
    The node-handoff (run ON a fresh child node) is applied generically inside
    ManagerInterface for node_aware managers — this wrapper is identical to web_manager's."""
    iface = ManagerInterface(manager_name)

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        return iface.execute(tool_message)

    tool_class = type(manager_name, (BaseTool,), {
        "__init__": lambda self, _n=manager_name: BaseTool.__init__(self, _n),
        "execute": execute,
    })
    args_model = create_model(f"{manager_name}_args", task=(str, ...), information=(str, ...))
    outer = create_model(f"{manager_name}_arguments", tool_name=(str, ...), arguments=(args_model, ...))
    tool_registry.registry[manager_name] = {
        "tool_class": tool_class,
        "tool_args": {"args": args_model, "arguments": outer},
        "prompts": {},
        "tool_contract": {
            "name": manager_name,
            "description": description,
            "inputs": [
                {"name": "task", "type": "string", "required": True, "description": ""},
                {"name": "information", "type": "string", "required": True, "description": ""},
            ],
            "outputs": [{"path": "data.node_id", "type": "string",
                         "description": "the child node this manager ran on"}],
            "metadata": metadata,
        },
    }
