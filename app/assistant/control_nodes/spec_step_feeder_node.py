"""
Spec Step Feeder — feeds one goal at a time from a learned task spec.

Sits before the planner in the playwright manager pipeline. Parses
the task spec into broad goals with guidance, injects only the current
goal into the planner's context, and advances when the planner sets
step_done=true.

When no structured steps are found (first run, vague spec), passes
the full task through unchanged so the planner explores freely.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_STEP_PATTERN = re.compile(
    r"^step\s+(\d+):\s*(.+?)$",
    re.IGNORECASE | re.MULTILINE,
)


class SpecStepFeederNode(ControlNode):
    """Parse spec into goals, feed one at a time to the planner."""

    _BB_KEY_STEPS = "_spec_steps"
    _BB_KEY_UNEXPECTED = "_spec_unexpected"
    _BB_KEY_CURRENT = "_spec_current_step"
    _BB_KEY_ORIGINAL_TASK = "_spec_original_task"
    _BB_KEY_GOAL_LINE = "_spec_goal_line"
    _BB_KEY_CONTEXT = "_spec_context_sections"
    _BB_KEY_INITIALIZED = "_spec_feeder_initialized"

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        if not self.blackboard.get_state_value(self._BB_KEY_INITIALIZED):
            self._initialize()

        steps = self.blackboard.get_state_value(self._BB_KEY_STEPS)
        if not steps:
            # No structured steps — pass full task through, planner explores freely.
            self.blackboard.update_state_value("has_spec_steps", False)
            self.blackboard.update_state_value("last_agent", self.name)
            return
        self.blackboard.update_state_value("has_spec_steps", True)

        current_idx = self.blackboard.get_state_value(self._BB_KEY_CURRENT, 0)

        # Check if planner used action=step_complete to signal goal done.
        last_action = str(self.blackboard.get_state_value("action") or "").strip()
        if last_action == "step_complete" and current_idx < len(steps):
            current_idx += 1
            self.blackboard.update_state_value(self._BB_KEY_CURRENT, current_idx)
            self.blackboard.update_state_value("action", "")
            logger.info("[%s] Goal %d complete, advancing to goal %d/%d",
                        self.name, current_idx, current_idx + 1, len(steps))

            # Skip transition work if this was the final step — we're about to exit.
            if current_idx < len(steps):
                self._prepare_next_step()

        if current_idx >= len(steps):
            # All goals complete.
            logger.info("[%s] All %d goals complete.", self.name, len(steps))
            self.blackboard.update_state_value("result", {
                "final_answer_task": str(self.blackboard.get_state_value(self._BB_KEY_GOAL_LINE, "") or "")[:200],
                "final_answer_answer": f"Completed all {len(steps)} goals successfully.",
            })
            self.blackboard.update_state_value("exit", True)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Build the prompt for the current goal.
        step = steps[current_idx]
        goal_line = self.blackboard.get_state_value(self._BB_KEY_GOAL_LINE, "")
        unexpected = self.blackboard.get_state_value(self._BB_KEY_UNEXPECTED, "")

        prompt = f"CURRENT GOAL — {current_idx + 1} of {len(steps)}: {step['description']}\n"
        prompt += f"Done when: {step['after']}\n"

        if step.get("guidance"):
            prompt += f"\nGuidance:\n{step['guidance']}\n"

        context_sections = self.blackboard.get_state_value(self._BB_KEY_CONTEXT, "")
        if context_sections:
            prompt += f"\n{context_sections}\n"

        if unexpected:
            prompt += f"\n{unexpected}\n"

        prompt += f"\nOverall task: {goal_line}\n"
        is_last_step = (current_idx == len(steps) - 1)
        if is_last_step:
            prompt += "\nThis is the final step. When done, use action=step_complete then return_control on the next turn."
        else:
            prompt += f"\nYou are on step {current_idx + 1} of {len(steps)}. return_control is NOT available."
            prompt += "\nWhen the 'Done when' state matches the snapshot, use action=step_complete (no other action on that turn)."

        self.blackboard.update_state_value("task", prompt)
        self.blackboard.update_state_value("last_agent", self.name)
        logger.info("[%s] Feeding goal %d/%d: %s",
                    self.name, current_idx + 1, len(steps), step["description"][:60])

    def _prepare_next_step(self):
        """Clear stale state and take a fresh snapshot for the next step."""
        # Clear state from previous step — stale refs, old history, modal map.
        self.blackboard.update_state_value("playwright_modal_map", None)
        self.blackboard.update_state_value("checklist", None)
        # Clear history so the planner starts fresh without stale refs and
        # irrelevant clicks from the previous step.
        try:
            scope_id = self.blackboard.get_current_scope_id()
            if scope_id:
                msgs = list(self.blackboard.get_messages_for_scope(scope_id) or [])
                for m in msgs:
                    meta = getattr(m, "metadata", None)
                    if not isinstance(meta, dict):
                        meta = {}
                    meta["history_hidden"] = True
                    m.metadata = meta
                logger.debug("[%s] Hid %d history messages from previous step.", self.name, len(msgs))
        except Exception:
            logger.debug("[%s] Failed to clear step history", self.name, exc_info=True)
        # Brief settle so any cart/confirmation animation clears, then
        # take a fresh snapshot so the planner sees current page state.
        import time
        time.sleep(3)
        try:
            from app.assistant.lib.mcp.tool_runner import mcp_stdio_call_tool, format_mcp_tool_result_content
            from app.assistant.lib.tools.playwright_snapshot_utils import summarize_actionable_snapshot, make_snapshot_id
            server_entry = self.tool_registry.get_mcp_server_entry("npm/playwright-mcp")
            if isinstance(server_entry, dict):
                snap_resp = mcp_stdio_call_tool(
                    server_entry=server_entry,
                    tool_name="browser_snapshot",
                    arguments={},
                    timeout_s=15,
                )
                snap_text, snap_err, _ = format_mcp_tool_result_content(snap_resp)
                if not snap_err and isinstance(snap_text, str):
                    summary = summarize_actionable_snapshot(snap_text, max_elements=200)
                    snapshot_id = make_snapshot_id("step_transition")
                    snapshot_card = {
                        "snapshot_id": snapshot_id,
                        "url": summary.get("url"),
                        "title": summary.get("title"),
                        "actionable_elements": summary.get("actionable_elements") or [],
                        "actionable_total": summary.get("actionable_total", 0),
                    }
                    self.blackboard.update_state_value("playwright_latest_snapshot", snapshot_card)
                    self.blackboard.update_state_value("playwright_latest_snapshot_id", snapshot_id)
                    from app.assistant.control_nodes.tool_result_handler import ToolResultHandler
                    self.blackboard.update_state_value(
                        "playwright_latest_snapshot_summary",
                        ToolResultHandler._format_snapshot_card(snapshot_card),
                    )
                    logger.info("[%s] Took fresh snapshot after step transition.", self.name)
        except Exception:
            logger.debug("[%s] Failed to take step-transition snapshot", self.name, exc_info=True)

    def _initialize(self):
        """Parse the spec into goals on first call."""
        task = str(self.blackboard.get_state_value("task") or "")
        self.blackboard.update_state_value(self._BB_KEY_ORIGINAL_TASK, task)

        # Extract overall goal from markdown body.
        goal_line = self._extract_goal(task)
        self.blackboard.update_state_value(self._BB_KEY_GOAL_LINE, goal_line)

        # Parse steps, unexpected situations, and context sections.
        steps = self._parse_steps(task)
        unexpected = self._extract_unexpected(task)
        context_sections = self._extract_context_sections(task)

        self.blackboard.update_state_value(self._BB_KEY_STEPS, steps)
        self.blackboard.update_state_value(self._BB_KEY_UNEXPECTED, unexpected)
        self.blackboard.update_state_value(self._BB_KEY_CONTEXT, context_sections)
        self.blackboard.update_state_value(self._BB_KEY_CURRENT, 0)
        self.blackboard.update_state_value(self._BB_KEY_INITIALIZED, True)

        if steps:
            logger.info("[%s] Parsed %d goals from spec.", self.name, len(steps))
        else:
            logger.info("[%s] No structured goals found — planner will explore freely.", self.name)

    def _extract_goal(self, spec_text: str) -> str:
        """Extract the overall goal from the markdown body (after YAML frontmatter)."""
        body = spec_text
        if "---" in spec_text:
            parts = spec_text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2]
        for line in body.split("\n"):
            stripped = line.strip().lstrip("#").strip()
            if stripped.lower().startswith("goal"):
                continue  # Skip the "# Goal" header itself
            if stripped and len(stripped) > 10:
                return stripped
        return ""

    def _parse_steps(self, spec_text: str) -> List[Dict[str, str]]:
        """Extract goal-based steps from the spec."""
        steps = []
        lines = spec_text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            step_match = _STEP_PATTERN.match(line)
            if step_match:
                step_num = int(step_match.group(1))
                description = step_match.group(2).strip()
                after = ""
                guidance_lines = []
                in_guidance = False

                # Scan indented lines for after: and guidance: blocks.
                j = i + 1
                while j < len(lines):
                    stripped = lines[j].strip()
                    # Stop at next step, next section header, or unexpected situations
                    if _STEP_PATTERN.match(lines[j]) or lines[j].startswith("#"):
                        break
                    if stripped.lower().startswith("after:"):
                        after = stripped[6:].strip()
                        in_guidance = False
                    elif stripped.lower().startswith("guidance:"):
                        in_guidance = True
                    elif in_guidance and stripped.startswith("- "):
                        guidance_lines.append(stripped)
                    elif not stripped:
                        pass  # blank line
                    j += 1

                steps.append({
                    "step_num": step_num,
                    "description": description,
                    "after": after,
                    "guidance": "\n".join(guidance_lines) if guidance_lines else "",
                })
                i = j
            else:
                i += 1

        return steps

    def _extract_unexpected(self, spec_text: str) -> str:
        """Extract the 'Unexpected situations' section as a block of text."""
        lines = spec_text.split("\n")
        capture = False
        result = []
        for line in lines:
            if "unexpected situation" in line.lower() and "#" in line:
                capture = True
                result.append(line.strip())
                continue
            if capture:
                if line.startswith("#") and "unexpected" not in line.lower():
                    break  # Next section
                if line.strip():
                    result.append(line.strip())
        return "\n".join(result) if result else ""

    def _extract_context_sections(self, spec_text: str) -> str:
        """Extract knowledge sections (e.g., Customization structure, Ordering flexibility).

        Returns all # sections that are NOT Goal, Steps, Unexpected situations,
        or YAML frontmatter. These are contextual knowledge the planner needs.
        """
        body = spec_text
        if "---" in spec_text:
            parts = spec_text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2]

        skip_headers = {"goal", "steps", "unexpected situations"}
        lines = body.split("\n")
        sections: list[str] = []
        capture = False
        current: list[str] = []

        for line in lines:
            if line.startswith("#"):
                header = line.lstrip("#").strip().lower()
                # Save previous section if we were capturing
                if capture and current:
                    sections.append("\n".join(current))
                    current = []
                # Decide whether to capture this section
                if any(header.startswith(s) for s in skip_headers):
                    capture = False
                elif _STEP_PATTERN.match(line):
                    capture = False
                else:
                    capture = True
                    current.append(line.strip())
                continue
            if capture:
                current.append(line.rstrip())

        if capture and current:
            sections.append("\n".join(current))

        return "\n\n".join(s.strip() for s in sections if s.strip())
