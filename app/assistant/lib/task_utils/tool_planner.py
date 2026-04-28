"""
Tool planner for task spec creation.

Given a natural language description of what a step should accomplish,
queries the tool registry and recommends either:
- A deterministic tool sequence (fixed tool calls, no LLM planning)
- A manager delegation (open-ended work requiring LLM judgment)

This bridges the gap between the user's natural language intent and
the spec writer's need for concrete tool names and arguments.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Manager descriptions for non-deterministic delegation.
_MANAGER_CAPABILITIES = {
    "personal_admin_manager": "Email workflows, calendar ops, todo tasks, Google Docs. Use when the step involves reading/sending email, managing calendar, or multi-step admin work.",
    "web_manager": "Web search and research. Use for open-ended research, multi-query synthesis, or when search terms are dynamic.",
    "playwright_manager": "Real browser automation. Use when the step involves visiting a website, taking screenshots, reading page content, extracting headlines or data from web pages, login, form filling, or any multi-page navigation.",
    "devices_manager": "Smart home control (Nest thermostat, lights, Ring). Use for any home automation.",
    "emi_team_manager": "General-purpose fallback. Use for file transformations, multi-skill synthesis, or when no specialized manager fits.",
}

# Tools to skip when building the catalog (internal/system tools).
_SKIP_TOOLS = frozenset({
    "find_tool", "install_tool", "run_task",
    "task_compile_manager", "create_dayflow_ticket", "one_shot_tool_runner",
    "set_ui_mute", "set_screen_capture_enabled", "move_foreground_window",
})


# --- Pydantic response model ---

class PlannedToolCall(BaseModel):
    tool: str = Field(description="Tool name from the catalog.")
    args_json: str = Field(
        default="{}",
        description='Arguments as a JSON object string, e.g. {"url": "https://www.cnn.com"}. Use concrete values when known.',
    )
    purpose: str = Field(
        default="",
        description="One-line description of what this tool call accomplishes.",
    )


class ToolPlanResult(BaseModel):
    recommendation: Literal["deterministic", "manager"] = Field(
        description="'deterministic' for fixed tool sequence, 'manager' for open-ended LLM work.",
    )
    manager_name: str = Field(
        default="",
        description="Manager name when recommendation is 'manager'. Empty for deterministic.",
    )
    tools: List[PlannedToolCall] = Field(
        default_factory=list,
        description="Ordered tool calls when recommendation is 'deterministic'. Empty for manager.",
    )
    reasoning: str = Field(
        default="",
        description="Brief explanation of why this approach was chosen.",
    )
    produces_description: str = Field(
        default="",
        description="One-line description of what this step produces (for the artifact declaration).",
    )


# --- Tool narrowing ---

def narrow_tools_for_task(
    *,
    task_title: str,
    task_goal: str,
    step_descriptions: list[str],
) -> list[str] | None:
    """Use shared::tool_narrower to filter tools to those relevant for a task.

    Combines the task title, goal, and all step descriptions into a single
    query so the narrower can pick tools across the whole task at once.

    Returns a list of tool names to include in the catalog, or None if
    narrowing fails (caller should fall back to the full catalog).
    """
    from app.assistant.ServiceLocator.service_locator import DI

    registry = DI.tool_registry
    all_tool_names = sorted(registry.list_tools())

    # Build candidate list: skip system tools and managers (same as catalog builder).
    candidates: list[dict[str, str]] = []
    for tool_name in all_tool_names:
        if tool_name in _SKIP_TOOLS:
            continue
        # Include managers in candidate list — narrower should see them too.
        desc_obj = registry.get_tool_description_compact(tool_name)
        description = ""
        if desc_obj is not None:
            description = str(getattr(desc_obj, "description", "") or "").strip()
            if not description:
                description = str(desc_obj).strip()[:150]
        candidates.append({"name": tool_name, "description": description[:200]})

    if not candidates:
        return None

    # Build the query from task context + all step descriptions.
    task_context = f"{task_title}: {task_goal}"
    steps_text = "\n".join(f"- {s}" for s in step_descriptions if s)
    full_query = f"{task_context}\n\nSteps:\n{steps_text}"

    # Deterministic pre-filter by tool metadata (domain + actions). Cheap, no LLM.
    from app.assistant.lib.task_utils.tool_domain_filter import filter_candidates
    pre_filtered, trace = filter_candidates(
        task_text=full_query,
        candidate_tools=candidates,
        tool_registry=registry,
    )
    logger.info(
        "tool_domain_filter: %d → %d (domains=%s actions=%s)",
        len(candidates), len(pre_filtered),
        trace.get("inferred_domains"), trace.get("inferred_actions"),
    )
    # If deterministic filter narrowed to a tight set, skip the LLM narrower.
    if (
        trace.get("inferred_domains")
        and len(pre_filtered) <= 10
        and trace.get("unmigrated_count", 0) == 0
    ):
        narrowed_names = [c["name"] for c in pre_filtered]
        logger.info(
            "tool_domain_filter: short-circuit — skipping LLM narrower. Tools: %s",
            narrowed_names,
        )
        return narrowed_names or None

    # Otherwise, pass the filtered candidate set to the LLM narrower.
    candidates = pre_filtered

    try:
        import json as _json
        from app.assistant.utils.pydantic_classes import Message

        candidate_payload = _json.dumps(candidates, ensure_ascii=False)

        narrower = DI.agent_factory.create_agent("shared::tool_narrower")
        result_msg = narrower.action_handler(
            Message(
                agent_input={
                    "task": full_query,
                    "information": "This is for task spec planning. Include BOTH leaf tools (for deterministic steps) AND managers (for open-ended steps). Do not prefer managers over leaf tools — include all relevant tools.",
                    "candidate_tools": candidate_payload,
                }
            )
        )
        result_data = getattr(result_msg, "data", None)
        if not isinstance(result_data, dict):
            logger.warning("Tool narrower returned invalid data type: %s", type(result_data).__name__)
            return None

        likely_raw = result_data.get("likely_tools")
        if not isinstance(likely_raw, list):
            logger.warning("Tool narrower missing likely_tools field.")
            return None

        candidate_names = {c["name"] for c in candidates}
        narrowed = [
            str(t).strip() for t in likely_raw
            if isinstance(t, str) and str(t).strip() in candidate_names
        ]

        reason = str(result_data.get("reason") or "").strip()
        logger.info(
            "Tool narrower: %d → %d tools. Reason: %s",
            len(candidates), len(narrowed), reason[:120],
        )
        logger.debug("Tool narrower result: %s", narrowed)

        return narrowed if narrowed else None

    except Exception as e:
        logger.error("Tool narrower failed: %s", e)
        logger.debug("Tool narrower exception details", exc_info=True)
        return None


# --- Catalog builder ---

def build_tool_catalog(only_tools: list[str] | None = None) -> dict[str, Any]:
    """Build a condensed catalog of available tools from the registry.

    Args:
        only_tools: If provided, only include these tool names (narrowed set).
                    Managers are always included regardless.
    """
    registry = DI.tool_registry
    all_tool_names = registry.list_tools()
    only_set = set(only_tools) if only_tools else None

    tools: list[dict[str, str]] = []

    for tool_name in sorted(all_tool_names):
        if tool_name.endswith("_manager"):
            # Managers go in the managers section, not tools.
            continue
        if tool_name in _SKIP_TOOLS:
            continue
        if only_set is not None and tool_name not in only_set:
            continue

        desc_obj = registry.get_tool_description_compact(tool_name)
        description = ""
        if desc_obj is not None:
            description = str(getattr(desc_obj, "description", "") or "").strip()
            if not description:
                description = str(desc_obj).strip()[:150]

        contract = registry.get_tool_contract(tool_name)
        args_hint = ""
        usage_notes = ""
        if isinstance(contract, dict):
            inputs = contract.get("inputs")
            if isinstance(inputs, list):
                arg_parts = []
                for inp in inputs:
                    if isinstance(inp, dict):
                        name = str(inp.get("name") or "").strip()
                        typ = str(inp.get("type") or "").strip()
                        required = bool(inp.get("required", False))
                        desc = str(inp.get("description") or "").strip()[:80]
                        if name:
                            marker = " *required*" if required else ""
                            entry = f"{name} ({typ}{marker})"
                            if desc:
                                entry += f": {desc}"
                            arg_parts.append(entry)
                if arg_parts:
                    args_hint = "; ".join(arg_parts)

            # Include usage notes from arguments_prompt when catalog is narrowed
            # (affordable context since we have fewer tools).
            if only_set is not None:
                raw_notes = str(contract.get("arguments_prompt") or "").strip()
                if raw_notes:
                    usage_notes = raw_notes[:400]

        entry = {
            "name": tool_name,
            "description": description[:200],
            "arguments": args_hint[:300],
        }
        if usage_notes:
            entry["usage_notes"] = usage_notes
        tools.append(entry)

    managers = [
        {"name": name, "purpose": purpose}
        for name, purpose in _MANAGER_CAPABILITIES.items()
    ]

    return {"tools": tools, "managers": managers}


def build_tool_catalog_for_prompt(
    catalog: dict[str, Any] | None = None,
    only_tools: list[str] | None = None,
) -> str:
    """Build a prompt-ready string summarizing available tools and managers.

    Args:
        catalog: Pre-built catalog dict. If None, built from registry.
        only_tools: If provided, only include these tools (narrowed set).
    """
    if catalog is None:
        catalog = build_tool_catalog(only_tools=only_tools)

    parts: list[str] = []
    parts.append("## Available Tools (for deterministic steps)\n")

    for t in catalog["tools"]:
        line = f"- **{t['name']}**: {t['description']}"
        if t["arguments"]:
            line += f"\n  Arguments: {t['arguments']}"
        if t.get("usage_notes"):
            line += f"\n  Notes: {t['usage_notes']}"
        parts.append(line)

    parts.append("\n## Available Managers (for open-ended steps)\n")
    for m in catalog["managers"]:
        parts.append(f"- **{m['name']}**: {m['purpose']}")

    return "\n".join(parts)


# --- Tool planning ---

def plan_step_tools(
    *,
    step_intent: str,
    tool_catalog_text: str | None = None,
) -> dict[str, Any]:
    """Use an LLM to recommend tools for a step based on intent.

    Args:
        step_intent: Natural language description of what the step should do.
        tool_catalog_text: Pre-built catalog text. Built fresh if None.

    Returns dict matching ToolPlanResult schema.
    """
    if tool_catalog_text is None:
        tool_catalog_text = build_tool_catalog_for_prompt()

    system_prompt = f"""You are a tool planning assistant for an AI automation system.

