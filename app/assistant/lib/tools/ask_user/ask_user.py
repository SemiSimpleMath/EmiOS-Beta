from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, UserMessage, UserMessageData
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from datetime import datetime, timezone
import uuid

logger = get_logger(__name__)

class AskUserTool(BaseTool):
    def __init__(self):
        super().__init__('ask_user')

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        question = tool_message.tool_data.get('arguments', {}).get('text')
        if not question:
            return make_tool_error(
                error_code="ask_user_invalid_input",
                message="Missing 'question' argument.",
                abort_policy="abort_task",
                retryable=False,
                details={"tool_name": "ask_user"},
            )

        request_context = tool_message.tool_data.get("request_context") if isinstance(tool_message.tool_data, dict) else None
        if not isinstance(request_context, dict):
            request_context = {}

        request_id = request_context.get("request_id")
        room_id = request_context.get("room_id")
        reply_to = request_context.get("reply_to")
        user_identity = request_context.get("user_identity")

        timeout_seconds = request_context.get("ask_user_timeout_seconds", 300.0)
        try:
            timeout_seconds = float(timeout_seconds)
        except Exception as e:
            logger.error("[ask_user] Invalid ask_user timeout value: %r", timeout_seconds)
            logger.debug("[ask_user] ask_user timeout parse exception details", exc_info=True)
            return make_tool_error(
                error_code="ask_user_invalid_timeout",
                message=f"Invalid ask_user timeout: {timeout_seconds!r}. Error: {e}",
                abort_policy="abort_task",
                retryable=False,
                details={"timeout_seconds": timeout_seconds},
            )
        if timeout_seconds <= 0:
            return make_tool_error(
                error_code="ask_user_invalid_timeout",
                message="ask_user timeout must be > 0 seconds.",
                abort_policy="abort_task",
                retryable=False,
                details={"timeout_seconds": timeout_seconds},
            )

        question_service = DI.question_service
        question_id = question_service.request_question(
            text=question,
            request_id=request_id if isinstance(request_id, str) else None,
            room_id=room_id if isinstance(room_id, str) else None,
            reply_to=reply_to if isinstance(reply_to, dict) else None,
            user_identity=user_identity if isinstance(user_identity, str) else None,
            timeout_seconds=timeout_seconds,
        )

        self._emit_question_to_user(
            question=question,
            question_id=question_id,
            request_id=request_id if isinstance(request_id, str) else None,
            room_id=room_id if isinstance(room_id, str) else None,
            reply_to=reply_to if isinstance(reply_to, dict) else None,
            user_identity=user_identity if isinstance(user_identity, str) else None,
        )
        logger.info("[ask_user] Waiting for response. question_id=%s request_id=%r", question_id, request_id)

        try:
            answer = question_service.wait_for_answer(question_id=question_id)
        except TimeoutError as e:
            logger.error("[ask_user] Timeout waiting for answer. question_id=%s error=%s", question_id, e)
            logger.debug("[ask_user] timeout exception details", exc_info=True)
            return make_tool_error(
                error_code="ask_user_timeout",
                message=f"ask_user timed out after {timeout_seconds:.1f}s (question_id={question_id}).",
                abort_policy="abort_task",
                retryable=False,
                details={
                    "question_id": question_id,
                    "timeout_seconds": timeout_seconds,
                    "request_id": request_id if isinstance(request_id, str) else None,
                },
            )
        except Exception as e:
            logger.error("[ask_user] Failed waiting for answer. question_id=%s error=%s", question_id, e)
            logger.debug("[ask_user] wait exception details", exc_info=True)
            return make_tool_error(
                error_code="ask_user_wait_failed",
                message=f"ask_user failed while waiting for response: {e}",
                abort_policy="abort_task",
                retryable=False,
                details={"question_id": question_id},
            )

        return ToolResult(
            result_type='ask_user_response',
            content=answer,
            data={
                "question": question,
                "question_id": question_id,
                "answer": answer,
                "request_id": request_id if isinstance(request_id, str) else None,
            },
            data_list=[{'question_id': question_id, 'answer': answer}],
        )

    def _emit_question_to_user(
        self,
        *,
        question: str,
        question_id: str,
        request_id: str | None,
        room_id: str | None,
        reply_to: dict | None,
        user_identity: str | None,
    ) -> None:
        channel_type = ""
        if isinstance(reply_to, dict):
            channel_type = str(reply_to.get("type") or "").strip().lower()

        if channel_type == "slack":
            self._emit_question_to_slack(
                question=question,
                question_id=question_id,
                request_id=request_id,
                channel_id=str(reply_to.get("channel_id") or "").strip(),
            )
            return

        if channel_type == "twilio_sms":
            self._emit_question_to_sms(
                question=question,
                question_id=question_id,
                request_id=request_id,
                to_number=str(reply_to.get("to") or "").strip(),
                from_number=str(reply_to.get("from") or "").strip(),
            )
            return

        # No explicit reply_to channel — default to socketio in the ask_user's
        # room. ask_user is inherently a user-interaction tool; a room_id means
        # "show this here." Without this, dayflow-dispatched tasks (which have
        # no inbound user message to pin a reply_to from) silently fail at
        # emi_event_relay's "no reply_to resolved" check → 5-min timeout → abort.
        effective_reply_to = reply_to
        if not (isinstance(effective_reply_to, dict) and effective_reply_to.get("type")):
            if isinstance(room_id, str) and room_id.strip():
                effective_reply_to = {"type": "socketio", "room_id": room_id.strip()}

        widget = {
            "data_type": "ask_user",
            "question": question,
            "question_id": question_id,
            "request_id": request_id,
            "room_id": room_id,
            "reply_to": effective_reply_to,
            "user_identity": user_identity,
        }
        msg = UserMessage(
            data_type="user_msg",
            sender="ask_user_tool",
            receiver=None,
            timestamp=datetime.now(timezone.utc),
            id=str(uuid.uuid4()),
            request_id=request_id,
            role="assistant",
            user_message_data=UserMessageData(
                feed=question,
                chat=question,
                widget_data=[widget],
                tts=False,
            ),
            metadata={"reply_to": effective_reply_to} if isinstance(effective_reply_to, dict) else None,
        )
        msg.event_topic = "socket_emit"
        DI.event_hub.publish(msg)

    @staticmethod
    def _format_channel_question_text(*, question: str, question_id: str, request_id: str | None) -> str:
        request_token = request_id if isinstance(request_id, str) and request_id.strip() else "-"
        return (
            f"{question}\n\n"
            f"Reply naturally.\n"
            f"If I cannot deterministically match your reply, use:\n"
            f"ANSWER {question_id} {request_token} <your answer>"
        )

    def _emit_question_to_slack(self, *, question: str, question_id: str, request_id: str | None, channel_id: str) -> None:
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("Slack ask_user requires non-empty reply_to.channel_id.")
        from app.assistant.lib.core_tools.slack.slack import SlackTool

        text = self._format_channel_question_text(
            question=question,
            question_id=question_id,
            request_id=request_id,
        )
        result = SlackTool().handle_send_message({"channel_id": channel_id.strip(), "text": text})
        logger.info("[ask_user] Slack question emitted channel_id=%s content=%s", channel_id, getattr(result, "content", ""))

    def _emit_question_to_sms(
        self,
        *,
        question: str,
        question_id: str,
        request_id: str | None,
        to_number: str,
        from_number: str,
    ) -> None:
        if not isinstance(to_number, str) or not to_number.strip():
            raise ValueError("SMS ask_user requires non-empty reply_to.to.")
        if not isinstance(from_number, str) or not from_number.strip():
            raise ValueError("SMS ask_user requires non-empty reply_to.from.")
        from app.services.twilio_sms import TwilioSmsService

        text = self._format_channel_question_text(
            question=question,
            question_id=question_id,
            request_id=request_id,
        )
        sid = TwilioSmsService().send_sms(
            to_number=to_number.strip(),
            from_number=from_number.strip(),
            body=text,
        )
        logger.info("[ask_user] SMS question emitted sid=%s to=%s", sid, to_number)


def get_tool_class():
    return AskUserTool
