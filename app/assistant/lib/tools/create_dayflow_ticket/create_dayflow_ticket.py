from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.ticket_manager import get_ticket_manager
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult, UserMessage, UserMessageData

logger = get_logger(__name__)
_DEFAULT_TICKET_TYPE = "dayflow_orchestrator"


class CreateDayflowTicketTool(BaseTool):
    def __init__(self) -> None:
        super().__init__("create_dayflow_ticket")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            args = self._extract_arguments(tool_message)

            # Accept the generic dispatcher shape (task + information) on
            # equal footing with ticket_brief. The switchboard hands every
            # tool call as {"task": ..., "information": ...}; ticket
            # dispatch should flow through that same shape so it isn't a
            # special case at the dispatcher layer.
            if (
                not args.get("title")
                and not args.get("ticket_brief")
                and (args.get("task") or args.get("information"))
            ):
                task_text = str(args.get("task") or "").strip()
                info_raw = args.get("information")
                if isinstance(info_raw, dict):
                    import json as _json
                    info_text = str(
                        info_raw.get("task") or info_raw.get("information")
                        or _json.dumps(info_raw, ensure_ascii=False)
                    ).strip()
                else:
                    info_text = str(info_raw or "").strip()
                brief = f"{task_text}\n{info_text}".strip() if task_text or info_text else ""
                if brief:
                    args["ticket_brief"] = brief

            # If pre-formatted fields are missing but a brief is provided,
            # use LLM to generate the ticket copy.
            if not args.get("title") and args.get("ticket_brief"):
                formatted = self._format_brief(args["ticket_brief"])
                args.update(formatted)

            ticket_kind = self._required_str(args, "ticket_kind").lower()
            suggestion_type = self._required_str(args, "suggestion_type")
            title = self._required_str(args, "title")
            message = self._required_str(args, "message")

            action_type = str(args.get("action_type") or "none").strip() or "none"
            action_params = args.get("action_params")
            trigger_context = dict(args.get("trigger_context") or {})
            trigger_reason = str(args.get("trigger_reason") or "").strip() or None
            valid_hours = self._coerce_valid_hours(args.get("valid_hours", 4))
            status_effect = args.get("status_effect")
            speak_tts = bool(args.get("speak_tts", True))
            button_layout, plan_mode_available = self._ui_policy_for_ticket_kind(ticket_kind)

            if action_params is not None and not isinstance(action_params, dict):
                raise ValueError("action_params must be an object when provided.")
            if trigger_context is not None and not isinstance(trigger_context, dict):
                raise ValueError("trigger_context must be an object when provided.")
            if status_effect is not None and not isinstance(status_effect, list):
                raise ValueError("status_effect must be an array when provided.")
            if "button_layout" in args:
                raise ValueError("button_layout is not allowed; it is derived from ticket_kind.")
            if "plan_mode_available" in args:
                raise ValueError("plan_mode_available is not allowed; it is derived from ticket_kind.")

            ticket_manager = get_ticket_manager()
            ticket = ticket_manager.create_ticket(
                ticket_type=_DEFAULT_TICKET_TYPE,
                suggestion_type=suggestion_type,
                title=title,
                message=message,
                action_type=action_type,
                action_params=action_params if isinstance(action_params, dict) else {},
                trigger_context=trigger_context if isinstance(trigger_context, dict) else {},
                trigger_reason=trigger_reason,
                valid_hours=valid_hours,
                status_effect=status_effect if isinstance(status_effect, list) else [],
            )
            if ticket is None:
                raise RuntimeError("ticket_manager.create_ticket returned None.")
            if not ticket_manager.mark_proposed(ticket.ticket_id):
                raise RuntimeError(f"Failed to mark ticket proposed: {ticket.ticket_id}")

            ticket_dict = ticket.to_dict()
            ticket_dict["button_layout"] = button_layout
            ticket_dict["plan_mode_available"] = plan_mode_available

            suggestion_msg = Message(event_topic="proactive_suggestion", data=ticket_dict)
            DI.event_hub.publish(suggestion_msg)

            if speak_tts:
                tts_text = ticket_dict.get("message") or ticket_dict.get("title") or "I have a suggestion for you."
                tts_message = UserMessage(
                    data_type="user_msg",
                    sender="create_dayflow_ticket",
                    receiver=None,
                    timestamp=datetime.now(timezone.utc),
                    role="assistant",
                    user_message_data=UserMessageData(feed=None, tts=True, tts_text=tts_text),
                    metadata={"reply_to": {"type": "socketio", "room_id": "master_room"}},
                )
                tts_message.event_topic = "socket_emit"
                DI.event_hub.publish(tts_message)

            # Wait for the user's response to the ticket.
            timeout = float(args.get("wait_timeout_seconds", 600))
            response = self._wait_for_ticket_response(ticket.ticket_id, title, timeout)
            return response
        except Exception as e:
            logger.error("create_dayflow_ticket failed: %s", e)
            logger.debug("create_dayflow_ticket exception details", exc_info=True)
            return ToolResult(result_type="error", content=f"create_dayflow_ticket failed: {e}", data={})

    @staticmethod
    def _format_brief(brief: str) -> dict[str, str]:
        """Use the ticket_builder agent to convert a brief into ticket fields."""
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message
            from app.assistant.scope.loader import load_scope_for_source

            agent = DI.agent_factory.create_agent("dayflow_orchestrator::ticket_builder")

            # Scope is the dayflow_orchestrator room scope.yaml (resources: [all]
            # there is a superset of the resource_user_data this agent resolves).
            scope = load_scope_for_source(
                kind="room",
                source_id="dayflow_orchestrator",
                actor_id="create_dayflow_ticket",
                identity_overrides={
                    "scope_id": "ticket_builder_inline",
                    "owner_id": "dayflow_orchestrator",
                    "surface": "system",
                    "room_id": "dayflow_orchestrator",
                },
            )

            msg = Message(
                agent_input={"ticket_brief": brief},
                scope_context=scope,
            )
            agent.blackboard.update_state_value("ticket_brief", brief)

            result = agent.action_handler(msg)
            data = getattr(result, "data", None)
            if isinstance(data, dict):
                # Agent form fields: ticket_kind, suggestion_type, title, message
                if data.get("title"):
                    return {
                        "ticket_kind": str(data.get("ticket_kind") or "advice"),
                        "suggestion_type": str(data.get("suggestion_type") or "general"),
                        "title": str(data.get("title") or ""),
                        "message": str(data.get("message") or brief),
                    }
        except Exception as e:
            logger.warning("create_dayflow_ticket: ticket_builder agent failed: %s", e)
            logger.debug("create_dayflow_ticket: ticket_builder exception details", exc_info=True)

        # Fallback: mechanical extraction if agent fails.
        is_decision = any(w in brief.lower() for w in ("ask", "decide", "choice", "confirm", "whether"))
        return {
            "ticket_kind": "decision" if is_decision else "advice",
            "suggestion_type": "general",
            "title": brief[:60].strip(),
            "message": brief.strip(),
        }

    @staticmethod
    def _wait_for_ticket_response(ticket_id: str, title: str, timeout: float) -> ToolResult:
        """Block until the user responds to the ticket, or timeout.

        Listens for 'dayflow_ticket_responded' events on the EventHub and
        checks if the event matches our ticket_id.
        """
        import threading

        result_holder: dict = {}
        event = threading.Event()

        def _on_ticket_responded(msg):
            data = getattr(msg, "data", None)
            if not isinstance(data, dict):
                return
            if data.get("ticket_id") != ticket_id:
                return
            result_holder["action"] = data.get("action", "")
            result_holder["user_text"] = data.get("user_text", "")
            result_holder["target_state"] = data.get("target_state", "")
            event.set()

        # Register listener
        DI.event_hub.register_event("dayflow_ticket_responded", _on_ticket_responded)

        try:
            logger.info("[create_dayflow_ticket] Waiting for response to ticket %s (timeout=%.0fs)", ticket_id, timeout)
            answered = event.wait(timeout=timeout)

            if not answered:
                logger.info("[create_dayflow_ticket] Ticket %s timed out after %.0fs", ticket_id, timeout)
                # Mark the ticket expired in the DB so bulk-list endpoints and
                # the UI re-fetch see it's no longer pending. Then publish an
                # update event so the frontend refreshes immediately instead
                # of waiting for its poll cycle.
                try:
                    get_ticket_manager().mark_expired(ticket_id, reason="wait_timeout")
                except Exception as exc:
                    logger.warning(
                        "[create_dayflow_ticket] mark_expired failed for %s: %s",
                        ticket_id, exc,
                    )
                try:
                    DI.event_hub.publish(Message(
                        event_topic="proactive_suggestion_update",
                        data={"ticket_id": ticket_id, "reason": "timeout"},
                    ))
                except Exception as exc:
                    logger.warning(
                        "[create_dayflow_ticket] publish suggestion_update failed for %s: %s",
                        ticket_id, exc,
                    )
                return ToolResult(
                    result_type="ticket_response",
                    content=f"Ticket '{title}' shown to user but no response within {int(timeout)}s.",
                    data={
                        "ticket_id": ticket_id,
                        "title": title,
                        "action": "timeout",
                        "user_text": "",
                    },
                )

            action = result_holder.get("action", "")
            user_text = result_holder.get("user_text", "")

            # Build human-readable result
            action_label = {
                "done": "accepted (done)",
                "willdo": "accepted (will do)",
                "acknowledge": "acknowledged",
                "accept": "accepted",
                "skip": "declined (skipped)",
                "no": "declined (no)",
                "dismiss": "dismissed",
                "later": "snoozed",
            }.get(action, action)

            content = f"User {action_label} ticket '{title}'."
            if user_text:
                content += f" User said: \"{user_text}\""

            logger.info("[create_dayflow_ticket] Ticket %s response: %s", ticket_id, content)

            return ToolResult(
                result_type="ticket_response",
                content=content,
                data={
                    "ticket_id": ticket_id,
                    "title": title,
                    "action": action,
                    "user_text": user_text,
                },
            )
        finally:
            # Unregister the closure so we don't accumulate per-ticket
            # listeners on event_hub. Each leftover handler is a no-op but
            # every 'dayflow_ticket_responded' publish iterates them all.
            try:
                DI.event_hub.unregister_event("dayflow_ticket_responded", _on_ticket_responded)
            except Exception:
                pass

    @staticmethod
    def _extract_arguments(tool_message: ToolMessage) -> dict[str, Any]:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments")
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object.")
        return args

    @staticmethod
    def _required_str(args: dict[str, Any], key: str) -> str:
        value = str(args.get(key) or "").strip()
        if not value:
            raise ValueError(f"Missing required string field: {key}")
        return value

    @staticmethod
    def _coerce_valid_hours(raw: Any) -> int:
        try:
            value = int(raw)
        except Exception as e:
            raise ValueError(f"valid_hours must be an integer, got {raw!r}") from e
        if value <= 0:
            raise ValueError("valid_hours must be > 0")
        return value

    @staticmethod
    def _ui_policy_for_ticket_kind(ticket_kind: str) -> tuple[str, bool]:
        if ticket_kind == "notify":
            return "notify", False
        if ticket_kind == "decision":
            return "decision", True
        if ticket_kind == "advice":
            return "advice", True
        raise ValueError(
            f"ticket_kind must be one of: notify, decision, advice. Got: {ticket_kind!r}"
        )


def get_tool_class():
    return CreateDayflowTicketTool