Given a step description and a catalog of available tools and managers, recommend the best execution strategy.

{tool_catalog_text}

## Decision criteria

Choose **deterministic** when:
- The exact tools and arguments are known (e.g., navigate to a specific URL)
- No LLM reasoning is needed to decide what to do
- The sequence is the same every time

Choose **manager** when:
- The work is open-ended (research, compose email, synthesize information)
- Tool arguments depend on runtime data or require judgment
- The step involves multi-turn interaction or complex decision-making
- The step involves both collecting data AND interpreting/synthesizing it

Important: respect the user's steps. Each step maps 1:1 to what the user wrote. Do NOT split or restructure steps. If a step mixes collection and interpretation, plan it as manager — the manager handles the full scope.

## System variables available at runtime

These are resolved at execution time. Use them instead of hardcoded dates/times:
- ${{now}} — current UTC ISO datetime
- ${{now_local}} — current local ISO datetime
- ${{today}} — today's date (YYYY-MM-DD)
- ${{hours_ago_N}} — ISO datetime N hours before now (e.g. ${{hours_ago_10}})
- ${{minutes_ago_N}} — ISO datetime N minutes before now
- ${{artifact_N}} — output from a prior step
- ${{prev_result}} — previous tool call's result within the same step

**CRITICAL**: Never hardcode timestamps or dates. If a step says "last 10 hours", use ${{hours_ago_10}}, not a literal datetime.

