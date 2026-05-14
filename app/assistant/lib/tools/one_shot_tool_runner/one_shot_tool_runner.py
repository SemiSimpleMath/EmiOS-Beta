from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from pydantic import BaseModel

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, Message, UserMessage, UserMessageData
from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)


class OneShotToolPick(BaseModel):
    tool_name: str
    arguments_json: str
    reason: str | None = None


OneShotToolPick.model_rebuild()


class OneShotToolRunner(BaseTool):
    """
    One-shot tool runner.

    - Uses a single LLM call to choose a tool and produce arguments.
    - Executes the tool immediately.
    - Returns a deterministic final answer based on tool_result.
    """

    ALLOWED_TOOLS = [
        "create_scheduler_event",
        "create_repeating_scheduler_event",
        "create_todo_task",
        "get_todo_tasks",
        "create_calendar_event",
        "create_repeating_calendar_event",
        "get_calendar_events",
        "get_weather",
        "set_ui_mute",
        "peak_at_link",
        "summarize_link",
        "get_important_emails",
        "nest_home_control",
        "lights_control",
        "ring_camera_control",
    ]

    def __init__(self):
        super().__init__("one_shot_tool_runner")

    def _tool_schema(self, tool_name: str) -> dict:
        try:
            form = DI.tool_registry.get_tool_form(tool_name)
        except Exception:
            form = None
        if form is None:
            return {}
        if hasattr(form, "model_json_schema"):
            try:
                return form.model_json_schema()
            except Exception:
                return {}
        if hasattr(form, "schema"):
            try:
                return form.schema()
            except Exception:
                return {}
        return {}

    def _build_tools_text(self) -> str:
        tools_block = []
        for name in self.ALLOWED_TOOLS:
            desc = ""
            try:
                desc = DI.tool_registry.get_tool_description(name) or ""
            except Exception:
                desc = ""
            schema = self._tool_schema(name)
            try:
                schema_str = json.dumps(schema, indent=2, ensure_ascii=True)
            except Exception:
                schema_str = str(schema)
            tools_block.append(
                f"- {name}: {desc}\n  JSON Schema for arguments:\n{schema_str}"
            )
        return "\n".join(tools_block).strip()

    @staticmethod
    def _task_looks_distance_like(text: str) -> bool:
        if not isinstance(text, str):
            return False
        lowered = text.lower()
        markers = [
            "distance",
            "great-circle",
            "great circle",
            "haversine",
            "straight-line",
            "straight line",
            "miles between",
            "km between",
            "kilometers between",
        ]
        return any(marker in lowered for marker in markers)

    def _call_agent(self, task: str, information: str, tools_text: str) -> dict:
        agent = DI.agent_factory.create_agent("one_shot_agent")
        if agent is None:
            return {}
        msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "task": task,
                "information": information,
                "tools_text": tools_text,
            }
        )
        response = agent.action_handler(msg)
        if isinstance(response, dict):
            return response
        if isinstance(response, OneShotToolPick):
            return response.model_dump()
        return {}

    def _execute_tool(self, tool_name: str, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        tool_class = DI.tool_registry.get_tool_class(tool_name)
        if tool_class is None:
            return make_tool_error(
                error_code="tool_not_found",
                message=f"Tool not found: {tool_name}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "one_shot_tool_runner", "requested": tool_name},
            )
        tool_instance = tool_class()
        msg = ToolMessage(
            tool_name=tool_name,
            tool_data={"tool_name": tool_name, "arguments": arguments},
            task=tool_message.task,
        )
        result = tool_instance.execute(msg)
        if isinstance(result, ToolMessage) and isinstance(result.tool_result, ToolResult):
            return result.tool_result
        if isinstance(result, ToolResult):
            return result
        return make_tool_error(
            error_code="unexpected_result_type",
            message=f"Unexpected tool result type from {tool_name}",
            abort_policy="abort_tool",
            retryable=False,
            details={"tool": "one_shot_tool_runner", "requested": tool_name},
        )

    def _normalize_datetime_value(self, value: str) -> str:
        raw = value.strip()
        # If already ISO with T, keep as-is (still allow timezone offset).
        if "T" in raw:
            return raw
        # Strip trailing timezone abbreviations like "PST", "EST", etc.
        raw = re.sub(r"\s+[A-Z]{2,5}$", "", raw)
        # Convert "YYYY-MM-DD HH:MM:SS" -> "YYYY-MM-DDTHH:MM:SS"
        raw = re.sub(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})$", r"\1T\2", raw)
        return raw

    def _normalize_tool_arguments(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        args = dict(arguments)
        if tool_name in {"create_scheduler_event", "create_repeating_scheduler_event"}:
            for key in ("start_date", "end_date"):
                if isinstance(args.get(key), str) and args[key].strip():
                    args[key] = self._normalize_datetime_value(args[key])
        if tool_name in {"create_calendar_event", "create_repeating_calendar_event"}:
            for key in ("start", "end"):
                if isinstance(args.get(key), str) and args[key].strip():
                    args[key] = self._normalize_datetime_value(args[key])
        if tool_name in {"create_scheduler_event", "create_repeating_scheduler_event"}:
            # One-shot reminder aliases should not have to emit internal scheduler defaults.
            if not isinstance(args.get("task_type"), str) or not args.get("task_type", "").strip():
                args["task_type"] = "reminder"
            if not isinstance(args.get("importance"), int):
                args["importance"] = 3
            if not isinstance(args.get("sound"), str) or not args.get("sound", "").strip():
                args["sound"] = "default"
        return args

    @staticmethod
    def _canonicalize_tool_name(tool_name: Any) -> Any:
        if not isinstance(tool_name, str):
            return tool_name
        mapping = {
            "create_reminder": "create_scheduler_event",
            "create_repeating_reminder": "create_repeating_scheduler_event",
        }
        normalized = tool_name.strip()
        return mapping.get(normalized, normalized)

    @staticmethod
    def _pick_str(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _format_when(value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""

    def _deterministic_success_text(
        self,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_result: ToolResult,
    ) -> str:
        if tool_name == "create_todo_task":
            title = self._pick_str(arguments.get("task_name"), arguments.get("title"), "Untitled task")
            due = self._format_when(arguments.get("due_date"))
            if due:
                return f"Todo task set: {title} (due: {due})."
            return f"Todo task set: {title}."

        if tool_name == "get_todo_tasks":
            items = tool_result.data_list if isinstance(tool_result.data_list, list) else []
            if not items:
                return "No active todo tasks."
            return tool_result.content or f"Found {len(items)} active todo task(s)."

        if tool_name in {"create_calendar_event", "create_repeating_calendar_event"}:
            title = self._pick_str(arguments.get("event_name"), arguments.get("title"), "Untitled event")
            start = self._format_when(arguments.get("start"))
            end = self._format_when(arguments.get("end"))
            recurrence = self._pick_str(arguments.get("recurrence_rule"))
            if start and end and recurrence:
                return f"Calendar set: {title} from {start} to {end} ({recurrence})."
            if start and end:
                return f"Calendar set: {title} from {start} to {end}."
            if start:
                return f"Calendar set: {title} at {start}."
            return f"Calendar set: {title}."

        if tool_name in {"create_scheduler_event", "create_repeating_scheduler_event"}:
            title = self._pick_str(arguments.get("title"), arguments.get("task_type"), "Reminder")
            start = self._format_when(arguments.get("start_date"))
            interval = arguments.get("interval")
            if tool_name == "create_repeating_scheduler_event" and interval:
                if start:
                    return f"Reminder set: {title}, every {interval} seconds starting {start}."
                return f"Reminder set: {title}, every {interval} seconds."
            if start:
                return f"Reminder set: {title} at {start}."
            return f"Reminder set: {title}."

        if tool_name == "get_weather":
            row = tool_result.data_list[0] if isinstance(tool_result.data_list, list) and tool_result.data_list else {}
            if not isinstance(row, dict):
                row = {}
            location = row.get("location") if isinstance(row.get("location"), dict) else {}
            weather = row.get("weather") if isinstance(row.get("weather"), dict) else {}
            city = self._pick_str(location.get("name"), arguments.get("city"), "requested location")
            condition = self._pick_str(weather.get("description"), "n/a")
            temp = weather.get("temperature")
            if temp is not None:
                return f"Weather at {city}: {temp} F, {condition}."
            return f"Weather at {city}: {condition}."

        if tool_name == "set_ui_mute":
            action = self._pick_str(arguments.get("action"), "toggle").lower()
            if action == "mute":
                return "UI audio muted."
            if action == "unmute":
                return "UI audio unmuted."
            return "UI audio mute toggled."

        if tool_name == "peak_at_link":
            url = self._pick_str(arguments.get("url"), "link")
            return f"Peeked at {url}."

        if tool_name == "summarize_link":
            url = self._pick_str(arguments.get("url"), "link")
            return f"Summarized {url}."

        if tool_name == "get_important_emails":
            if tool_result.result_type == "error":
                return tool_result.content or "Failed to fetch email summary."
            items = tool_result.data_list if isinstance(tool_result.data_list, list) else []
            count = len(items)
            if count == 0:
                return "No important emails in the last 12 hours."
            return tool_result.content or f"Found {count} important email(s)."

        # Keep deterministic fallback for any future one-shot tool additions.
        return tool_result.content or f"Completed {tool_name}."

    def _finalize(self, tool_name: str, arguments: Dict[str, Any], tool_result: ToolResult) -> ToolResult:
        if tool_result.result_type == "error":
            content = tool_result.content or f"Failed to execute {tool_name}."
        elif tool_name in {"peak_at_link", "summarize_link"}:
            # Preserve rich read-only output instead of collapsing it to a canned one-liner.
            content = tool_result.content or f"Completed {tool_name}."
        elif tool_name == "get_todo_tasks":
            items = tool_result.data_list if isinstance(tool_result.data_list, list) else []
            if not items:
                content = "No active todo tasks."
            else:
                lines = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip() or "(untitled)"
                    due = str(item.get("due") or "").strip()
                    status = str(item.get("status") or "").strip()
                    task_list = str(item.get("task_list_title") or "").strip()
                    line = f"• {title}"
                    if due:
                        line += f" (due: {due})"
                    if task_list:
                        line += f" [{task_list}]"
                    if status:
                        line += f" — {status}"
                    lines.append(line)
                content = "\n".join(lines) if lines else tool_result.content or "No active todo tasks."
        elif tool_name == "get_important_emails":
            items = tool_result.data_list if isinstance(tool_result.data_list, list) else []
            if tool_result.result_type == "error" or not items:
                content = tool_result.content or "No important emails found."
            else:
                lines = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    subject = str(item.get("subject") or "").strip() or "(no subject)"
                    sender = str(item.get("from") or item.get("sender") or "").strip() or "Unknown"
                    summary = str(item.get("summary") or "").strip()
                    importance = item.get("importance")
                    action_items = item.get("action_items") or []
                    line = f"• {subject} — from {sender}"
                    if importance is not None:
                        line += f" [importance: {importance}]"
                    if summary:
                        line += f"\n  {summary}"
                    if isinstance(action_items, list) and action_items:
                        line += f"\n  Actions: {', '.join(str(a) for a in action_items)}"
                    lines.append(line)
                content = "\n".join(lines) if lines else tool_result.content or "No important emails found."
        else:
            content = self._deterministic_success_text(
                tool_name=tool_name,
                arguments=arguments if isinstance(arguments, dict) else {},
                tool_result=tool_result,
            )
        return ToolResult(
            result_type="final_answer",
            content=content,
            data_list=tool_result.data_list or [],
            data=tool_result.data,
        )

    def _quick_ui_text(self, tool_name: str, tool_result: ToolResult) -> str:
        if tool_result.result_type == "error":
            return tool_result.content or "Something went wrong."
        mapping = {
            "create_scheduler_event": "Reminder set.",
            "create_repeating_scheduler_event": "Repeating reminder set.",
            "create_calendar_event": "Calendar event created.",
            "create_repeating_calendar_event": "Repeating calendar event created.",
            "create_todo_task": "Todo created.",
            "get_todo_tasks": "Fetching todos...",
            "get_weather": "Weather fetched.",
            "set_ui_mute": "Updated UI mute state.",
            "peak_at_link": "Link preview ready.",
            "summarize_link": "Link summary ready.",
            "get_important_emails": "Checking email...",
            "nest_home_control": "Applying Nest update...",
            "lights_control": "Applying lights update...",
            "ring_camera_control": "Applying Ring action...",
        }
        return mapping.get(tool_name, "Done.")

    def _quick_ui_text_for_tool(self, tool_name: str) -> str:
        mapping = {
            "create_scheduler_event": "Reminder set.",
            "create_repeating_scheduler_event": "Repeating reminder set.",
            "create_calendar_event": "Calendar event created.",
            "create_repeating_calendar_event": "Repeating calendar event created.",
            "create_todo_task": "Todo created.",
            "get_todo_tasks": "Fetching todos...",
            "get_weather": "Weather fetched.",
            "set_ui_mute": "Updated UI mute state.",
            "peak_at_link": "Link preview ready.",
            "summarize_link": "Link summary ready.",
            "get_important_emails": "Checking email...",
            "nest_home_control": "Applying Nest update...",
            "lights_control": "Applying lights update...",
            "ring_camera_control": "Applying Ring action...",
        }
        return mapping.get(tool_name, "Done.")

    def _publish_quick_ui_message(self, text: str, *, tool_message: ToolMessage | None = None) -> None:
        if not isinstance(text, str) or not text.strip():
            return
        message_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # Extract reply_to + request_id from the parent tool_message so the
        # relay knows where to deliver this user-facing message. Without
        # these, the relay can't resolve a transport and drops the message
        # silently (symptom: UI sees no "Checking email..." placeholder).
        reply_to = None
        request_id = None
        if tool_message is not None:
            ctx = (tool_message.tool_data or {}).get("request_context") or {}
            if isinstance(ctx, dict):
                rt = ctx.get("reply_to")
                if isinstance(rt, dict) and rt:
                    reply_to = rt
                rid = ctx.get("request_id") or tool_message.request_id
                if isinstance(rid, str) and rid.strip():
                    request_id = rid.strip()

        try:
            user_msg_bb = Message(
                data_type="emi_msg",
                sender=self.name,
                receiver=None,
                content=text,
                timestamp=now,
                id=message_id,
                role="assistant",
                is_chat=True,
            )
            try:
                DI.global_blackboard.add_msg(user_msg_bb)
            except Exception:
                pass
            user_msg_data = UserMessageData(chat=text, feed=text)
            metadata: dict = {}
            if reply_to:
                metadata["reply_to"] = reply_to
            user_msg_chat = UserMessage(
                data_type="user_msg",
                sender=self.name,
                receiver=None,
                timestamp=now,
                id=message_id,
                role="assistant",
                user_message_data=user_msg_data,
                metadata=metadata or None,
                request_id=request_id,
            )
            user_msg_chat.event_topic = "socket_emit"
            DI.event_hub.publish(user_msg_chat)
        except Exception as e:
            logger.error(f"[one_shot_tool_runner] Failed to publish quick UI message: {e}")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = tool_message.tool_data.get("arguments", {}) if tool_message.tool_data else {}
        task = args.get("task") or tool_message.task or ""
        information = args.get("information") or tool_message.information or ""

        if not isinstance(task, str) or not task.strip():
            return make_tool_error(
                error_code="missing_task",
                message="Missing required argument: task",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "one_shot_tool_runner"},
            )
        if self._task_looks_distance_like(task) or self._task_looks_distance_like(str(information or "")):
            return make_tool_error(
                error_code="unsupported_task_type",
                message=(
                    "one_shot_tool_runner does not support distance/geospatial calculations. "
                    "Route this request to a web/manager flow (e.g., emi_team_manager or web_manager)."
                ),
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "one_shot_tool_runner"},
            )

        tools_text = self._build_tools_text()
        pick = self._call_agent(task=task.strip(), information=str(information or "").strip(), tools_text=tools_text)
        tool_name = self._canonicalize_tool_name(pick.get("tool_name"))
        arguments_json = pick.get("arguments_json")
        arguments = None
        if isinstance(arguments_json, str) and arguments_json.strip():
            try:
                arguments = json.loads(arguments_json)
            except Exception:
                arguments = None

        if tool_name not in self.ALLOWED_TOOLS:
            return make_tool_error(
                error_code="unsupported_tool",
                message=f"Invalid tool selected: {tool_name!r}. Allowed: {self.ALLOWED_TOOLS}",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "one_shot_tool_runner", "selected": tool_name},
            )
        if not isinstance(arguments, dict):
            return make_tool_error(
                error_code="invalid_arguments",
                message="Tool arguments must be a valid JSON object.",
                abort_policy="abort_tool",
                retryable=False,
                details={"tool": "one_shot_tool_runner"},
            )

        arguments = self._normalize_tool_arguments(tool_name, arguments)
        if tool_message.request_id:
            try:
                self._publish_quick_ui_message(
                    self._quick_ui_text_for_tool(tool_name),
                    tool_message=tool_message,
                )
            except Exception as e:
                logger.error(f"[one_shot_tool_runner] Failed to publish quick UI message: {e}")
        tool_result = self._execute_tool(tool_name, arguments, tool_message)
        if tool_message.request_id and tool_result.result_type == "error":
            try:
                self._publish_quick_ui_message(
                    self._quick_ui_text(tool_name, tool_result),
                    tool_message=tool_message,
                )
            except Exception as e:
                logger.error(f"[one_shot_tool_runner] Failed to publish error UI message: {e}")
        return self._finalize(tool_name, arguments, tool_result)


def get_tool_class():
    return OneShotToolRunner
