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

    def _run_on_given_node(self, work_id, node_id, tool_message):
        from app.assistant.manager_runtime.execution import current_owner, Owner, REGISTRY
        from work_objects.runtime import peek_work_context
        if current_owner() is not None or peek_work_context() is not None:
            return self._run_on_given_node_body(work_id, node_id, tool_message)
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
        node = store.load(work_id).nodes[node_id]
        owner = Owner(store, work_id, node_id, int(node.payload.get("dispatch_epoch") or 0))
        store.start_execution(owner)
        try:
            with REGISTRY.span("attempt", self.manager_name, owner=owner, attribution=node_id):
                return self._run_on_given_node_body(work_id, node_id, tool_message)
        finally:
            REGISTRY.finish_owner(owner)

    def _run_on_given_node_body(self, work_id, node_id, tool_message):
        """Run this manager ON the node the caller handed it.

        ONE entry, for callers of both kinds, because the manager's job is the same either way:

          - The dayflow dispatch gives it a TOP-LEVEL node — the architect's unit, claimed by
            the gate, judged by the finalizer, counting toward the goal.
          - A manager already working gives it a SUB-NODE it just created for a piece of its own
            work — scratch, run immediately, answering straight back to its caller.

        Nested records are worker-owned provenance, not orchestrator assignments. The
        orchestrator's ownership filter excludes them from promotion, wakes and dispatch.
        The worker executes its delegation synchronously; its result is kept for takeover
        and finalizer judgment. This shared entry therefore does not require a prior claim
        for a provenance record. Top-level dispatch claims remain a separate concern.

        Returns the manager's ToolResult verbatim. It does NOT record the result — the caller
        records, the same as for every other tool. work_objects imports stay lazy and guarded:
        the dependency is one-way, work_objects -> app.
        """
        from work_objects.runtime import reset_work_context, set_work_context
        from work_objects.runtime_setup import ensure_manager_services
        from work_objects.work_tools import register_work_tools
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store

        ensure_manager_services()
        DI.manager_registry.preload_all()
        register_work_tools(DI.tool_registry)

        store = get_dayflow_work_store()
        node = store.load(work_id).nodes[node_id]

        # The manager's own `node_input` config decides how the node is handed over — no
        # manager-name branching:
        #   "task"   -> the node's content as the task (+ its dependencies' results as information)
        #   "render" -> the manager's render node loads the graph projection; the message still
        #               carries the node's real goal so a degraded projection cannot make the
        #               worker invent a task (the 06-23 contamination).
        config = DI.manager_registry.get(self.manager_name) or {}
        node_input = config.get("node_input", "render")

        token = set_work_context(store, work_id, node_id, actor=self.manager_name)
        try:
            if node_input == "task":
                from work_objects.discharge import _render_dependencies
                return self.invoke_on(
                    task=(node.content or node.title),
                    information=_render_dependencies(store.load(work_id), node_id),
                    scope_context=getattr(tool_message, "scope_context", None),
                )
            goal_txt = (node.content or node.title or "").strip()
            from app.assistant.dayflow_orchestrator.work_context import render_view
            return self.invoke_on(
                task=render_view("discharge_task", directive=goal_txt),
                information="",
                scope_context=getattr(tool_message, "scope_context", None),
            )
        finally:
            reset_work_context(token)

    def _run_on_child_node(self, task, information, tool_message):
        """Node-handoff: a node-aware manager called inside a WorkObject context runs ON a fresh child
        node minted under the caller's current node — it gets the whole graph as context and writes its
        findings back — instead of a one-shot sub-manager call. Returns a ToolResult, or None to fall
        through to the ordinary ephemeral path (no active work context, or work_objects not present).

        work_objects imports are lazy + guarded: app must NOT hard-depend on work_objects (the dependency
        is one-way, work_objects -> app). Outside a work run this is dead weight that returns None."""
        try:
            from work_objects.runtime import get_work_context
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
        # Nest the delegated work UNDER the caller's ACTIVE checklist item (its in-progress subtask), not
        # the node it owns — so the graph reads goal -> checklist item -> this delegation. Same resolver
        # the WorkPlanner reconcile uses, so attribution is consistent; falls back to the owned node when
        # there's no active item (single-step node).
        from work_objects.runtime import active_attribution_node
        parent_id = active_attribution_node(ctx.store, ctx.work_id, ctx.node_id)
        child_id = new_id("node")
        ctx.store.apply("add_node", {
            "work_id": ctx.work_id, "id": child_id, "type": "subtask", "parent_id": parent_id,
            "title": task_text[:80], "content": task_text, "owner_agent": self.manager_name,
            "satisfied_when_kind": "tool_success",
        }, actor=ctx.actor)
        # The child is handed over through the SAME entry the dispatch uses — the manager's job is
        # identical either way, and the only difference is who decided the node exists.
        #
        # It used to go through discharge_node, which refuses any node that is not already
        # `dispatched`. A child is an internal provenance record, excluded from orchestrator
        # scheduling by its ownership relationship. So every delegation raised on the node it had just created, from the day
        # that assertion landed (2026-08-04) — one node-handoff has succeeded since, in June.
        result = self._run_on_given_node(ctx.work_id, child_id, tool_message)

        # The sub-node is the manager's scratch, but its outcome is PROVENANCE: recorded on the
        # child so a later pass reads what was already tried instead of redoing it.
        from work_objects.result_recorder import record_tool_result
        record_tool_result(ctx.store, ctx.work_id, child_id, result,
                           actor=self.manager_name,
                           evidence_title=f"{self.manager_name} result")
        child = ctx.store.load(ctx.work_id).nodes.get(child_id)
        status = child.status if child else "unknown"
        logger.info("[node-handoff] %s ran on child node %s -> %s", self.manager_name, child_id, status)
        if result is not None:
            return result
        # The manager produced no result object — surface the node's resolution so the caller still
        # sees the handoff completed (and does NOT read None as "fall through to the ephemeral path").
        return ToolResult(
            result_type=f"node::{self.manager_name}",
            content=f"[{self.manager_name}] node {child_id} -> {status} (no result produced).",
            data={"node_id": child_id, "status": status},
        )

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """The TOOL entry point: unpack the tool call, then make the manager call."""
        tool_data = tool_message.tool_data or {}
        args = tool_data.get('arguments', {}) if isinstance(tool_data.get('arguments'), dict) else {}
        task = args.get('task')
        information = args.get('information')
        data = tool_data.get('data') if isinstance(tool_data.get('data'), dict) else {}

        # Node seams, keyed on the manager's `node_aware` config (no manager names here) and
        # inert for an ordinary manager:
        #   - the caller named a node -> run ON it. The caller decided that node exists: the
        #     dispatch gate for a top-level unit, a working manager for a sub-node of its own.
        #   - inside an active work context with no node named -> the legacy path, which mints
        #     a child for itself out of the caller's context. Dormant since 2026-06 and due to
        #     go: creating the node is the CALLER's job, and `work_add_subtask` is how.
        # Anything else is an ephemeral sub-manager call, unchanged.
        if (DI.manager_registry.get(self.manager_name) or {}).get("node_aware"):
            work_id = str(args.get("work_id") or "").strip()
            node_id = str(args.get("node_id") or "").strip()
            if work_id and node_id:
                return self._run_on_given_node(work_id, node_id, tool_message)
            handoff = self._run_on_child_node(task, information, tool_message)
            if handoff is not None:
                return handoff

        task_file = args.get('task_file')
        if isinstance(task_file, str) and task_file.strip():
            data = dict(data)
            data["task_file"] = task_file.strip()

        return self.invoke_on(
            task=task,
            information=information,
            scope_context=getattr(tool_message, "scope_context", None),
            data=data,
            content=(tool_message.content or args.get('question')),
            log_request_id=tool_message.request_id,
            has_task_file=bool(isinstance(task_file, str) and task_file.strip()),
        )

    def invoke_on(self, *, task, information, scope_context, data=None, content=None,
                  log_request_id=None, has_task_file: bool = False) -> ToolResult:
        """THE manager call, and nothing else: the sub-manager scope seam, the standard
        ``task_request`` message shape, the invoke, and a structured tool error on failure.
        Returns the manager's ToolResult verbatim.

        It deliberately knows nothing about why it was called or what the result MEANS —
        that belongs to the caller. ``execute`` is this plus the tool-call plumbing
        (argument extraction + the node handoff); the dayflow work dispatcher
        (``work_objects.discharge``) calls in here directly because it already owns the node
        it is running. Both therefore get the same scope construction, the same message
        shape, and the same error contract — instead of the dispatcher hand-rolling its own
        ``create_manager`` + ``invoke`` and bypassing the scope seam entirely.

        ``task`` wins over ``content``; ``content`` is what the tool path passes when a
        caller phrased the request as raw content or a ``question`` argument.
        """
        data = data if isinstance(data, dict) else {}
        # Route through the scope factory seam (SCOPE_AUDIT.md Step 1).
        # Today this returns parent_scope verbatim; Step 5 will replace this
        # passthrough with the authority-cap + local-tools construction in
        # exactly one place. Downstream manager_invoker.apply() still runs
        # _apply_manager_narrowing on the receiving side — that's where the
        # May 5 fix lives and where Step 5's logic will eventually move.
        inherited_scope = _scope_factory.for_sub_manager(
            parent_scope=scope_context,
            child_manager_name=self.manager_name,
        )
        request_id = log_request_id
        if self._should_trace_emi_ingress():
            logger.info(
                "[emi_team ingress] execute start request_id=%s has_task_file=%s has_inherited_scope=%s data_keys=%s",
                request_id,
                has_task_file,
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

        manager_content = (task or content or information
                           or f"Process request for {self.manager_name}")

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
