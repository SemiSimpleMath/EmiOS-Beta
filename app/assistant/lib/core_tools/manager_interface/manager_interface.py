# File: assistant/lib/core_tools/manager_interface.py

import uuid

from app.assistant.utils.pydantic_classes import ToolMessage, Message, ToolResult
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

# Scope construction seam — sub-manager scope flows through this factory
# instead of the parent's scope being stamped verbatim. See
# docs/architecture/SCOPE_AUDIT.md sections 3 + 7 for the migration plan.
# Today this is a passthrough; Step 5 will land the authority-cap semantics
# here in ONE place rather than the prior 40+ scattered sites.
_scope_factory = ScopeAdapter()


"""
When a manager calls another manager it does it as a tool call in a sync manner.

When a manager is called via async manner with event hub, it will return the result via event hub.
"""

class ManagerInterface:
    """
    Generic interface for executing manager requests, handling both direct calls and event hub requests.

    Tool visibility narrowing is intentionally NOT performed here. It belongs inside
    the manager's own tool_scope_service.initialize_scope(), which has full access to
    the manager config (hidden_tools, always_show, use_narrower) and runs after the
    manager is instantiated with the correct allowed tool set.
    """

    def __init__(self, manager_name: str):
        self.manager_name = manager_name

    def _should_trace_emi_ingress(self) -> bool:
        return str(self.manager_name or "").strip() == "emi_team_manager"

    def _run_on_child_node(self, task, information, tool_message):
        """Node-handoff: a node-aware manager called inside a WorkObject context runs ON a fresh child
        node minted under the caller's current node — it gets the whole graph as context and writes its
        findings back — instead of a one-shot sub-manager call. Returns a ToolResult, or None to fall
        through to the ordinary ephemeral path (no active work context, or work_objects not present).

        work_objects imports are lazy + guarded: app must NOT hard-depend on work_objects (the dependency
        is one-way, work_objects -> app). Outside a work run this is dead weight that returns None."""
        try:
            from work_objects.runtime import get_work_context
            from work_objects.work_runtime import run_node
            from work_objects.model import new_id
        except Exception:
            return None
        try:
            ctx = get_work_context()
        except Exception:
            return None  # not inside a WorkObject run -> ordinary sub-manager call

        task_text = str(task or tool_message.content or information or "").strip()
        if not task_text:
            return None
        child_id = new_id("node")
        ctx.store.apply("add_node", {
            "work_id": ctx.work_id, "id": child_id, "type": "subtask", "parent_id": ctx.node_id,
            "title": task_text[:80], "content": task_text, "owner_agent": self.manager_name,
            "satisfied_when_kind": "tool_success",
        }, actor=ctx.actor)
        # run_node sets the work context to the child, invokes the manager, closes the node, and
        # restores the caller's context — so the caller's loop resumes on its own node afterward.
        status = run_node(ctx.store, ctx.work_id, child_id, manager_name=self.manager_name)

        wo = ctx.store.load(ctx.work_id)
        child = wo.nodes.get(child_id)
        result_text = ""
        if child and child.pod_ref:
            try:
                from app.assistant.pod_store.pod_store import PodStore
                result_text = (getattr(PodStore().get(child.pod_ref), "body", "") or "").strip()
            except Exception as e:
                logger.debug("node-handoff pod fetch failed: %s", e)
        if not result_text and child:
            findings = [(n.content or "").strip() for n in wo.nodes.values()
                        if n.parent_id == child_id and n.type == "evidence" and (n.content or "").strip()]
            result_text = (child.content or "").strip() or " | ".join(findings)
        logger.info("[node-handoff] %s ran on child node %s -> %s", self.manager_name, child_id, status)
        return ToolResult(
            result_type=f"node::{self.manager_name}",
            content=f"[{self.manager_name}] node {child_id} -> {status}.\n"
                    f"{(result_text or '(no result recorded)')[:1800]}",
            data={"node_id": child_id, "status": status, "pod_ref": child.pod_ref if child else None},
        )

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data or {}
        args = tool_data.get('arguments', {}) if isinstance(tool_data.get('arguments'), dict) else {}
        task = args.get('task')
        information = args.get('information')
        data = tool_data.get('data') if isinstance(tool_data.get('data'), dict) else {}

        # Node-handoff seam: a node-aware manager called inside an active WorkObject context runs ON a
        # fresh child node (it edits the graph), not as an ephemeral sub-manager. Generic — keyed on the
        # manager's `node_aware` config (no manager names here), inert outside a work context.
        if (DI.manager_registry.get(self.manager_name) or {}).get("node_aware"):
            handoff = self._run_on_child_node(task, information, tool_message)
            if handoff is not None:
                return handoff

        task_file = args.get('task_file')
        if isinstance(task_file, str) and task_file.strip():
            data = dict(data)
            data["task_file"] = task_file.strip()
        request_id = tool_message.request_id
        parent_scope = getattr(tool_message, "scope_context", None)
        # Route through the scope factory seam (SCOPE_AUDIT.md Step 1).
        # Today this returns parent_scope verbatim; Step 5 will replace this
        # passthrough with the authority-cap + local-tools construction in
        # exactly one place. Downstream manager_invoker.apply() still runs
        # _apply_manager_narrowing on the receiving side — that's where the
        # May 5 fix lives and where Step 5's logic will eventually move.
        inherited_scope = _scope_factory.for_sub_manager(
            parent_scope=parent_scope,
            child_manager_name=self.manager_name,
        )
        if self._should_trace_emi_ingress():
            logger.info(
                "[emi_team ingress] execute start request_id=%s has_task_file=%s has_inherited_scope=%s data_keys=%s",
                request_id,
                bool(isinstance(task_file, str) and task_file.strip()),
                bool(inherited_scope),
                sorted(list(data.keys())) if isinstance(data, dict) else [],
            )

        try:
            invocation_name = f"{self.manager_name}_{uuid.uuid4().hex[:8]}"
            self.manager = DI.multi_agent_manager_factory.create_manager(
                self.manager_name, name=invocation_name
            )
        except Exception as e:
            logger.error("Failed to create manager instance for %s: %s", self.manager_name, e)
            logger.debug("manager creation exception details", exc_info=True)
            raise RuntimeError(f"Failed to create a manager for {self.manager_name}: {e}")

        manager_content = None
        if task:
            manager_content = task
        elif tool_message.content:
            manager_content = tool_message.content
        elif tool_message.tool_data.get('arguments', {}).get('question'):
            manager_content = tool_message.tool_data.get('arguments', {}).get('question')
        elif information:
            manager_content = information
        else:
            manager_content = f"Process request for {self.manager_name}"

        manager_message = Message(
            event_topic="task_request",
            sender=self.manager_name,
            receiver=None,
            content=manager_content,
            task=task,
            information=information,
            request_id=None,
            data=data,
            scope_context=inherited_scope,
        )
        logger.info(f"{self.manager_name.capitalize()}: Processing content '{manager_content[:50]}...' with ID {request_id}")

        try:
            logger.debug(
                "[%s] Dispatching manager message request_id=%s data_keys=%s",
                self.manager_name,
                request_id,
                sorted(list(data.keys())) if isinstance(data, dict) else [],
            )

            result = DI.manager_invoker.invoke(self.manager, manager_message)

            logger.info(f"{self.manager_name.capitalize()}: Received result.")

            # Always return synchronously. The caller (tool_caller) expects
            # a ToolResult back. The old async event_hub path (request_id check)
            # was firing on every call because request_id leaks from the parent
            # context, causing the synchronous caller to receive None and crash.
            return result

        except Exception as e:
            logger.error(f"{self.manager_name.capitalize()} execution failed: %s", e)
            logger.debug("%s manager execution exception details", self.manager_name, exc_info=True)
            error_result = make_tool_error(
                error_code="manager_interface_execute_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"manager_name": self.manager_name},
            )
            return error_result