## Rules
- Only use tool names from the catalog above
- For deterministic steps, provide concrete argument values when the intent specifies them
- For time-relative arguments, use system variables (${{hours_ago_N}}, ${{now}}, ${{today}})
- Always fill in produces_description to describe what the step creates"""

    user_prompt = f"""Plan the execution for this step:

{step_intent}

Return your recommendation as structured output."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message

        agent = DI.agent_factory.create_agent("task_ir::tool_planner")
        agent.blackboard.update_state_value("step_intent", step_intent)
        agent.blackboard.update_state_value("tool_catalog", tool_catalog_text)

        msg = Message(agent_input={
            "step_intent": step_intent,
            "tool_catalog": tool_catalog_text,
        })
        result = agent.action_handler(msg)
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            return data

        logger.warning("Tool planner agent returned unexpected result: %s", type(data).__name__)
    except Exception as e:
        logger.error("Tool planner failed: %s", e)
        logger.debug("Tool planner exception details", exc_info=True)

    return {
        "recommendation": "manager",
        "manager_name": "emi_team_manager",
        "tools": [],
        "reasoning": f"Fallback — could not plan tools for: {step_intent}",
        "produces_description": "",
    }


def plan_multiple_steps(
    step_intents: list[str],
    only_tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Plan tools for multiple steps in one batch.

    Builds the catalog once and reuses it for all steps.

    Args:
        step_intents: Natural language descriptions for each step.
        only_tools: If provided, only include these tools in the catalog.
    """
    catalog_text = build_tool_catalog_for_prompt(only_tools=only_tools)
    results = []
    for intent in step_intents:
        result = plan_step_tools(step_intent=intent, tool_catalog_text=catalog_text)
        results.append(result)
    return results
