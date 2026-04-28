"""
Control node that runs the tool planner agent on task spec steps.

When the spec has plain English steps, the planner agent analyzes
all steps and recommends tools/executors. The recommendations are
injected into the blackboard for the spec writer to apply.

Sits between task_create (chat agent) and task_spec_writer in the
task creation pipeline.
"""
from __future__ import annotations

import re
from typing import Any

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_TOOL_PLANNER_AGENT = "master_room::tool_planner"


class TaskToolPlannerNode(ControlNode):
    """
    Read the current task spec, extract plain English steps, invoke
    the tool planner agent, and inject recommendations for the spec writer.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        chat_response = str(self.blackboard.get_state_value("chat_response", "") or "").strip()
        latest_exchange = str(self.blackboard.get_state_value("latest_exchange", "") or "").strip()
        current_spec = str(self.blackboard.get_state_value("spec", "") or "").strip()

        if not current_spec:
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Extract plain English steps from the current spec.
        steps = self._extract_spec_steps(current_spec)
        if not steps:
            logger.debug("[%s] No steps found in current spec.", self.name)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Check if steps already have tool details (already planned).
        if self._steps_already_planned(current_spec):
            logger.debug("[%s] Steps already have tool details, skipping planner.", self.name)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Check if there are step-related changes in the latest exchange.
        combined = f"{latest_exchange}\n{chat_response}".lower()
        has_step_content = any(signal in combined for signal in [
            "step", "fetch", "get", "capture", "send", "write", "read",
            "navigate", "search", "email", "todo", "screenshot", "notify",
            "summarize", "combine", "report",
        ])

        if not has_step_content:
            logger.debug("[%s] No step-related content in exchange.", self.name)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Build the tool catalog and step descriptions for the agent.
        try:
            from app.assistant.lib.task_utils.tool_planner import build_tool_catalog_for_prompt
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            catalog_text = build_tool_catalog_for_prompt()

            # Format steps as numbered list for the agent.
            steps_text = "\n".join(
                f"{i}. **{name}**: {desc}"
                for i, (name, desc) in enumerate(steps, start=1)
            )

            logger.info(
                "[%s] Running tool planner agent on %d step(s).",
                self.name, len(steps),
            )

            # Invoke the agent with catalog injected via blackboard.
            agent = DI.agent_factory.create_agent(_TOOL_PLANNER_AGENT)
            agent.blackboard.update_state_value("agent_input_catalog", catalog_text)
            result = agent.action_handler(Message(agent_input=steps_text))

            data = result.data or {}
            step_plans = data.get("step_plans") or []

            if step_plans:
                formatted = self._format_recommendations(steps, step_plans)
                self.blackboard.update_state_value("tool_planner_recommendations", formatted)
                logger.info(
                    "[%s] Tool planner agent produced %d plan(s).",
                    self.name, len(step_plans),
                )
            else:
                logger.info("[%s] Tool planner agent returned no plans.", self.name)

        except Exception as e:
            logger.error("[%s] Tool planner failed: %s", self.name, e)
            logger.debug("[%s] Tool planner exception details", self.name, exc_info=True)

        self.blackboard.update_state_value("last_agent", self.name)

    def _extract_spec_steps(self, spec: str) -> list[tuple[str, str]]:
        """Extract step names and descriptions from the spec markdown."""
        steps: list[tuple[str, str]] = []
        pattern = re.compile(
            r'^\s*\d+\.\s*\*\*(.+?)\*\*[^*\n]*\n((?:(?!\n\d+\.\s*\*\*|\n##).*\n?)*)',
            re.MULTILINE,
        )
        for match in pattern.finditer(spec):
            name = match.group(1).strip()
            body = match.group(2).strip()
            if "(deterministic)" in name or "→" in name or "->" in name:
                continue
            if body and len(body) > 10:
                steps.append((name, f"{name}: {body}"))
            elif name and len(name) > 5:
                steps.append((name, name))
        return steps

    def _steps_already_planned(self, spec: str) -> bool:
        """Check if steps already have tool/executor annotations."""
        return "(deterministic)" in spec or "→" in spec or "-> " in spec

    def _format_recommendations(
        self,
        original_steps: list[tuple[str, str]],
        step_plans: list[dict[str, Any]],
    ) -> str:
        """Produce ready-to-insert spec step text from agent output."""
        parts: list[str] = []
        parts.append("INSERT THE FOLLOWING STEPS INTO THE SPEC (replace the existing ## Steps section):\n")

        for step_num, plan in enumerate(step_plans, start=1):
            if not isinstance(plan, dict):
                continue
            step_name = str(plan.get("step_name") or f"Step {step_num}").strip()
            recommendation = str(plan.get("recommendation") or "manager").strip()
            produces_desc = str(plan.get("produces_description") or "step output").strip()

            if recommendation == "deterministic":
                tools = plan.get("tools") or []
                parts.append(f"{step_num}. **{step_name}** (deterministic)")
                parts.append(f"   - tools:")
                for t in tools:
                    if not isinstance(t, dict):
                        continue
                    tool_name = str(t.get("tool") or "?").strip()
                    args = str(t.get("args_json") or t.get("args") or "{}").strip()
                    parts.append(f"     - {tool_name}({args})")
                parts.append(f"   - produces: artifact_{step_num} ({produces_desc})")
            else:
                manager = str(plan.get("manager_name") or "emi_team_manager").strip()
                parts.append(f"{step_num}. **{step_name}** → {manager}")
                reasoning = str(plan.get("reasoning") or "").strip()
                if reasoning:
                    parts.append(f"   {reasoning[:150]}")
                parts.append(f"   - produces: artifact_{step_num} ({produces_desc})")

            if step_num > 1:
                prior_artifacts = ", ".join(f"artifact_{i}" for i in range(1, step_num))
                parts.append(f"   - consumes: {prior_artifacts}")

            parts.append("")

        return "\n".join(parts)
