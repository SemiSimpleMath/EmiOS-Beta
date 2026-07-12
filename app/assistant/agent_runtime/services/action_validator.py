from typing import Any, Callable

from app.assistant.lib.tool_registry.mcp_install_registry import list_installed_records
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ActionValidator:
    """
    Unified action/action_input validator for Agent-family classes.
    """

    @staticmethod
    def _has_meaningful_action_input(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) > 0
        return True

    @staticmethod
    def _resolve_namespaced_mcp_action(action: str, allowed_actions: set[str]) -> str:
        """
        Normalize bare MCP tool names to full namespaced action when unambiguous.

        Example:
          action = "maps_distance_matrix"
          allowed contains "mcp::npm/server-google-maps::maps_distance_matrix"
          -> returns namespaced action above
        """
        raw = (action or "").strip()
        if not raw:
            return raw
        if "::" in raw:
            return raw

        candidates = [
            a for a in (allowed_actions or set())
            if isinstance(a, str) and a.startswith("mcp::") and a.endswith(f"::{raw}")
        ]
        if not candidates:
            # If manager policy allows install_tool, permit immediate use of installed
            # MCP tools even if the dynamic allow list has not been merged yet.
            if "install_tool" in (allowed_actions or set()):
                try:
                    installed = list_installed_records(enabled_only=True)
                    installed_candidates = []
                    for rec in installed:
                        n = str(getattr(rec, "namespaced_tool_name", "") or "").strip()
                        if n and n.startswith("mcp::") and n.endswith(f"::{raw}"):
                            installed_candidates.append(n)
                    if len(installed_candidates) == 1:
                        allowed_actions.add(installed_candidates[0])
                        return installed_candidates[0]
                    if len(installed_candidates) > 1:
                        raise ValueError(
                            "Ambiguous installed MCP action short name "
                            f"'{raw}'. Candidates: {sorted(installed_candidates)}. "
                            "Use full namespaced action."
                        )
                except Exception:
                    # Preserve loud failure path below if not resolvable.
                    pass
            return raw
        if len(candidates) == 1:
            return candidates[0]

        # Fail loudly on ambiguous short names.
        raise ValueError(
            "Ambiguous MCP action short name "
            f"'{raw}'. Candidates: {sorted(candidates)}. Use full namespaced action."
        )

    def validate(
        self,
        *,
        agent_name: str,
        result_dict: dict,
        allowed_actions: set[str],
        list_action_validator: Callable[[dict, list, set[str]], None],
    ) -> None:
        if not isinstance(result_dict, dict):
            logger.error("[%s] Result must be dict for action validation.", agent_name)
            raise TypeError(f"[{agent_name}] Result must be dict for action validation.")

        action = result_dict.get("action")

        if isinstance(action, list):
            list_action_validator(result_dict, action, allowed_actions)
            return

        if not isinstance(action, str) or not action.strip():
            logger.error("[%s] Invalid action: expected non-empty string, got %r", agent_name, action)
            raise ValueError(f"[{agent_name}] Invalid action: expected non-empty string.")

        action = action.strip()
        action = self._resolve_namespaced_mcp_action(action, allowed_actions)
        result_dict["action"] = action

        # Planner control sentinels (see Planner._skip_actions): flow signals consumed
        # by the agent class itself — never tools/nodes, never in the tool policy's
        # allowed set. Since enforce() validates every EMITTED action, these must pass
        # here or a planner's step completion raises on the valid sentinel it was
        # instructed to emit ("step_complete" routes to the spec step feeder).
        if action in {"step_complete", "none"}:
            return

        if action not in allowed_actions:
            logger.error(
                "[%s] Invalid action '%s'. Allowed actions count=%s",
                agent_name,
                action,
                len(allowed_actions),
            )
            logger.debug("[%s] Allowed actions: %s", agent_name, sorted(allowed_actions))
            raise ValueError(f"[{agent_name}] Invalid action '{action}' (not allowed tool/node/exit action).")

        if action in {"return_control", "done"}:
            return

        action_input = result_dict.get("action_input")
        if not self._has_meaningful_action_input(action_input):
            logger.error(
                "[%s] Action '%s' requires non-empty action_input. Got: %r",
                agent_name,
                action,
                action_input,
            )
            raise ValueError(f"[{agent_name}] Action '{action}' requires non-empty action_input.")

