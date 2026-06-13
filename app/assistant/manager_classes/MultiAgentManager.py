import json
import uuid

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.agent_registry.agent_loader import AgentLoader
from app.assistant.manager_runtime.services.tool_scope_service import ToolScopeService
from app.assistant.utils.pydantic_classes import Message, ScopeContext, ToolResult
from app.assistant.utils.pipeline_state import ensure_pipeline_state, set_pipeline_state
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class MultiAgentManager:
    def __init__(self, name: str, manager_config: dict, tool_registry, agent_registry):
        """
        Base class for multi-agent managers.

        Manages agent execution flow, blackboard state, and tool registry.

        Parameters:
        - name: Name of the manager instance.
        - manager_config: Configuration for the manager.
        - tool_registry: Shared tool registry.
        - agent_registry: Registry containing agent definitions.
        """
        self.name = name
        self.blackboard = Blackboard()
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry
        self.manager_config = manager_config

        # Load agents from config
        self.agent_loader = AgentLoader(
            config_source=self.manager_config,
            blackboard=self.blackboard,
            agent_registry=self.agent_registry,
            tool_registry=self.tool_registry,
            parent=self
        )
        self.agent_loader.load_agents()

        self.flow_config = manager_config.get("flow_config") or {}
        self._validate_strict_routing_config()
        self.tool_scope_service = ToolScopeService()
        self._register_configured_events()
        self.set_manager_role_binding()

    def set_manager_role_binding(self):
        self.blackboard.update_state_value('role_bindings', self.manager_config.get("role_bindings", {}))

    def resolve_role_binding(self, role_name):
        bindings = self.blackboard.get_state_value('role_bindings', {})
        return bindings.get(role_name, role_name)

    def _validate_strict_routing_config(self) -> None:
        """
        Fail-fast routing validation (always on).

        Contract:
        - state_map must be explicit and internally coherent.
        - control node references must exist in manager config.
        - no hardcoded topology assumptions for nodes that explicitly set next_agent.
        """
        flow_cfg = self.flow_config if isinstance(self.flow_config, dict) else {}
        if not isinstance(flow_cfg, dict):
            raise ValueError(f"[{self.name}] flow_config must be a dict.")

        state_map = flow_cfg.get("state_map")
        if not isinstance(state_map, dict) or not state_map:
            raise ValueError(f"[{self.name}] flow_config.state_map must be a non-empty dict.")
        for src, dst in state_map.items():
            if not isinstance(src, str) or not src.strip():
                raise ValueError(f"[{self.name}] state_map contains an empty source key.")
            if not isinstance(dst, str) or not dst.strip():
                raise ValueError(f"[{self.name}] state_map['{src}'] must be a non-empty string target.")

        control_nodes = self.manager_config.get("control_nodes")
        if not isinstance(control_nodes, list):
            raise ValueError(f"[{self.name}] control_nodes must be a list in manager config.")
        control_node_names = {
            str(n.get("name")).strip()
            for n in control_nodes
            if isinstance(n, dict) and isinstance(n.get("name"), str) and str(n.get("name")).strip()
        }
        if not control_node_names:
            raise ValueError(f"[{self.name}] control_nodes must include at least one named node.")

        tool_return_cfg = flow_cfg.get("tool_return")
        if isinstance(tool_return_cfg, dict):
            tool_call_result_handler_node = tool_return_cfg.get("tool_call_result_handler_node")
            if not isinstance(tool_call_result_handler_node, str) or not tool_call_result_handler_node.strip():
                raise ValueError(
                    f"[{self.name}] flow_config.tool_return.tool_call_result_handler_node must be a non-empty string."
                )

            tool_call_result_handler_node = tool_call_result_handler_node.strip()
            if tool_call_result_handler_node not in state_map:
                raise ValueError(
                    f"[{self.name}] flow_config.tool_return.tool_call_result_handler_node="
                    f"'{tool_call_result_handler_node}' must exist in state_map."
                )

        critic_cfg = flow_cfg.get("critic")
        if isinstance(critic_cfg, dict):
            for k in ("subject_agent", "critic_agent", "continue_agent"):
                v = critic_cfg.get(k)
                if not isinstance(v, str) or not v.strip():
                    raise ValueError(f"[{self.name}] flow_config.critic.{k} must be a non-empty string.")

        summary_cfg = flow_cfg.get("summary")
        if isinstance(summary_cfg, dict):
            for k in ("source_agent", "summary_agent", "resume_agent"):
                v = summary_cfg.get(k)
                if not isinstance(v, str) or not v.strip():
                    raise ValueError(f"[{self.name}] flow_config.summary.{k} must be a non-empty string.")

    def _register_configured_events(self):
        events = self.manager_config.get("events", [])
        for event_name in events:
            handler_name = f"{event_name}_handler"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise ValueError(f"Handler '{handler_name}' not found in manager '{self.name}' for event '{event_name}'")
            event_hub = DI.event_hub
            try:
                event_hub.register_event(event_name, handler)
            except Exception:
                # Duplicate registration or conflicting handler
                logger.error(
                    "Event registration failed in manager '%s' for event '%s' (handler=%r, id=%s)",
                    self.name,
                    event_name,
                    handler,
                    id(handler),
                )
                logger.debug("event registration exception details", exc_info=True)
                raise

    def _emit_route_trace(
        self,
        *,
        cycle_number: int,
        last_agent: str | None,
        next_agent: str | None,
        route_source: str,
    ) -> None:
        trace_item = {
            "cycle": int(cycle_number),
            "last_agent": last_agent,
            "next_agent": next_agent,
            "route_source": route_source,
        }
        logger.info(
            "[%s] route_trace cycle=%s source=%s last_agent=%r next_agent=%r",
            self.name,
            cycle_number,
            route_source,
            last_agent,
            next_agent,
        )

        try:
            existing = self.blackboard.get_state_value("manager_route_trace", [])
            if not isinstance(existing, list):
                existing = []
            existing.append(trace_item)
            self.blackboard.update_global_state_value("manager_route_trace", existing[-200:])
        except Exception as e:
            logger.debug("[%s] Failed to persist manager_route_trace: %s", self.name, e, exc_info=True)

    def _drain_mailbox(self) -> None:
        """Drain anything queued for this invocation, applying each message
        to the blackboard. All dispatch logic lives in MailboxDispatcher;
        this manager method is a one-line entry point.

        Called at the top of every cycle in ``_run_loop``. Never raises.
        """
        invocation_id = self.blackboard.get_state_value("_invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id.strip():
            return
        from app.assistant.manager_runtime.mailbox import MailboxDispatcher
        MailboxDispatcher().drain_to(
            blackboard=self.blackboard,
            invocation_id=invocation_id,
            role_resolver=self.resolve_role_binding,
        )

    def _seed_local_blackboard_messages(self, seeded_payload: object, *, target_scope_id: str | None = None) -> int:
        if seeded_payload is None:
            return 0
        if not isinstance(seeded_payload, list):
            raise ValueError(f"[{self.name}] seeded_chat_messages must be a list when provided.")
        if target_scope_id is not None and (not isinstance(target_scope_id, str) or not target_scope_id.strip()):
            raise ValueError(f"[{self.name}] target_scope_id must be a non-empty string when provided.")
        seeded_count = 0
        for idx, item in enumerate(seeded_payload):
            if not isinstance(item, dict):
                raise ValueError(
                    f"[{self.name}] seeded_chat_messages[{idx}] must be an object, got {type(item).__name__}."
                )
            try:
                seeded_msg = Message.model_validate(item)
                if isinstance(target_scope_id, str) and target_scope_id.strip():
                    if seeded_msg.scope_id and seeded_msg.scope_id != target_scope_id.strip():
                        meta = seeded_msg.metadata if isinstance(seeded_msg.metadata, dict) else {}
                        meta = dict(meta)
                        meta["origin_scope_id"] = seeded_msg.scope_id
                        seeded_msg.metadata = meta
                    seeded_msg.scope_id = target_scope_id.strip()
                self.blackboard.add_msg(seeded_msg)
                seeded_count += 1
            except Exception as e:
                logger.error("[%s] Failed hydrating seeded_chat_messages[%d]: %s", self.name, idx, e)
                logger.debug("[%s] seeded message hydration exception details", self.name, exc_info=True)
                raise
        return seeded_count

    @staticmethod
    def _test_mode_active() -> bool:
        """True when running under a test harness (EMI_TEST_MODE set by
        tests/test_setup.py, or pytest's PYTEST_CURRENT_TEST). Production never
        sets either."""
        import os
        return (
            os.environ.get("EMI_TEST_MODE") == "1"
            or "PYTEST_CURRENT_TEST" in os.environ
        )

    def _apply_no_inbound_scope(self) -> None:
        """Handle an inbound Message that carries NO scope_context.

        PRODUCTION: fail loud — a manager must never run with the scope gate
        off. Every invocation must carry a scope (manager_invoker attaches one;
        see scratch/REQUEST_HANDLER_CALLERS_AUDIT.md).

        TEST: substitute a permissive scope (allow-all, authority 100,
        resources-all) so harnesses that don't build their own scope still run.
        This is equivalent to the prior ungated behavior, so tests are
        unaffected — only the production default flips from open to refuse.
        """
        if not self._test_mode_active():
            raise ValueError(
                f"[{self.name}] request_handler invoked with no scope_context — refusing "
                "to run ungated. Every manager invocation must carry a scope (route "
                "through manager_invoker, which attaches one)."
            )
        from app.assistant.utils.pydantic_classes import (
            ScopeApprovalPolicy,
            ScopeContext,
            ScopeResourcePolicy,
            ScopeToolPolicy,
        )
        test_scope = ScopeContext(
            scope_id="scope::test::auto",
            owner_id="test_owner", actor_id="test_actor", surface="test",
            tools=ScopeToolPolicy(allowed_tools=["all"]),
            approval=ScopeApprovalPolicy(authority_level=100),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        )
        self.blackboard.update_state_value("scope_context", test_scope.model_dump())
        self.blackboard.update_state_value("scope_contract_enforced", True)

    def request_handler(self, user_message: Message):
        """
        Handles a new task request by setting up the blackboard and invoking Delegator.
        """
        logger.info(f"🛠️ {self.name} received task: {user_message.content}")

        root_scope_id = None
        result = None
        try:
            resolved_task = user_message.task
            resolved_info = user_message.information
            resolved_data = user_message.data if isinstance(user_message.data, dict) else {}
            scope_context = getattr(user_message, "scope_context", None)

            self.blackboard.update_state_value('task', resolved_task)
            self.blackboard.update_state_value('information', resolved_info)
            if resolved_data:
                for key, value in resolved_data.items():
                    self.blackboard.update_state_value(key, value)

            # Surface room context onto the blackboard so the 17 downstream
            # readers of get_state_value("room_id") / "room_surface" /
            # "room_context_id" actually find it. Without this, every
            # non-master room silently fell back to master_room — most
            # consequentially in chat_memory_rag where it leaked cross-room
            # RAG matches into agent prompts. Surfaced by the room test harness
            # via slack/__test__ → chat_gate seeing master_room pod IDs.
            for _attr, _key in (
                ("room_id", "room_id"),
                ("room_surface", "room_surface"),
                ("room_context_id", "room_context_id"),
            ):
                _val = getattr(user_message, _attr, None)
                if isinstance(_val, str) and _val.strip():
                    self.blackboard.update_state_value(_key, _val.strip())

            # Preserve reply_to from the inbound message metadata so agents inside
            # the manager can set their _active_reply_to (needed for TTS routing).
            try:
                inbound_meta = getattr(user_message, "metadata", None)
                inbound_reply_to = inbound_meta.get("reply_to") if isinstance(inbound_meta, dict) else None
                if isinstance(inbound_reply_to, dict):
                    self.blackboard.update_state_value("inbound_reply_to", inbound_reply_to)
            except Exception:
                logger.debug("[%s] Failed to extract reply_to from inbound message metadata", self.name, exc_info=True)
            if scope_context is not None:
                try:
                    if hasattr(scope_context, "model_dump"):
                        self.blackboard.update_state_value("scope_context", scope_context.model_dump())
                    elif isinstance(scope_context, dict):
                        self.blackboard.update_state_value("scope_context", dict(scope_context))
                    else:
                        raise TypeError(
                            f"[{self.name}] scope_context must be ScopeContext or dict, got {type(scope_context).__name__}"
                        )
                    self.blackboard.update_state_value("scope_contract_enforced", True)
                except Exception as e:
                    logger.error("[%s] Failed loading scope_context into manager runtime: %s", self.name, e)
                    logger.debug("[%s] scope_context load exception details", self.name, exc_info=True)
                    raise
            else:
                self._apply_no_inbound_scope()
            try:
                self.tool_scope_service.initialize_scope(
                    blackboard=self.blackboard,
                    tool_registry=self.tool_registry,
                    manager_config=self.manager_config,
                    task=resolved_task,
                    information=resolved_info,
                )
            except Exception as e:
                logger.error("[%s] Failed to initialize tool scope: %s", self.name, e, exc_info=True)
                raise

            # Initialize pipeline state (global) for this request.

            ### This has a code smell....
            try:
                ps = ensure_pipeline_state(self.blackboard)
                ps["stage"] = None
                ps["pending_tool"] = None
                ps["last_tool_result_ref"] = None
                ps["last_tool_result_meta"] = None
                ps["resume_target"] = None
                ps["flags"] = {}
                ps["scratch"] = {}
                set_pipeline_state(self.blackboard, ps)
            except Exception:
                logger.error("[%s] Failed to initialize pipeline state", self.name)
                logger.debug("failed to initialize pipeline state exception details", exc_info=True)
                raise
            try:
                # Make flow policy readable to deterministic control nodes.
                self.blackboard.update_state_value("manager_flow_config", self.flow_config)
            except Exception as e:
                logger.error("[%s] Failed to set manager_flow_config on blackboard: %s", self.name, e)
                logger.debug("[%s] manager_flow_config injection exception details", self.name, exc_info=True)
                raise
            try:
                control_nodes_cfg = self.manager_config.get("control_nodes", [])
                node_cfg_map = {}
                if isinstance(control_nodes_cfg, list):
                    for entry in control_nodes_cfg:
                        if not isinstance(entry, dict):
                            continue
                        node_name = entry.get("name")
                        if not isinstance(node_name, str) or not node_name.strip():
                            continue
                        raw_cfg = entry.get("config", {})
                        if raw_cfg is None:
                            raw_cfg = {}
                        if not isinstance(raw_cfg, dict):
                            raise ValueError(
                                f"[{self.name}] control node '{node_name}' config must be a dict when provided."
                            )
                        node_cfg_map[node_name.strip()] = dict(raw_cfg)
                self.blackboard.update_state_value("manager_control_node_configs", node_cfg_map)
            except Exception as e:
                logger.error("[%s] Failed to set manager_control_node_configs on blackboard: %s", self.name, e)
                logger.debug("[%s] manager_control_node_configs injection exception details", self.name, exc_info=True)
                raise

            # Manager loop counters (reset every request).
            # These are useful for delegator-gated behaviors (e.g., run critic every N loops).
            try:
                self.blackboard.update_global_state_value("manager_name", self.name)
                self.blackboard.update_global_state_value("manager_loop_count", 0)    # 0-based
                self.blackboard.update_global_state_value("manager_loop_number", 1)   # 1-based (next loop number)
                self.blackboard.update_global_state_value(
                    "manager_max_cycles",
                    int(self.manager_config.get("max_cycles", 30)),
                )
            except Exception:
                # Keep request startup resilient; counters are optional.
                logger.error("[%s] Failed to initialize manager loop counters", self.name)
                logger.debug("failed to initialize manager loop counters exception details", exc_info=True)


            self.blackboard.add_request_id(user_message.request_id)
            self.set_manager_role_binding()  # put role bindings back

            # Execution trace capture (opt-in via manager config).
            try:
                trace_cfg = self.manager_config.get("execution_trace")
                if isinstance(trace_cfg, dict) and trace_cfg.get("enabled", False):
                    from app.assistant.execution_trace.execution_trace_recorder import ExecutionTraceRecorder
                    recorder = ExecutionTraceRecorder(manager_name=self.name, config=trace_cfg)
                    recorder.begin_run(
                        task_id=str(resolved_data.get("task_id") or ""),
                        task_text=str(resolved_task or ""),
                        information=str(resolved_info or ""),
                        spec_version=resolved_data.get("spec_version"),
                    )
                    self.blackboard.update_global_state_value("execution_trace_recorder", recorder)
            except Exception:
                logger.debug("[%s] Failed to initialize execution trace recorder", self.name, exc_info=True)

            # Create a single root scope for this request.
            root_scope_id = f"root_scope_{uuid.uuid4()}"
            logger.info(f"[{self.name}] Creating root scope '{root_scope_id}' for request")
            self.blackboard.push_call_context(self.name, self.name, root_scope_id)
            target_scope_id = self.blackboard.get_current_scope_id()
            seeded_count = self._seed_local_blackboard_messages(
                resolved_data.get("seeded_chat_messages"),
                target_scope_id=target_scope_id if isinstance(target_scope_id, str) and target_scope_id.strip() else None,
            )
            incoming_payload = resolved_data.get("seeded_chat_messages")
            logger.info(
                "[HISTORY_DIAG:C/D] seed_local_blackboard_messages: incoming_type=%s incoming_len=%s seeded_count=%d target_scope_id=%s",
                type(incoming_payload).__name__,
                len(incoming_payload) if hasattr(incoming_payload, "__len__") else "n/a",
                seeded_count,
                target_scope_id,
            )
            if seeded_count > 0:
                self.blackboard.update_state_value("seeded_chat_messages_count", seeded_count)

            result = self.run_agent_loop()
        except Exception as e:
            logger.error("❌ Error in request_handler: %s", e)
            logger.debug("error in request_handler exception details", exc_info=True)
            try:
                self.blackboard.update_state_value("error", True)
                self.blackboard.update_state_value("error_message", str(e))
            except Exception:
                logger.debug("[%s] Failed to set request_handler error state", self.name, exc_info=True)
            result = self.handle_exit_error()
        finally:
            # Always pop the root scope for this request.
            if root_scope_id:
                try:
                    while True:
                        ctx = self.blackboard.get_current_call_context()
                        if not ctx:
                            break
                        if ctx[2] == root_scope_id:
                            self.blackboard.pop_call_context()
                            break
                        logger.warning(
                            "[%s] Non-root scope remained at request end; unwinding: %r",
                            self.name,
                            ctx,
                        )
                        self.blackboard.pop_call_context()
                except Exception:
                    logger.error("[%s] Failed to pop root scope '%s'", self.name, root_scope_id)
                    logger.debug("failed to pop root scope exception details", exc_info=True)
        return result

    def _run_loop(self, max_cycles, delegator, delegator_data):
        # max_cycles budgets LLM-AGENT ACTIVATIONS — the unit that costs
        # time and money. Deterministic plumbing (control nodes, tool
        # dispatch/handling hops) does NOT consume budget; before
        # 2026-06-11 it did, which made "12 cycles" mean ~3 real agent
        # decisions and aborted legitimate work mid-answer.
        cycles = 0        # loop iterations (plumbing included) — backstop only
        agent_cycles = 0  # LLM-agent activations — the budgeted unit
        # A state_map cycle between control nodes never activates an agent,
        # so an iteration backstop is still required to stop infinite spins.
        iteration_cap = max(max_cycles * 8, 40)
        from app.assistant.control_nodes.control_node import ControlNode
        while True:
            logger.info(
                "[%s] MANAGER LOOP - Cycle %d (agents %d/%d)",
                self.name, cycles + 1, agent_cycles, max_cycles,
            )

            # Expose current loop counters on the blackboard BEFORE delegator runs.
            # This makes them visible to delegator/planner logic without relying on local variables.
            try:
                # Update global so nested scopes can always read it.
                self.blackboard.update_global_state_value("manager_loop_count", cycles)
                self.blackboard.update_global_state_value("manager_loop_number", cycles + 1)
            except Exception:
                logger.error("[%s] Failed to update manager loop counters", self.name)
                logger.debug("failed to update manager loop counters exception details", exc_info=True)

            # Drain mailbox: deliver outside-of-loop messages (e.g. @mention
            # steering, cancel signals from another thread, runtime context
            # injection) before any agent runs this cycle. Always at the safe
            # boundary — never mid-LLM-call.
            self._drain_mailbox()

            # Cooperative cancellation hook (used by orchestrators).
            if self.blackboard.get_state_value("cancelled", False) or self.blackboard.get_state_value("cancel", False):
                logger.warning(f"⚠️ {self.name} cancelled. Exiting loop.")
                return "cancelled"
            
            if agent_cycles >= max_cycles:
                logger.warning(
                    f"⚠️ {self.name} reached max agent cycles ({max_cycles}; "
                    f"{cycles} loop iterations)."
                )
                return "max_cycles"
            if cycles >= iteration_cap:
                logger.warning(
                    f"⚠️ {self.name} hit the iteration backstop ({iteration_cap}) "
                    f"with only {agent_cycles} agent cycles — likely a control-node "
                    f"routing loop."
                )
                return "max_cycles"
            if self.blackboard.get_state_value("exit", False):
                logger.info(f"✅ Task completed by {self.name}. Exiting loop.")
                return "success"
            if self.blackboard.get_state_value('error'):
                logger.warning(f"⚠️ {self.name} detected error state. Exiting loop.")
                return "error"

            # 1. Always call delegator to handle routing logic
            pre_next_agent = self.blackboard.get_state_value("next_agent")
            pre_last_agent = self.blackboard.get_state_value("last_agent")
            delegator.action_handler(Message(data_type='agent_activation', data=delegator_data))
            next_agent_name = self.blackboard.get_state_value('next_agent')
            route_source = (
                "explicit_override"
                if isinstance(pre_next_agent, str) and pre_next_agent.strip()
                else "state_map"
            )
            self._emit_route_trace(
                cycle_number=cycles + 1,
                last_agent=pre_last_agent if isinstance(pre_last_agent, str) else None,
                next_agent=next_agent_name if isinstance(next_agent_name, str) else None,
                route_source=route_source,
            )
            
            logger.debug("[%s] After delegator, next_agent = '%s'", self.name, next_agent_name)

            if not next_agent_name:
                logger.error("❌ Delegator did not determine a next agent. Exiting.")
                self.blackboard.update_state_value("error", True)
                self.blackboard.update_state_value("error_message", "delegator returned no next_agent")
                return "error"

            next_agent_name = self.resolve_role_binding(next_agent_name)
            next_agent = self.agent_registry.get_agent_instance(next_agent_name)
            if not next_agent:
                logger.error(f"❌ Failed to retrieve agent: {next_agent_name}. Exiting.")
                self.blackboard.update_state_value("error", True)
                self.blackboard.update_state_value("error_message", f"missing agent instance: {next_agent_name}")
                return "error"

            # 3. Activate the agent (it now runs within a guaranteed scope).
            next_scope_raw = self.blackboard.get_state_value("scope_context")
            next_scope: ScopeContext | None = None
            if next_scope_raw is not None:
                if isinstance(next_scope_raw, ScopeContext):
                    next_scope = next_scope_raw
                elif isinstance(next_scope_raw, dict):
                    next_scope = ScopeContext.model_validate(next_scope_raw)
                else:
                    raise ValueError(
                        f"[{self.name}] scope_context on blackboard must be ScopeContext or dict, "
                        f"got {type(next_scope_raw).__name__}"
                    )
            inbound_reply_to = self.blackboard.get_state_value("inbound_reply_to")
            activation_meta = {"reply_to": inbound_reply_to} if isinstance(inbound_reply_to, dict) else None
            next_agent.action_handler(Message(
                data_type='agent_activation',
                scope_context=next_scope,
                metadata=activation_meta,
            ))

            # Record step in execution trace (if enabled).
            # Only record planner steps — the agent that makes browser action decisions.
            try:
                recorder = self.blackboard.get_state_value("execution_trace_recorder")
                is_planner = isinstance(next_agent_name, str) and "planner" in next_agent_name
                if recorder is not None and is_planner:
                    snapshot = self.blackboard.get_state_value("playwright_latest_snapshot")
                    page_url = snapshot.get("url", "") if isinstance(snapshot, dict) else ""
                    recorder.record_step(
                        step_number=cycles + 1,
                        agent_name=next_agent_name,
                        action=str(self.blackboard.get_state_value("action") or ""),
                        action_input=str(self.blackboard.get_state_value("action_input") or ""),
                        tool_arguments=self.blackboard.get_state_value("tool_arguments"),
                        page_url=page_url,
                        reasoning=str(self.blackboard.get_state_value("what_i_am_thinking") or ""),
                        note=str(self.blackboard.get_state_value("note") or ""),
                    )
            except Exception:
                logger.debug("[%s] Failed to record execution trace step", self.name, exc_info=True)

            if not isinstance(next_agent, ControlNode):
                agent_cycles += 1
                try:
                    self.blackboard.update_global_state_value("manager_agent_cycles", agent_cycles)
                except Exception:
                    logger.debug("[%s] Failed to update manager_agent_cycles", self.name, exc_info=True)
            cycles += 1

    def run_agent_loop(self):
        """
        Runs the agent execution loop until the task is complete or max cycles are reached.
        """
        logger.info(f"🔄 Starting execution loop for {self.name}")
        max_cycles = self.manager_config.get("max_cycles", 30)
        self.delegator_name = self.resolve_role_binding('delegator')

        try:
            delegator = self.agent_registry.get_agent_instance(self.delegator_name)
            if delegator is None:
                logger.error(f"❌ No delegator found in manager: {self.name}")
            self.blackboard.update_state_value('last_agent', self.delegator_name)

            exit_reason = self._run_loop(max_cycles, delegator, {"flow_config": self.flow_config})
            return self.handle_exit_reason(exit_reason)

        except Exception as e:
            logger.error("❌ Fatal error in agent loop: %s", e)
            logger.debug("fatal error in agent loop exception details", exc_info=True)
            try:
                self.blackboard.update_state_value("error", True)
                self.blackboard.update_state_value("error_message", str(e))
            except Exception:
                logger.debug("[%s] Failed to set run_agent_loop error state", self.name, exc_info=True)
            return self.handle_exit_error()

    def handle_exit_reason(self, exit_reason):
        self._finalize_trace(exit_reason)
        if exit_reason == "success":
            return self.handle_exit()
        elif exit_reason == "max_cycles":
            return self.handle_exit_max_limit()
        elif exit_reason == "error":
            return self.handle_exit_error()
        else:
            return self.handle_unknown_exit()

    def handle_exit(self):
        logger.info("[%s] Exiting via control node.", self.name)

        final_raw = self.blackboard.get_state_value("final_answer")
        final_data = final_raw
        if final_raw is None:
            final_raw = ""

        # If the graceful-exit path ran (max_cycles / error / unknown), the
        # manager_exit_kind flag is set and `final_answer` is the structured
        # abort report from graceful_exit_control_node. Surface that as a
        # distinct result_type so downstream callers (run_task, response
        # formatters) can tell this wasn't a completion.
        exit_kind = str(self.blackboard.get_state_value("manager_exit_kind", "") or "").strip()
        is_aborted = exit_kind == "aborted"

        if is_aborted and isinstance(final_data, dict):
            content = str(final_data.get("final_answer_answer") or "").strip()
            if not content:
                content = f"{self.name} aborted without producing a final answer."
        else:
            content = final_raw if isinstance(final_raw, str) else json.dumps(final_raw)

        return ToolResult(
            result_type="manager_aborted" if is_aborted else "final_answer",
            content=content,
            data_list=[{}],
            data=final_data,
        )

    def handle_exit_max_limit(self):
        if self.flow_config.get("state_map", {}).get("max_limit"):
            exit_state = "max_limit"
        elif self.flow_config.get("state_map", {}).get("graceful_exit"):
            exit_state = "graceful_exit"
        elif self.flow_config.get("state_map", {}).get("error_exit"):
            exit_state = "error_exit"
        else:
            exit_state = "default_error_exit"
        return self.handle_graceful_exit(exit_state, true_reason="max_limit")

    def handle_exit_error(self):
        if self.flow_config.get("state_map", {}).get("error_exit"):
            exit_state = "error_exit"
        elif self.flow_config.get("state_map", {}).get("graceful_exit"):
            exit_state = "graceful_exit"
        else:
            exit_state = "default_error_exit"
        return self.handle_graceful_exit(exit_state, true_reason="error_exit")

    def handle_unknown_exit(self):
        if self.flow_config.get("state_map", {}).get("graceful_exit"):
            exit_state = "graceful_exit"
        elif self.flow_config.get("state_map", {}).get("error_exit"):
            exit_state = "error_exit"
        else:
            exit_state = "default_error_exit"
        return self.handle_graceful_exit(exit_state, true_reason="unknown_exit")

    def handle_graceful_exit(self, exit_state, *, true_reason=None):
        """
        Runs the agent execution loop in graceful exit mode.

        The exit loop re-enters _run_loop, which overwrites the
        manager_loop_count counters — so the aborted run's cycle count and
        TRUE abort reason are stashed first under dedicated keys for the
        graceful_exit_control_node's abort report. (Without this the report
        always claimed "Cycles run: 0" and labeled max-cycle aborts as
        generic graceful-exit routing.)
        """
        logger.info(f"🔄 Starting graceful exit loop for {self.name}, for state {exit_state}")
        try:
            aborted_cycles = self.blackboard.get_state_value("manager_agent_cycles", None)
            if aborted_cycles is None:
                aborted_cycles = self.blackboard.get_state_value("manager_loop_count", None)
            self.blackboard.update_global_state_value("manager_aborted_cycles", aborted_cycles)
            self.blackboard.update_global_state_value(
                "manager_abort_reason", true_reason or exit_state,
            )
        except Exception:
            logger.debug("[%s] Failed to stash abort counters", self.name, exc_info=True)
        max_cycles = self.manager_config.get("max_exit_cycles", 10)
        self.delegator_name = self.resolve_role_binding('delegator')
        self.blackboard.update_state_value('last_agent', exit_state)
        self.blackboard.update_state_value('error', False)  # Reset error for exit flow

        try:
            delegator = self.agent_registry.get_agent_instance(self.delegator_name)
            exit_reason = self._run_loop(max_cycles, delegator, {"flow_config": self.flow_config})
            return self.handle_graceful_exit_reason(exit_reason)
        except Exception as e:
            logger.error("❌ Fatal error in graceful exit loop: %s", e)
            logger.debug("fatal error in graceful exit loop exception details", exc_info=True)
            try:
                self.blackboard.update_state_value("error", True)
                self.blackboard.update_state_value("error_message", str(e))
            except Exception:
                logger.debug("[%s] Failed to set graceful_exit error state", self.name, exc_info=True)
            return self.handle_default_error_exit()

    def handle_graceful_exit_reason(self, exit_reason):
        self._finalize_trace(exit_reason)
        if exit_reason == "success":
            return self.handle_exit()
        if exit_reason == "max_cycles" or exit_reason == "error":
            return self.handle_default_error_exit()

    def _finalize_trace(self, exit_reason: str) -> None:
        """End the execution trace recording if one is active."""
        try:
            recorder = self.blackboard.get_state_value("execution_trace_recorder")
            if recorder is None:
                return
            final_answer = self.blackboard.get_state_value("final_answer")
            answer_text = ""
            if isinstance(final_answer, dict):
                answer_text = str(final_answer.get("final_answer_answer") or "")
            elif isinstance(final_answer, str):
                answer_text = final_answer
            recorder.end_run(
                exit_reason=exit_reason,
                success=(exit_reason == "success"),
                final_answer=answer_text,
            )
        except Exception:
            logger.debug("[%s] Failed to finalize execution trace", self.name, exc_info=True)

    def handle_default_error_exit(self):
        logger.warning("[%s] Exiting via default error path.", self.name)
        final_raw = self.blackboard.get_state_value("final_answer")
        final_raw_error = final_raw if isinstance(final_raw, str) else json.dumps(final_raw)
        error_message = self.blackboard.get_state_value("error_message", "")
        final = (f"Something has gone wrong during {self.name} execution. "
                 f"Error: {error_message or 'unknown'} "
                 f"Debug info: {final_raw_error}")

        return ToolResult(
            result_type="final_answer",
            content=final,
            data_list=[{}]
        )
