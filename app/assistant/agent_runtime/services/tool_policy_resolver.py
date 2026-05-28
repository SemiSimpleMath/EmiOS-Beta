"""Tool policy resolver — determines which tools an agent can see and use.

Extracted from Agent.py to keep the base class focused on LLM interaction.
All tool filtering logic lives here: config-based allow/except, task-level
restrictions, dynamic allow/deny, and visibility filtering.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from app.assistant.utils.logging_config import get_logger

if TYPE_CHECKING:
    from app.assistant.agent_registry.agent_registry import AgentRegistry

logger = get_logger(__name__)


class ToolPolicyResolver:
    """Resolves the set of tools an agent is allowed to use and see.

    Reads from:
    - agent_registry (static config: allowed_tools, except_tools)
    - tool_registry (available tools in the system)
    - blackboard (dynamic runtime overrides: task_allowed_tools,
      task_except_tools, dynamic_allowed_tools, dynamic_denied_tools,
      visible_tools)
    """

    def __init__(self, agent_name: str, agent_registry: "AgentRegistry", tool_registry, blackboard):
        self._name = agent_name
        self._agent_registry = agent_registry
        self._tool_registry = tool_registry
        self._blackboard = blackboard

    def get_tools(self) -> List[str]:
        """Return the full list of tools this agent is allowed to call."""
        agent_config = self._agent_registry.get_agent_config(self._name)
        if not agent_config:
            logger.warning("[%s] Agent not found in registry.", self._name)
            return []

        # --- Static config: allowed_tools / except_tools ---
        allowed_tools_raw = agent_config.get("allowed_tools", [])
        allow_all = False
        if isinstance(allowed_tools_raw, str):
            allow_all = allowed_tools_raw.strip() == "all"
            allowed_tools = {allowed_tools_raw.strip()} if allowed_tools_raw.strip() else set()
        elif isinstance(allowed_tools_raw, list):
            allowed_tools = {str(x).strip() for x in allowed_tools_raw if isinstance(x, str) and str(x).strip()}
            allow_all = "all" in allowed_tools
        else:
            allowed_tools = set()

        if allow_all:
            allowed_tools = set(self._tool_registry.list_tools())
        except_tools = set(agent_config.get("except_tools", []))
        all_available_tools = set(self._tool_registry.list_tools())
        valid_tools = [tool for tool in allowed_tools if tool in all_available_tools and tool not in except_tools]
        missing_tools = allowed_tools - all_available_tools
        if missing_tools:
            logger.warning("[%s] References unavailable tools: %s", self._name, missing_tools)

        # Manager's always_show list. Tools in always_show bypass the
        # task-level restrictions below — this matches the bypass that
        # tool_scope_service.initialize_scope already applies for scope
        # contract filtering, so both filter paths agree.
        # See SCOPE_AUDIT.md Step 1.5: without this, a tool declared in
        # the manager's tool_visibility.always_show survives the scope
        # filter (tool_scope_service path) but gets stripped here when
        # task_allowed_tools is narrower — the http_request bug.
        manager_always_show = self._bb_get("manager_always_show")
        always_show_set: set[str] = set()
        if isinstance(manager_always_show, list):
            always_show_set = {
                str(x).strip()
                for x in manager_always_show
                if isinstance(x, str) and str(x).strip()
            }

        # --- Task-level restrictions (further restrict capabilities) ---
        task_allowed = self._bb_get("task_allowed_tools")
        task_except = self._bb_get("task_except_tools")

        if isinstance(task_allowed, list):
            allowset = {str(x).strip() for x in task_allowed if isinstance(x, str) and x.strip()}
            if "all" in allowset and len(allowset) > 1:
                raise ValueError(f"[{self._name}] task_allowed_tools cannot mix 'all' with specific tool names.")
            # Manager-scoped dynamic allows can expand task-level allowset.
            dyn_allowed = self._bb_get("dynamic_allowed_tools")
            if isinstance(dyn_allowed, list) and dyn_allowed and "all" not in allowset:
                allowset.update({str(x).strip() for x in dyn_allowed if isinstance(x, str) and x.strip()})
            if "all" not in allowset:
                valid_tools = [t for t in valid_tools if t in allowset or t in always_show_set]

        if isinstance(task_except, list) and task_except:
            denyset = {str(x).strip() for x in task_except if isinstance(x, str) and x.strip()}
            if denyset:
                valid_tools = [t for t in valid_tools if t not in denyset or t in always_show_set]

        # Dynamic deny list can tighten policy at runtime.
        dyn_denied = self._bb_get("dynamic_denied_tools")
        if isinstance(dyn_denied, list) and dyn_denied:
            denyset = {str(x).strip() for x in dyn_denied if isinstance(x, str) and x.strip()}
            if denyset:
                valid_tools = [t for t in valid_tools if t not in denyset or t in always_show_set]

        # If task-level allowset was not provided, include dynamic allowed tools.
        if not isinstance(task_allowed, list):
            dyn_allowed = self._bb_get("dynamic_allowed_tools")
            if isinstance(dyn_allowed, list) and dyn_allowed:
                for t in dyn_allowed:
                    if not isinstance(t, str) or not t.strip():
                        continue
                    if t in all_available_tools and t not in valid_tools:
                        valid_tools.append(t)

        logger.debug("[%s] Final allowed tools: %s", self._name, valid_tools)
        return valid_tools

    def get_visible_tools(self) -> List[str]:
        """Return the subset of allowed tools that should be shown in prompts."""
        allowed = self.get_tools()
        visible_raw = self._bb_get("visible_tools")
        if not isinstance(visible_raw, list) or not visible_raw:
            return allowed
        allowed_set = set(allowed)
        visible = [t for t in visible_raw if isinstance(t, str) and t in allowed_set]
        return visible if visible else allowed

    def get_tool_descriptions(self) -> Dict[str, str]:
        """Return tool descriptions for visible tools."""
        visible_tools = self.get_visible_tools()
        return self._tool_registry.get_tool_descriptions(visible_tools)

    def get_tool_arguments_prompt(self) -> Dict[str, str]:
        """Return tool argument prompts for visible tools."""
        visible_tools = self.get_visible_tools()
        return self._tool_registry.get_tool_arguments_prompts(visible_tools)

    # ------------------------------------------------------------------
    # Node policy (agent-to-agent delegation)
    # ------------------------------------------------------------------

    def get_allowed_nodes(self) -> List[str]:
        """Return the list of agent nodes this agent can delegate to."""
        agent_config = self._agent_registry.get_agent_config(self._name)
        if not agent_config:
            logger.warning("[%s] Agent not found in registry.", self._name)
            return []

        intrinsic_actions = {"return_control", "done"}
        allowed_nodes = agent_config.get("allowed_nodes", [])
        if allowed_nodes == "all":
            allowed_nodes = set(self._agent_registry.list_agents()) - intrinsic_actions
        else:
            allowed_nodes = {
                str(n).strip()
                for n in allowed_nodes
                if isinstance(n, str) and str(n).strip() and str(n).strip() not in intrinsic_actions
            }

        except_nodes = set(agent_config.get("except_nodes", []))
        all_nodes = set(self._agent_registry.list_agents())
        instantiated_nodes = set(self._agent_registry.list_instantiated_agents())
        enforce_instantiated = len(instantiated_nodes) > 0

        valid_nodes = [
            a
            for a in allowed_nodes
            if self._agent_registry.is_callable_agent(a) and a not in except_nodes
        ]

        missing_configs = allowed_nodes - all_nodes
        if missing_configs:
            logger.warning("[%s] References unavailable agents (no config): %s", self._name, missing_configs)

        if enforce_instantiated:
            not_instantiated = (allowed_nodes & all_nodes) - instantiated_nodes
            if not_instantiated:
                logger.warning(
                    "[%s] References agents not instantiated in this runtime: %s",
                    self._name, not_instantiated,
                )

        logger.debug("[%s] Final allowed nodes: %s", self._name, valid_nodes)
        return valid_nodes

    # ------------------------------------------------------------------
    # Combined action policy
    # ------------------------------------------------------------------

    def get_allowed_actions(self) -> set:
        """Return the combined set of allowed tools + nodes + intrinsic actions."""
        allowed_actions = {"return_control", "done"}
        allowed_actions.update(str(x).strip() for x in (self.get_tools() or []) if isinstance(x, str) and str(x).strip())
        allowed_actions.update(str(x).strip() for x in (self.get_allowed_nodes() or []) if isinstance(x, str) and str(x).strip())
        return allowed_actions

    def is_tool_action(self, action_name: str) -> bool:
        """Check if an action name refers to a tool (vs a node)."""
        if not isinstance(action_name, str) or not action_name.strip():
            return False
        return action_name in (self.get_tools() or [])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _bb_get(self, key: str) -> Any:
        """Read from blackboard with safe fallback."""
        try:
            return self._blackboard.get_state_value(key)
        except Exception:
            logger.debug("[%s] Could not read %s from blackboard", self._name, key, exc_info=True)
            return None
