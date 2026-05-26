import json
from typing import Any, Dict, Optional

from app.assistant.agent_classes.Agent import Agent
from app.assistant.agent_runtime.exceptions import PromptRenderError
from app.assistant.utils.pydantic_classes import Message, ToolResult

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ToolArguments(Agent):
    """
    ToolArguments agent

    Gets input from the planner, tool and tool_args. Normalizes them using an LLM.

    """

    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None, components=None):
        super().__init__(
            name=name,
            blackboard=blackboard,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            llm_params=llm_params,
            parent=parent,
            components=components,
        )


    def _resolve_schema_for_target(self, target_name: str) -> Optional[dict]:
        """
        Returns a schema for the tool or agent
        """
        # Tool?
        if self.tool_registry.get_tool(target_name) is not None:
            return self.tool_registry.get_tool_form(target_name)

        # Agent?
        agent_cfg = self.agent_registry.get_agent_config(target_name)
        if isinstance(agent_cfg, dict):
            if agent_cfg.get("type") == "control_node":
                return None
            return self.agent_registry.get_agent_input_form(target_name)

        return None

    def _set_prompt_context_for_target(self, target_name: str, action_input: Any) -> None:
        """
        Populate state that prompt templates can use.
        Your prompt builder can reference these keys if desired.
        """
        try:
            tool_desc = self.tool_registry.get_tool_description(target_name)
        except Exception:
            tool_desc = None

        try:
            tool_args = self.tool_registry.get_tool_arguments_prompt(target_name)
        except Exception:
            tool_args = None

        # These are optional, but they make prompt templating easy and consistent.
        self.blackboard.update_state_value("target_name", target_name)
        self.blackboard.update_state_value("tool_description", tool_desc)
        self.blackboard.update_state_value("tool_args", tool_args)

        # Keep raw input available for templates and debugging.
        # Store as string for readability if it is a dict.
        if isinstance(action_input, dict):
            try:
                raw_str = json.dumps(action_input, ensure_ascii=False)
            except Exception:
                raw_str = str(action_input)
            self.blackboard.update_state_value("arguments", raw_str)
        elif isinstance(action_input, str):
            self.blackboard.update_state_value("arguments", action_input)
        else:
            self.blackboard.update_state_value("arguments", None)

    def action_handler(self, message: Message) -> ToolResult:
        timer_id = None
        self._set_agent_busy()
        try:
            # Keep the base behavior that unpacks message.agent_input into state, enforces scope, etc.
            self._update_blackboard_state(message)
            self._store_incoming_message(message)

            target_name = self.blackboard.get_state_value('target_name')
            if not target_name:
                msg = f"[{self.name}] Missing target tool/agent name. Expected blackboard.target_name."
                logger.error(msg)
                self.blackboard.update_state_value("error_message", msg)
                self.blackboard.update_state_value("error", True)
                return ToolResult(result_type="tool_arguments_error", content=msg, data={"error": msg})

            action_input = self.blackboard.get_state_value('arguments')
            self._set_prompt_context_for_target(target_name, action_input)

            schema = self._resolve_schema_for_target(target_name)
            if not schema:
                # No args needed (control node, agent without input schema, or unknown target).
                # For unknown target, fail loudly so you do not silently call with {}.
                is_tool = self.tool_registry.get_tool(target_name) is not None
                is_agent = isinstance(self.agent_registry.get_agent_config(target_name), dict)
                if not is_tool and not is_agent:
                    msg = f"[{self.name}] Target '{target_name}' not found in tool or agent registry."
                    logger.error(msg)
                    return ToolResult(result_type="tool_arguments_error", content=msg, data={"error": msg})
                return ToolResult(
                    result_type="tool_arguments",
                    content=f"{self.name} produced empty arguments for '{target_name}'.",
                    data={"target_name": target_name, "arguments": {}},
                )

            # Fast path: if action_input is already a valid dict that matches
            # the schema, skip the LLM call entirely.
            fast_result = self._try_fast_validate(target_name, action_input, schema)
            if fast_result is not None:
                return fast_result

            # Build prompt using your standard prompt builder.
            try:
                messages = self.construct_prompt(message)
            except Exception as e:
                logger.error(f"[{self.name}] Error during prompt construction: {e}")
                logger.debug("[%s] prompt construction exception details", self.name, exc_info=True)
                raise PromptRenderError(f"[{self.name}] Prompt construction failed: {e}") from e

            # Run LLM with the resolved schema.
            llm_result = self._run_llm_with_schema(messages, schema)

            # Normalize and persist result.
            return self.process_llm_result(llm_result, target_name=target_name)

        except Exception as e:
            logger.error(f"[{self.name}] Unhandled exception: {e}", exc_info=True)
            raise
        finally:
            try:
                self._set_agent_idle()
            except Exception as e:
                logger.error(f"[{self.name}] Failed to release busy lock: {e}", exc_info=True)

    def _try_fast_validate(self, target_name: str, action_input: Any, schema) -> Optional[ToolResult]:
        """Skip the LLM if action_input is already a valid dict matching the schema.

        schema is the Pydantic model class (tool_name + arguments envelope).
        Tries to validate action_input against it. If it passes, skip the LLM.

        Returns a ToolResult on success, or None to fall through to the LLM path.
        """
        # action_input may be a dict, a JSON string, or a bare value.
        args = action_input
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    args = None
            except Exception:
                args = None
            # Bare value like "e1158" — if the schema has exactly one required
            # field, wrap the value as that field.
            if args is None and isinstance(action_input, str) and action_input.strip():
                try:
                    from pydantic import BaseModel
                    if isinstance(schema, type) and issubclass(schema, BaseModel):
                        fields = schema.model_fields
                        args_field = fields.get("arguments")
                        if args_field and hasattr(args_field.annotation, "model_fields"):
                            inner_fields = args_field.annotation.model_fields
                            required = [k for k, v in inner_fields.items() if v.is_required()]
                            if len(required) == 1:
                                args = {required[0]: action_input.strip()}
                except Exception:
                    pass
        if not isinstance(args, dict) or not args:
            return None

        try:
            from pydantic import BaseModel
            if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
                return None

            schema.model_validate({"tool_name": target_name, "arguments": args})

            normalized = {"target_name": target_name, "arguments": args}
            self.blackboard.update_state_value("tool_arguments", normalized)
            logger.info(
                "[%s] Fast-validated arguments for '%s' — skipped LLM call.",
                self.name, target_name,
            )
            return ToolResult(
                result_type="tool_arguments",
                content=f"{self.name} fast-validated arguments for '{target_name}'.",
                data=normalized,
            )
        except Exception as e:
            # Log structured-ish info so we can grep which tools routinely
            # confuse the planner (= signal their description / Contract
            # Inputs need richer hints).
            failure_class = self._classify_validation_error(e)
            arg_keys = sorted(args.keys()) if isinstance(args, dict) else []
            err_line = str(e).splitlines()[0] if str(e) else ""
            logger.info(
                "[%s] fast_validate_miss tool=%s failure_class=%s arg_keys=%s err=%s",
                self.name, target_name, failure_class, arg_keys, err_line,
            )
            return None

    @staticmethod
    def _classify_validation_error(err: Exception) -> str:
        """Coarse bucket for fast_validate misses. Used only for log signal."""
        try:
            from pydantic import ValidationError
            if isinstance(err, ValidationError):
                types = {e.get("type", "") for e in err.errors()}
                if any(t == "missing" for t in types):
                    return "missing_required"
                if any(t in ("extra_forbidden", "extra_field") for t in types):
                    return "unknown_field"
                # Pydantic v2 type/parse error codes end in `_type` or `_parsing`.
                if any(t.endswith("_type") or t.endswith("_parsing") for t in types):
                    return "type_mismatch"
                return "validation_other"
        except Exception:
            pass
        return "other"

    def process_llm_result(self, result: Any, *, target_name: str) -> ToolResult:
        self._maybe_print_llm_result(result)

        if isinstance(result, str):
            msg = f"[{self.name}] LLM returned string instead of dict for '{target_name}': {result}"
            logger.error(msg)
            return ToolResult(result_type="tool_arguments_error", content=msg, data={"error": msg})

        if not isinstance(result, dict):
            msg = f"[{self.name}] LLM returned invalid type for '{target_name}': {type(result)}"
            logger.error(msg)
            return ToolResult(result_type="tool_arguments_error", content=msg, data={"error": msg})

        # Unwrap: local tools produce {tool_name, arguments} envelope;
        # MCP tools produce flat dicts (the arguments directly).
        args = result.get("arguments")

        if args is None:
            # No "arguments" key — MCP flat format. Strip the tool_name
            # field if present; everything else IS the arguments.
            args = {k: v for k, v in result.items() if k != "tool_name"}
        if not isinstance(args, dict):
            msg = f"[{self.name}] LLM returned non-dict 'arguments' for '{target_name}': {type(args)}"
            logger.error(msg)
            return ToolResult(result_type="tool_arguments_error", content=msg, data={"error": msg})

        return ToolResult(
            result_type="tool_arguments",
            content=f"{self.name} produced arguments for '{target_name}'.",
            data={"target_name": target_name, "arguments": args},
        )