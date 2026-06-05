import json
import os
import re
import importlib
from pathlib import Path
from typing import List, Dict

from jinja2 import Template

from app.assistant.agent_runtime.exceptions import PromptRenderError
from app.assistant.agent_classes.PlaywrightAgent import PlaywrightAgent
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pipeline_state import get_pending_tool, set_pending_tool_arguments
from app.assistant.utils.pydantic_classes import Message

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


class ToolArguments(PlaywrightAgent):
    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent)
        self.tool_name = None
        self.tool_type = None
    def action_handler(self, message: Message):
        self._set_agent_busy()
        calling_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(calling_agent, str) or not calling_agent.strip():
            raise ValueError(f"[{self.name}] last_agent must be a non-empty string before tool argument generation.")
        calling_agent = calling_agent.strip()
        try:
            self.blackboard.update_state_value("next_agent", None)

            logger.debug(f"[{self.name}] Handling tool/agent argument selection.")

            pending = get_pending_tool(self.blackboard) or {}
            selected_node = pending.get("name")
            if not selected_node:
                logger.error(f"[{self.name}] No tool or agent selected to generate arguments for.")
                return

            try:
                messages = self.construct_prompt(message)
            except Exception as e:
                logger.error(f"[{self.name}] Error during prompt construction: {e}")
                logger.debug("[%s] tool argument prompt construction exception details", self.name, exc_info=True)
                raise PromptRenderError(f"[{self.name}] Prompt construction failed: {e}") from e

            # 🔀 Determine if it's a tool, agent, or control node
            if self.tool_registry.get_tool(selected_node):
                schema = self.tool_registry.get_tool_form(selected_node)
                self.tool_type = "Tool"
            elif self.agent_registry.get_agent_config(selected_node):
                agent_config = self.agent_registry.get_agent_config(selected_node)
                # Check if it's a control node
                if agent_config.get("type") == "control_node":
                    # It's a control node - no arguments needed, just pass it through
                    logger.info(f"[{self.name}] '{selected_node}' is a control node, no arguments needed.")
                    set_pending_tool_arguments(self.blackboard, {})
                    self._route_and_restore_caller(calling_agent)
                    return
                else:
                    # It's a local agent - check if it has an input schema
                    schema = self.agent_registry.get_agent_input_form(selected_node)
                    if schema:
                        # Agent has input schema - generate arguments
                        logger.info(f"[{self.name}] '{selected_node}' is a local agent with input schema, generating arguments.")
                        self.tool_type = "Agent"
                    else:
                        # Agent has no input schema - no arguments needed
                        logger.info(f"[{self.name}] '{selected_node}' is a local agent without input schema, no arguments needed.")
                        set_pending_tool_arguments(self.blackboard, {})
                        self._route_and_restore_caller(calling_agent)
                        return
            else:
                logger.error(f"[{self.name}] '{selected_node}' is neither a tool nor a registered agent.")
                return

            if not schema:
                logger.info(f"[{self.name}] No argument schema found for '{selected_node}' returning None.")
                return

            result = self._run_llm_with_schema(messages, schema)

            try:
                result = self.process_llm_result(result)
            except Exception as e:
                logger.error(f"Error processing result: {e}")
                raise

            self._route_and_restore_caller(calling_agent)
            return result
        except Exception as e:
            logger.error(f"[{self.name}] Unhandled exception in action_handler: {e}")
            raise
        finally:
            # ALWAYS release the busy lock, even on exceptions
            try:
                self._set_agent_idle()
            except Exception as e:
                logger.error(f"[{self.name}] Failed to release busy lock: {e}")

    def _route_and_restore_caller(self, calling_agent: str) -> None:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError(f"[{self.name}] manager_flow_config must be a dict.")
        state_map = flow_cfg.get("state_map")
        if not isinstance(state_map, dict):
            raise ValueError(f"[{self.name}] manager_flow_config.state_map must be a dict.")
        next_agent = state_map.get(self.name)
        if not isinstance(next_agent, str) or not next_agent.strip():
            raise ValueError(f"[{self.name}] Missing state_map route for '{self.name}'.")
        self.blackboard.update_state_value("last_agent", calling_agent)
        self.blackboard.update_state_value("next_agent", next_agent.strip())



    def construct_prompt(self, message: Message = None) -> List[Dict[str, str]]:
        """
        Constructs the system + user prompt using the correct tool's argument template.
        """
        system_prompt = self.get_system_prompt(message).replace('\n\n', '\n')
        user_prompt = self.get_user_prompt(message).replace('\n\n', '\n')

        system_prompt = re.sub(r'\n{3,}', '\n\n', system_prompt)
        user_prompt = re.sub(r'\n{3,}', '\n\n', user_prompt)

        msg = [
            {'role': 'system', 'content': system_prompt or f"[{self.name}] Error forming system prompt."},
            {'role': 'user', 'content': user_prompt or f"[{self.name}] Error forming user prompt."}
        ]
        return msg

    def get_system_prompt(self, message: Message = None):

        system_prompt_template = self.config.get("prompts", {}).get("system", "")

        if not system_prompt_template:
            logger.error(f"[{self.name}] No system prompt found.")
            return f"No system prompt available for {self.name}."

        # Load context items
        prompt_injections = self.config.get("system_context_items", {})
        if prompt_injections is not None:
            system_context = self.generate_injections_block(prompt_injections, message)
        else:
            system_context = None

        try:
            template = Template(system_prompt_template)
            rendered_output = template.render(**system_context or {}).replace('\n\n', '\n')
            return rendered_output

        except Exception as e:
            logger.error(f"[{self.name}] ERROR while rendering system prompt: {e}")
            raise

    def get_user_prompt(self, message: Message = None):
        """
        Retrieves and renders the user prompt for the selected tool's argument generation.
        """

        selected_tool = self.blackboard.get_state_value('selected_tool')

        user_prompt_template = self.config.get("prompts", {}).get("user", "")

        prompt_injections = self.config.get("user_context_items", {})
        user_context = self.generate_injections_block(prompt_injections, message)

        tool_args = self.tool_registry.get_tool_arguments_prompt(selected_tool)  # this function already renders it

        # Fetch the tool description and tool prompt. Already rendered
        tool_description = self.tool_registry.get_tool_description(selected_tool)

        user_context.update({'tool_description': tool_description,
                             'tool_args': tool_args})

        # Get agent configuration to get agents system prompt

        try:
            template = Template(user_prompt_template)
            rendered_output = template.render(**user_context or {}).replace('\n\n', '\n')
            return rendered_output

        except Exception as e:
            logger.error(f"[{self.name}] ERROR while rendering system prompt: {e}")
            raise


    def process_llm_result(self, result):
        logger.debug(f"[{self.name}] Processing LLM Result: {result}")
        self._maybe_print_llm_result(result)

        # Handle case where result is a string (error message)
        if isinstance(result, str):
            logger.error(f"[{self.name}] LLM returned string instead of dict: {result}")
            # Create a proper error structure
            result_dict = {
                "error": result,
                "action": "error",
                "content": f"LLM Error: {result}"
            }
        else:
            # Convert result to dictionary
            result_dict = result

        if not isinstance(result_dict, dict):
            logger.error(f"[{self.name}] LLM result is not a dict: {type(result_dict)} - {result_dict}")
            # Create a proper error structure
            result_dict = {
                "error": f"Invalid result type: {type(result_dict)}",
                "action": "error",
                "content": f"Invalid result type: {type(result_dict)}"
            }

        # Apply manager-specific argument normalizers (if configured).
        result_dict = self._apply_argument_normalizers(result_dict)

        # Deterministic normalization for `web_page_coords` in dev:
        # We want strict failures (no silent fallbacks).
        try:
            pending = get_pending_tool(self.blackboard) or {}
            selected_tool = pending.get("name")
            if (
                isinstance(result_dict, dict)
                and isinstance(selected_tool, str)
                and selected_tool == "web_page_coords"
                and isinstance(result_dict.get("arguments"), dict)
            ):
                a = result_dict["arguments"]
                if a.get("strict") is not True:
                    a["strict"] = True
        except Exception as e:
            logger.debug("Failed to enforce strict=True on web_page_coords arguments: %s", e, exc_info=True)

        # Deterministic normalization for vision helper agent inputs:
        # Vision agents require an absolute path to an on-disk PNG.
        #
        # The planner often only sees the screenshot filename (e.g. "mcp_....png") in summaries,
        # because `[mcp_image_path: ...]` markers are stripped and converted to multimodal blocks
        # before the LLM sees them.
        #
        # So: if the callee expects `image` and it is not absolute, resolve it to:
        #   <repo_root>/uploads/temp/<filename>
        #
        # Fail loudly if the resulting file does not exist.
        try:
            pending = get_pending_tool(self.blackboard) or {}
            selected_node = pending.get("name")
            if (
                isinstance(result_dict, dict)
                and isinstance(result_dict.get("image"), str)
                and isinstance(selected_node, str)
                and selected_node in {"shared::vision_page_scout", "shared::vision_target_picker"}
            ):
                raw_image = (result_dict.get("image") or "").strip()
                if not raw_image:
                    raise RuntimeError(
                        f"[{self.name}] Vision agent '{selected_node}' requires image path, but got empty 'image'."
                    )

                # Resolve relative/filename-only to uploads/temp/<filename>
                img_path = Path(raw_image)
                if not os.path.isabs(raw_image):
                    fname = img_path.name
                    if not fname:
                        raise RuntimeError(
                            f"[{self.name}] Vision agent '{selected_node}' got invalid image value: {raw_image!r}"
                        )
                    from app.assistant.utils.path_utils import get_uploads_dir
                    candidate = (get_uploads_dir() / "temp" / fname).resolve()
                    result_dict["image"] = str(candidate)
                    img_path = candidate

                # Validate existence
                if not img_path.exists():
                    raise RuntimeError(
                        f"[{self.name}] Vision agent '{selected_node}' image path does not exist: {str(img_path)}"
                    )
                # Validate png
                if img_path.suffix.lower() != ".png":
                    raise RuntimeError(
                        f"[{self.name}] Vision agent '{selected_node}' requires a .png image, got: {str(img_path)}"
                    )
        except Exception as e:
            logger.error(f"[{self.name}] Vision image normalization failed: {e}")
            raise

        set_pending_tool_arguments(self.blackboard, result_dict)

        response_message = Message(
            data_type="agent_result",
            sender=self.name,
            receiver="Blackboard",
            content=f"{self.name} acted\n Result: {json.dumps(result_dict)}"
        )
        self.blackboard.add_msg(response_message)
        logger.info(f"[{self.name}] Recorded response in blackboard history.")

        return result


    def _apply_argument_normalizers(self, result_dict: dict) -> dict:
        if not isinstance(result_dict, dict):
            return result_dict
        manager_config = {}
        try:
            manager_config = getattr(self.parent, "manager_config", {}) if self.parent is not None else {}
        except Exception:
            manager_config = {}
        normalizers = []
        if isinstance(manager_config, dict):
            normalizers = manager_config.get("tool_argument_normalizers") or []
        if not isinstance(normalizers, list) or not normalizers:
            return result_dict

        pending = get_pending_tool(self.blackboard) or {}
        for path in normalizers:
            if not isinstance(path, str) or not path.strip():
                continue
            try:
                mod_path, func_name = path.rsplit(".", 1)
                mod = importlib.import_module(mod_path)
                fn = getattr(mod, func_name, None)
                if not callable(fn):
                    raise RuntimeError(f"Normalizer '{path}' is not callable.")
                updated = fn(result_dict, pending, self.blackboard)
                if isinstance(updated, dict):
                    result_dict = updated
            except Exception as e:
                logger.error(f"[{self.name}] Argument normalizer failed ({path}): {e}")
                raise

        return result_dict