# Note to coding agents: This file should not be modified without user permission.
import json
from typing import List, Dict, Any, Optional, Union
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.assistant.agent_registry.agent_registry import AgentRegistry

from app.assistant.utils.pydantic_classes import Message, ScopeContext, ToolResult
from app.assistant.agent_runtime.types import AgentComponents
from app.assistant.agent_runtime.factories.agent_components_factory import AgentComponentsFactory
from app.assistant.agent_runtime.exceptions import PromptRenderError

from app.assistant.utils.logging_config import get_logger
from app.assistant.performance.performance_monitor import performance_monitor

logger = get_logger(__name__)

class Agent:
    def __init__(
        self,
        name,
        blackboard,
        agent_registry: "AgentRegistry",
        tool_registry,
        llm_params=None,
        parent=None,
        components: Optional[AgentComponents] = None,
    ):
        self.name = name
        self.parent = parent
        self.blackboard = blackboard
        self.agent_registry = agent_registry
        self.config = agent_registry.get_agent_config(self.name)
        self.tool_registry = tool_registry
        self.llm_params = llm_params if llm_params is not None else (self.config.get("llm_params") or {})
        self.append_fields = self.config.get("append_fields", [])
        self.components = components or AgentComponentsFactory.build_for_agent_global(self)

        # Tool/node policy resolver — determines allowed/visible tools and nodes.
        from app.assistant.agent_runtime.services.tool_policy_resolver import ToolPolicyResolver
        self._tool_policy = ToolPolicyResolver(
            agent_name=name,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            blackboard=blackboard,
        )

        # Action contract service — normalizes and validates LLM action payloads.
        from app.assistant.agent_runtime.services.action_contract_service import ActionContractService
        self._action_contract = ActionContractService(
            agent_name=name,
            config=self.config,
            policy_resolver=self._tool_policy,
            action_validator=self.components.action_validator,
        )

        # Input applier — unpacks inbound messages onto the blackboard.
        from app.assistant.agent_runtime.services.agent_input_applier import AgentInputApplier
        self._input_applier = AgentInputApplier(agent_name=name, blackboard=blackboard)

        # Result applier — writes LLM output to blackboard and creates audit messages.
        from app.assistant.agent_runtime.services.agent_result_applier import AgentResultApplier
        self._result_applier = AgentResultApplier(agent_name=name, config=self.config, blackboard=blackboard)

        # Per-request reply routing (transport-agnostic).
        # Set from inbound message.metadata.reply_to / message.request_id.
        self._active_reply_to: Optional[dict] = None
        self._active_request_id: Optional[str] = None

    def resolve_role_binding(self, role_name):
        bindings = self.blackboard.get_state_value('manager_role_bindings', {})
        return bindings.get(role_name, role_name)

    def get_llm_interface(self):
        return self.components.llm_client.get_llm_interface(agent=self)

    def get_default_llm_params(self):
        return {}

    def _set_agent_busy(self):
        self.components.status_tracker.set_busy(self.name, True)

    def _set_agent_idle(self):
        self.components.status_tracker.set_busy(self.name, False)

    def _check_for_quota_error(self, response_text: Any) -> None:
        self.components.llm_client.check_for_quota_error(
            agent_name=self.name,
            response_text=response_text,
        )

    def _update_blackboard_state(self, message: Message):
        """Unpack inbound message onto blackboard via AgentInputApplier."""
        self._input_applier.apply(self, message)

    def _store_incoming_message(self, message: Message):
        if message.content:
            self.blackboard.add_msg(message)

    def _increment_manager_agent_steps(self) -> None:
        """
        Track per-agent execution counts for manager-level deterministic gates
        (for example critic cadence decisions).
        """
        current = self.blackboard.get_state_value("manager_agent_steps", {})
        if current is None:
            current = {}
        if not isinstance(current, dict):
            raise TypeError(
                f"[{self.name}] manager_agent_steps must be a dict when provided, got {type(current)}."
            )

        updated = dict(current)
        prior = updated.get(self.name, 0)
        if not isinstance(prior, int):
            raise TypeError(
                f"[{self.name}] manager_agent_steps['{self.name}'] must be int, got {type(prior)}."
            )
        updated[self.name] = prior + 1
        self.blackboard.update_global_state_value("manager_agent_steps", updated)

    def _add_extra_msgs(self, message: Message):
        # default no-op; override in subclasses if needed
        pass

    def _run_llm_with_schema(self, messages, schema: Union[dict, str, None]):
        """
        Run the LLM with an optional structured output schema.

        schema:
          - dict: JSON schema, use_json = True
          - str: model specific format hint, use_json = False
          - None: no structured output
        """
        use_json = isinstance(schema, dict)

        try:
            result = self.call_llm(
                messages=messages,
                response_format=schema,
                use_json=use_json,
            )
            return result
        except Exception as e:
            logger.error(f"[{self.name}] LLM execution failed: {e}")
            raise

    def action_handler(self, message: Message) -> Any:
        """Agent lifecycle: prepare → build prompt → call LLM → finalize."""
        timer_id = performance_monitor.start_timer(f"agent_{self.name}", message.id)
        self._set_agent_busy()
        try:
            self._prepare_execution_context(message)
            messages = self._build_messages(message)
            raw_result = self._execute_model(messages)
            result = self._finalize_execution(raw_result, message)
            self._record_success(timer_id, message)
            return result
        except Exception as e:
            self._record_failure(timer_id, e, message)
            raise
        finally:
            try:
                self._set_agent_idle()
            except Exception as idle_err:
                logger.error("[%s] Failed to release busy lock: %s", self.name, idle_err)

    # ------------------------------------------------------------------
    # Lifecycle phases (called by action_handler)
    # ------------------------------------------------------------------

    def _prepare_execution_context(self, message: Message) -> None:
        """Phase 1: Unpack input, store message, set agent tracking."""
        self._update_blackboard_state(message)
        self._store_incoming_message(message)
        self.blackboard.update_state_value("last_agent", self.name)
        self._increment_manager_agent_steps()

    def _build_messages(self, message: Message) -> List[Dict[str, str]]:
        """Phase 2: Construct the LLM prompt."""
        try:
            return self.construct_prompt(message)
        except Exception as e:
            logger.error("[%s] Error during prompt construction: %s, %s", self.name, e, message)
            raise PromptRenderError(f"[{self.name}] Prompt construction failed: {e}") from e

    def _execute_model(self, messages: List[Dict[str, str]]) -> Any:
        """Phase 3: Call the LLM."""
        schema = self.config.get("structured_output")
        return self._run_llm_with_schema(messages, schema)

    def _finalize_execution(self, raw_result: Any, message: Message) -> Any:
        """Phase 4: Validate, apply to state, create audit message, route."""
        self._add_extra_msgs(message)
        return self.process_llm_result(raw_result)

    def _record_success(self, timer_id: str, message: Message) -> None:
        """Record successful execution timing."""
        performance_monitor.end_timer(timer_id, {
            "status": "success",
            "agent_name": self.name,
            "message_id": message.id,
        })

    def _record_failure(self, timer_id: str, error: Exception, message: Message) -> None:
        """Record failed execution timing."""
        logger.error("[%s] Unhandled exception in action_handler: %s", self.name, error)
        performance_monitor.end_timer(timer_id, {
            "status": "error",
            "agent_name": self.name,
            "error": str(error)[:200],
        })

    def call_llm(
            self,
            messages: List[Dict[str, Any]],
            response_format: Optional[Union[dict, str]] = None,
            use_json: bool = False,
    ) -> Any:
        return self.components.llm_client.call_structured_output(
            agent=self,
            messages=messages,
            response_format=response_format,
            use_json=use_json,
        )

    def _resolve_prompt_debug_flags(self) -> tuple[bool, bool]:
        return self.components.llm_client.resolve_prompt_debug_flags(agent=self)

    def _resolve_llm_result_debug_flag(self) -> bool:
        return self.components.llm_client.resolve_llm_result_debug_flag(agent=self)

    def _maybe_print_llm_result(self, result: Any) -> None:
        if not self._resolve_llm_result_debug_flag():
            return
        rendered = json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
        logger.model_output(f"--- LLM RESULT for {self.name} ---\n{rendered}\n---------------------------------")

    def construct_prompt(self, message: Message = None) -> List[Dict[str, str]]:
        return self.components.prompt_builder.construct_prompt(
            self,
            message,
            self.components.entity_injector.get_injection_keys(),
        )

    def get_system_prompt(self, message: Message = None):
        return self.components.prompt_builder.get_system_prompt(
            self,
            message,
            self.components.entity_injector.get_injection_keys(),
        )

    def get_user_prompt(self, message: Message = None):
        return self.components.prompt_builder.get_user_prompt(
            self,
            message,
            self.components.entity_injector.get_injection_keys(),
        )

    def get_allowed_nodes(self) -> List[str]:
        """Return the list of agent nodes this agent can delegate to."""
        return self._tool_policy.get_allowed_nodes()

    def get_tools(self) -> list:
        """Return the full list of tools this agent is allowed to call."""
        return self._tool_policy.get_tools()

    def get_visible_tools(self) -> list:
        """Return the subset of allowed tools shown in prompts."""
        return self._tool_policy.get_visible_tools()

    def get_tool_descriptions(self):
        """Return tool descriptions for visible tools."""
        return self._tool_policy.get_tool_descriptions()

    def get_tool_arguments_prompt(self):
        """Return tool argument prompts for visible tools."""
        return self._tool_policy.get_tool_arguments_prompt()

    # Build a labeled, chronological log with only the last real tool_result

    def build_recent_history(self, agent_messages):
        return self.components.history_formatter.format_recent_history(agent_messages or [])

    def build_history(self) -> str:
        """
        Default history policy for non-chat agents.
        Most agent calls are one-shot, so return empty unless overridden.
        """
        return ""

    def _format_entity_field(self, entities: List[str], field_name: str) -> str:
        return self.components.entity_injector.format_entity_field(entities, field_name)

    def _format_entity_multi_field(self, entities: List[str], field_names: List[str]) -> str:
        return self.components.entity_injector.format_entity_multi_field(entities, field_names)

    def _resolve_resource(self, resource_id: str) -> Any:
        return self.components.context_injector.resolve_resource(self, resource_id)

    def generate_injections_block(self, prompt_injections, message=None):
        return self.components.context_injector.generate_injections_block(
            self,
            prompt_injections,
            message,
            self.components.entity_injector.get_injection_keys(),
        )

    def _apply_llm_result_to_state(self, result_dict: dict):
        """Write LLM output keys to blackboard via AgentResultApplier."""
        self._result_applier.apply_result_to_state(result_dict)

    def _create_response_message(self, result_dict: dict):
        """Create audit message via AgentResultApplier."""
        self._result_applier.create_audit_message(result_dict)

    def _validate_list_action_and_action_input(
        self, result_dict: dict, action_list: list, allowed_actions: set[str]
    ) -> None:
        """Extension hook for list-based action payloads. Base Agent rejects them."""
        logger.error(
            "[%s] List-based action payload is not supported for this agent. action=%r",
            self.name,
            action_list,
        )
        raise ValueError(f"[{self.name}] List-based action payload is not supported.")

    def _validate_action_and_action_input(self, result_dict: dict) -> None:
        """Normalize and validate the action contract via ActionContractService."""
        self._action_contract.enforce(result_dict, self._validate_list_action_and_action_input)

    def _is_tool_action(self, action_name: str) -> bool:
        return self._tool_policy.is_tool_action(action_name)

    def _handle_flow_control(self, result_dict: dict):
        self.components.flow_controller.route(self, result_dict)

    def process_llm_result(self, result):
        """
        The main template method for processing LLM results.
        It orchestrates the validation, state updates, messaging, and flow control.
        """

        self._maybe_print_llm_result(result)

        # Step 1: Validate input
        if isinstance(result, str):
            logger.error(f"[{self.name}] LLM returned plain string (invalid structured output): {result}")
            raise ValueError(f"[{self.name}] Expected dict from LLM, got string.")
        if not isinstance(result, dict):
            logger.error(f"[{self.name}] LLM result is not a dict: {type(result)}")
            raise TypeError(f"[{self.name}] Expected dict from LLM, got {type(result)}.")

        result_dict = result

        # Step 1b: Validate action/action_input contract before mutating state
        self._validate_action_and_action_input(result_dict)

        # Step 2: Apply state changes (Shared Logic)
        self._apply_llm_result_to_state(result_dict)

        # Step 3: Create response message (Overridable Logic)
        self._create_response_message(result_dict)

        # Step 4: Handle flow control (Shared Logic)
        self._handle_flow_control(result_dict)

        # Step 4b: Emit a small progress fact for UI
        try:
            self.components.progress_emitter.emit_planner_decision(self, result_dict)
        except Exception as e:
            logger.error("[%s] Failed to emit planner progress fact: %s", self.name, e)
            logger.debug("[%s] planner progress emit exception details", self.name, exc_info=True)

        return ToolResult(
            result_type="llm_result",
            content=f"{self.name} acted.",
            data=result_dict
        )
