"""
Tool sequence executor for the task IR runtime.

Executes a deterministic sequence of tool calls without involving any
manager or LLM.  Each tool in the sequence is called directly via the
tool registry.  Results are collected and the final result is stored
as the step's produced data.

This is dramatically faster than routing through a manager — no planner,
no critic, no tool argument agent.  Use for steps where the exact tool
calls and arguments are known at compile time.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.task_ir_runtime.task_ir_steps import TaskIRSteps
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolSequenceExecutor:
    """
    Execute a list of tool calls in order, collecting results.

    Step schema for tool_sequence:
    {
      "id": "step_2",
      "kind": "tool_sequence",
      "title": "Capture CNN headlines",
      "tools": [
        {"tool": "web_navigate_snapshot", "args": {"url": "https://www.cnn.com"}},
        {"tool": "web_scroll", "args": {"direction": "down", "amount": 600, "steps": 2}},
        {"tool": "web_visual_scout", "args": {"question": "List top 5 headlines"}}
      ],
      "next_step": "step_3",
      "consumes_data_ids": [],
      "produces_data_ids": ["artifact_2"]
    }

    The final tool's result becomes the step output.  All intermediate
    results are logged but not stored as separate data IDs.
    """

    def execute_tool_sequence_step(
        self,
        *,
        step: dict[str, Any],
        context: dict[str, Any],
        task: dict[str, Any],
    ) -> dict[str, Any]:
        step_id = str(step.get("id") or "").strip()
        title = str(step.get("title") or step_id).strip()
        tools = step.get("tools")

        if not isinstance(tools, list) or not tools:
            raise ValueError(f"tool_sequence step '{step_id}' requires a non-empty 'tools' list.")

        logger.info(
            "TASKIR TOOL_SEQUENCE START step_id=%s title=%s tool_count=%d",
            step_id, title, len(tools),
        )

        # Resolve consumed inputs and make them available for arg substitution.
        consumed_ids = TaskIRSteps.normalized_step_data_ids(step=step, field_name="consumes_data_ids")
        consumed_values: dict[str, Any] = {}
        if consumed_ids:
            task_state = context.get("task_state", {})
            for data_id in consumed_ids:
                bucket = "artifacts" if data_id.startswith("artifact_") else "facts"
                value = task_state.get(bucket, {}).get(data_id)
                if value is not None:
                    consumed_values[data_id] = value

        results: list[dict[str, Any]] = []
        last_result: ToolResult | None = None

        for idx, tool_spec in enumerate(tools):
            if not isinstance(tool_spec, dict):
                raise ValueError(f"tool_sequence step '{step_id}' tools[{idx}] must be an object.")

            tool_name = str(tool_spec.get("tool") or "").strip()
            # Support both "args" (dict) and "args_json" (string) formats.
            tool_args = tool_spec.get("args")
            if tool_args is None:
                args_json = str(tool_spec.get("args_json") or "{}").strip()
                try:
                    tool_args = json.loads(args_json) if args_json else {}
                except json.JSONDecodeError:
                    tool_args = {}
            if not isinstance(tool_args, dict):
                tool_args = {}
            else:
                tool_args = dict(tool_args)

            if not tool_name:
                raise ValueError(f"tool_sequence step '{step_id}' tools[{idx}] missing 'tool' name.")

            # Substitute ${data_id} placeholders in string args with consumed values.
            tool_args = _substitute_args(tool_args, consumed_values)

            # Also substitute ${prev_result} with the previous tool's result content.
            if last_result is not None:
                prev_data = {
                    "prev_result": last_result.content or "",
                    "prev_result_data": last_result.data if isinstance(last_result.data, dict) else {},
                }
                tool_args = _substitute_args(tool_args, prev_data)

            logger.info(
                "TASKIR TOOL_SEQUENCE CALL [%d/%d] tool=%s args_keys=%s",
                idx + 1, len(tools), tool_name, list(tool_args.keys()),
            )

            tool_cls = DI.tool_registry.get_tool_class(tool_name)
            if tool_cls is None:
                raise ValueError(
                    f"tool_sequence step '{step_id}' tools[{idx}]: "
                    f"tool '{tool_name}' not found in registry."
                )

            instance = tool_cls()
            msg = ToolMessage(
                tool_name=tool_name,
                tool_data={
                    "tool_name": tool_name,
                    "arguments": tool_args,
                },
                content=f"tool_sequence:{step_id}:{tool_name}",
            )

            result = instance.execute(msg)
            if not isinstance(result, ToolResult):
                raise RuntimeError(
                    f"Tool '{tool_name}' returned non-ToolResult: {type(result).__name__}"
                )

            result_type = str(result.result_type or "").strip().lower()
            is_error = result_type in ("error", "tool_error", "manager_aborted") or getattr(result, "error", False)

            results.append({
                "tool": tool_name,
                "result_type": result_type,
                "content": str(result.content or "")[:500],
                "error": is_error,
            })

            if is_error:
                logger.warning(
                    "TASKIR TOOL_SEQUENCE ERROR [%d/%d] tool=%s: %s",
                    idx + 1, len(tools), tool_name,
                    str(result.content or "")[:2000],
                )
                # Don't abort the sequence — log the error and continue.
                # The step can declare error handling in the task spec.

            last_result = result
            logger.info(
                "TASKIR TOOL_SEQUENCE RESULT [%d/%d] tool=%s result_type=%s",
                idx + 1, len(tools), tool_name, result_type,
            )

        # Store final result in task_state for produced_data_ids.
        produced_ids = TaskIRSteps.normalized_step_data_ids(step=step, field_name="produces_data_ids")
        if produced_ids and last_result is not None:
            task_state = context.setdefault("task_state", {})
            artifacts = task_state.setdefault("artifacts", {})
            # Store the final result as the produced artifact.
            final_output = {
                "content": str(last_result.content or ""),
                "data": last_result.data if isinstance(last_result.data, dict) else {},
                "tool_sequence_results": results,
            }
            for data_id in produced_ids:
                if data_id.startswith("artifact_"):
                    artifacts[data_id] = final_output
                else:
                    facts = task_state.setdefault("facts", {})
                    facts[data_id] = final_output

            # Update data_index for downstream tracking.
            data_index = task_state.setdefault("data_index", {})
            for data_id in produced_ids:
                bucket = "artifacts" if data_id.startswith("artifact_") else "facts"
                data_index[data_id] = {
                    "state_path": f"task_state.{bucket}.{data_id}",
                    "producer_step_id": step_id,
                    "produced_at_utc": _now_iso(),
                }

        # Record in action history.
        history = context.get("action_history")
        if not isinstance(history, list):
            history = []
        history.append({
            "step_id": step_id,
            "executor": "tool_sequence",
            "result_type": "tool_sequence_completed",
            "content": f"Executed {len(tools)} tool(s): {', '.join(r['tool'] for r in results)}",
            "errors": sum(1 for r in results if r.get("error")),
            "timestamp_utc": _now_iso(),
        })
        context["action_history"] = history
        context["last_action_result"] = {
            "step_id": step_id,
            "executor": "tool_sequence",
            "result_type": "tool_sequence_completed",
            "content": str(last_result.content or "") if last_result else "",
            "data": last_result.data if last_result and isinstance(last_result.data, dict) else {},
        }

        logger.info(
            "TASKIR TOOL_SEQUENCE DONE step_id=%s tools=%d errors=%d",
            step_id, len(tools),
            sum(1 for r in results if r.get("error")),
        )

        return context


def _resolve_system_variables() -> dict[str, Any]:
    """Build a dict of system variables available at runtime.

    These are resolved fresh on every tool call so they reflect the
    actual execution time, not compile time.

    Available variables:
      ${now}           — current UTC ISO datetime  (2026-04-02T14:30:00+00:00)
      ${now_local}     — current local ISO datetime (2026-04-02T09:30:00-05:00)
      ${today}         — today's date YYYY-MM-DD
      ${hours_ago_N}   — ISO datetime N hours before now (e.g. ${hours_ago_10})
      ${minutes_ago_N} — ISO datetime N minutes before now
    """
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    variables: dict[str, str] = {
        "now": now_utc.isoformat(),
        "now_local": now_local.isoformat(),
        "today": now_local.strftime("%Y-%m-%d"),
    }
    return variables


_HOURS_AGO_PATTERN = "hours_ago_"
_MINUTES_AGO_PATTERN = "minutes_ago_"


def _substitute_args(args: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Replace ${key} placeholders in string values with values from the dict.

    Also resolves system variables (${now}, ${today}, ${hours_ago_N}, etc.).
    Recurses into nested dicts so invoke_agent agent_input gets resolved.
    """
    # Merge system variables (lower priority than explicit values).
    system_vars = _resolve_system_variables()
    merged = {**system_vars, **values}

    def _resolve(v: Any) -> Any:
        if isinstance(v, str):
            v = _resolve_dynamic_time_vars(v)
            for ref_key, ref_val in merged.items():
                placeholder = f"${{{ref_key}}}"
                if placeholder in v:
                    if v == placeholder:
                        return ref_val
                    elif isinstance(ref_val, str):
                        v = v.replace(placeholder, ref_val)
                    else:
                        v = v.replace(placeholder, json.dumps(ref_val, ensure_ascii=False))
            return v
        elif isinstance(v, dict):
            return {dk: _resolve(dv) for dk, dv in v.items()}
        elif isinstance(v, list):
            return [_resolve(item) for item in v]
        return v

    return {k: _resolve(v) for k, v in args.items()}


def _resolve_dynamic_time_vars(text: str) -> str:
    """Resolve ${hours_ago_N} and ${minutes_ago_N} patterns in a string."""
    import re
    from datetime import timedelta

    def _replace_time(match: re.Match) -> str:
        unit = match.group(1)   # "hours" or "minutes"
        n = int(match.group(2))
        now_utc = datetime.now(timezone.utc)
        if unit == "hours":
            result = now_utc - timedelta(hours=n)
        else:
            result = now_utc - timedelta(minutes=n)
        return result.isoformat()

    return re.sub(r"\$\{(hours|minutes)_ago_(\d+)\}", _replace_time, text)
