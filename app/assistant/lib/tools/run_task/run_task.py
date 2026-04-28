from __future__ import annotations

import json
from typing import Any, Optional

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.task_ir_runtime import get_task_ir_runner
from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import resolve_repo_path
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.task_registry import TaskRegistryEntry, resolve_task_entry

logger = get_logger(__name__)


class RunTaskTool(BaseTool):
    def __init__(self):
        super().__init__("run_task")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = tool_message.tool_data.get("arguments", {}) if tool_message.tool_data else {}
        run_id = str(args.get("run_id") or "").strip()
        if run_id:
            return self._resume_run(args=args, run_id=run_id)
        return self._start_run(args=args, tool_message=tool_message)

    def _start_run(self, *, args: dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        compiled_file = str(args.get("compiled_file") or "").strip()
        task_file = str(args.get("task_file") or "").strip()
        task_query = str(args.get("task_query") or args.get("task_name") or args.get("task") or "").strip()
        initial_context = args.get("initial_context") if isinstance(args.get("initial_context"), dict) else {}
        if not isinstance(initial_context, dict):
            raise ValueError("initial_context must be an object when provided.")
        initial_context = dict(initial_context)
        inherited_scope = getattr(tool_message, "scope_context", None)
        if inherited_scope is not None:
            if hasattr(inherited_scope, "model_dump"):
                initial_context["_task_ir_inherited_scope_context"] = inherited_scope.model_dump()
            elif isinstance(inherited_scope, dict):
                initial_context["_task_ir_inherited_scope_context"] = dict(inherited_scope)
            else:
                raise TypeError(
                    f"scope_context must be ScopeContext or dict, got {type(inherited_scope).__name__}."
                )

        resolved_entry: TaskRegistryEntry | None = None
        if not compiled_file:
            if task_file:
                return make_tool_error(
                    error_code="compiled_task_task_file_unsupported",
                    message="task_file is not supported in run_task runtime resolution. Provide compiled_file or task_query/task_name.",
                    abort_policy="abort_tool",
                    retryable=False,
                    details={"task_file": task_file},
                )
            if not task_query:
                task_query = str(tool_message.content or "").strip()
            entry, matches = resolve_task_entry(task_query)
            if entry is None:
                if matches:
                    options = ", ".join([m.task_id for m in matches])
                    return make_tool_error(
                        error_code="compiled_task_name_ambiguous",
                        message=f"Task name is ambiguous. Matches: {options}",
                        abort_policy="abort_tool",
                        retryable=False,
                        details={"task_query": task_query, "matches": [m.task_id for m in matches]},
                    )
                return make_tool_error(
                    error_code="compiled_task_not_found",
                    message="Task not found. Provide compiled_file or a known task name.",
                    abort_policy="abort_tool",
                    retryable=False,
                    details={"task_query": task_query},
                )
            resolved_entry = entry
            compiled_file = self._resolve_compiled_file_from_registry_entry(entry)

        try:
            compiled_task = self._read_compiled_task_file(compiled_file)
            runner = get_task_ir_runner()
            runner.ensure_event_subscription()
            run_state = runner.start_run(
                compiled_task=compiled_task,
                initial_context=initial_context,
            )
            status = str(run_state.get("status") or "")
            run_id = str(run_state.get("run_id") or "")
            waiting_event_name = str(run_state.get("waiting_event_name") or "")
            task_id = resolved_entry.task_id if resolved_entry else str(compiled_task.get("task_id") or "").strip() or None
            self._emit_task_running_notice(
                tool_message=tool_message,
                task_id=task_id,
                run_id=run_id,
                status=status,
                waiting_event_name=waiting_event_name,
            )
            label = (task_id or "task").replace("_", " ").strip()
            if status == "completed":
                human = f"Task '{label}' completed."
            elif waiting_event_name:
                human = f"Task '{label}' started; waiting on '{waiting_event_name}'."
            else:
                human = f"Task '{label}' {status or 'running'}."
            return ToolResult(
                result_type="success",
                content=human,
                data={
                    "run_id": run_id,
                    "status": status,
                    "waiting_event_name": waiting_event_name or None,
                    "compiled_file": compiled_file,
                    "task_id": task_id,
                },
            )
        except Exception as e:
            logger.error("run_task failed to start run: %s", e)
            logger.debug("run_task start exception details", exc_info=True)
            return make_tool_error(
                error_code="compiled_task_start_failed",
                message=f"Failed to start compiled task: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"compiled_file": compiled_file, "error": str(e)},
            )

    def _resume_run(self, *, args: dict[str, Any], run_id: str) -> ToolResult:
        event_name = str(args.get("event_name") or "").strip() or None
        event_payload = args.get("event_payload") if isinstance(args.get("event_payload"), dict) else {}
        try:
            runner = get_task_ir_runner()
            runner.ensure_event_subscription()
            run_state = runner.resume_run(
                run_id=run_id,
                event_name=event_name,
                event_payload=event_payload,
            )
            status = str(run_state.get("status") or "")
            waiting_event_name = str(run_state.get("waiting_event_name") or "")
            if status == "completed":
                human = "Task completed."
            elif waiting_event_name:
                human = f"Task resumed; waiting on '{waiting_event_name}'."
            else:
                human = f"Task {status or 'running'}."
            return ToolResult(
                result_type="success",
                content=human,
                data={
                    "run_id": run_id,
                    "status": status,
                    "waiting_event_name": waiting_event_name or None,
                },
            )
        except Exception as e:
            logger.error("run_task resume failed run_id=%s: %s", run_id, e)
            logger.debug("run_task resume exception details", exc_info=True)
            return make_tool_error(
                error_code="compiled_task_resume_failed",
                message=f"Failed to resume run '{run_id}': {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"run_id": run_id, "event_name": event_name, "error": str(e)},
            )

    def _emit_task_running_notice(
        self,
        *,
        tool_message: ToolMessage,
        task_id: str | None,
        run_id: str,
        status: str,
        waiting_event_name: str,
    ) -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData

            request_context = (tool_message.tool_data or {}).get("request_context", {})
            reply_to = request_context.get("reply_to") if isinstance(request_context, dict) else None
            if not isinstance(reply_to, dict):
                return
            if reply_to.get("type") != "socketio":
                return
            socket_id = str(reply_to.get("room") or "").strip()
            if not socket_id:
                return

            task_label = str(task_id or "").replace("_", " ").strip() or "task"
            if status == "waiting" and waiting_event_name:
                body = f"Task **{task_label}** started and waiting for: `{waiting_event_name}`"
            elif status == "completed":
                body = f"Task **{task_label}** completed."
            elif status == "failed":
                body = f"Task **{task_label}** failed to start."
            else:
                body = f"Task **{task_label}** is running. (run_id: `{run_id}`)"

            assistant_name = get_required_assistant_name()
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": reply_to},
                user_message_data=UserMessageData(
                    widget_data=[
                        {
                            "data_type": "task_running_notice",
                            "task_id": task_id,
                            "run_id": run_id,
                            "status": status,
                            "waiting_event_name": waiting_event_name or None,
                            "message": body,
                        }
                    ]
                ),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.info(
                "run_task: emitted task_running_notice task_id=%s run_id=%s status=%s socket=%s",
                task_id,
                run_id,
                status,
                socket_id,
            )
        except Exception as e:
            logger.error("run_task: failed to emit task_running_notice: %s", e)
            logger.debug("run_task: task_running_notice emit exception", exc_info=True)

    def _resolve_compiled_file_from_registry_entry(self, entry: TaskRegistryEntry) -> str:
        task_file_path = resolve_repo_path(entry.task_file)
        deterministic_path = (task_file_path.parent / f"{str(entry.task_id).strip()}.json").resolve()
        return deterministic_path.as_posix()

    def _read_compiled_task_file(self, compiled_file: str) -> dict[str, Any]:
        path = resolve_repo_path(compiled_file)
        if not path.exists():
            raise FileNotFoundError(f"Compiled task file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("compiled task file must contain a JSON object")
        return data


def get_tool_class():
    return RunTaskTool

