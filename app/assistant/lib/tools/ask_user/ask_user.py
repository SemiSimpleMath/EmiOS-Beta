from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from datetime import datetime, timezone
import time

logger = get_logger(__name__)

# Polling cadence for the ticket-based flow. Mirrors approval_gateway —
# 1s poll, total wall-clock bounded by request_context.ask_user_timeout_seconds.
_TICKET_POLL_INTERVAL_SECONDS = 1.0
_TICKET_VALID_HOURS = 1

# No answer within the window is the user's absence, not a tool failure — so it
# returns a SURVIVABLE error (abort_tool), never an abort_task that kills the
# calling manager. This text is the calling agent's entire instruction for the
# no-answer case; there is deliberately no matching guidance in the tool
# description or worker prompts.
_UNREACHABLE_MESSAGE = (
    "User could not be reached at this time. Decide whether to proceed on your "
    "best judgment; if their answer is essential, finish and state exactly what "
    "you still need from them. Asking again will not reach them."
)


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

        # Dispatch by reply_to channel. Slack and Twilio SMS keep their
        # existing QuestionService-based flow (those surfaces don't render
        # tickets yet). Everything else routes through a ticket — which the
        # global ticket UI surfaces in master_room regardless of the
        # calling chain's room_id, so a sub-manager invoked from dayflow
        # context (room_id="dayflow_orchestrator") can still reach the user.
        channel_type = ""
        if isinstance(reply_to, dict):
            channel_type = str(reply_to.get("type") or "").strip().lower()

        calling_agent = (
            (request_context.get("calling_agent") if isinstance(request_context, dict) else None)
            or "ask_user_tool"
        )

        if channel_type == "slack":
            return self._ask_via_slack(
                question=question,
                request_id=request_id if isinstance(request_id, str) else None,
                room_id=room_id if isinstance(room_id, str) else None,
                reply_to=reply_to,
                user_identity=user_identity if isinstance(user_identity, str) else None,
                timeout_seconds=timeout_seconds,
            )

        if channel_type == "twilio_sms":
            return self._ask_via_sms(
                question=question,
                request_id=request_id if isinstance(request_id, str) else None,
                room_id=room_id if isinstance(room_id, str) else None,
                reply_to=reply_to,
                user_identity=user_identity if isinstance(user_identity, str) else None,
                timeout_seconds=timeout_seconds,
            )

        return self._ask_via_ticket(
            question=question,
            request_id=request_id if isinstance(request_id, str) else None,
            calling_agent=str(calling_agent),
            calling_room_id=room_id if isinstance(room_id, str) else None,
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Ticket-based path (default — surfaces in master_room)
    # ------------------------------------------------------------------
    def _ask_via_ticket(
        self,
        *,
        question: str,
        request_id: str | None,
        calling_agent: str,
        calling_room_id: str | None,
        timeout_seconds: float,
    ) -> ToolResult:
        ticket_manager = DI.ticket_manager
        if ticket_manager is None:
            return make_tool_error(
                error_code="ask_user_ticket_unavailable",
                message="ask_user requires the ticket manager but it's not initialized.",
                abort_policy="abort_task",
                retryable=False,
                details={},
            )

        now = datetime.now(timezone.utc)
        title = question if len(question) <= 80 else question[:77].rstrip() + "..."
        ticket = ticket_manager.create_ticket(
            ticket_type="ask_user",
            suggestion_type="ask_user",
            title=title,
            message=question,
            action_type="ask_user",
            action_params={
                "calling_agent": calling_agent,
                "calling_room_id": calling_room_id,
                "request_id": request_id,
                "requested_at": now.isoformat(),
            },
            trigger_context={
                "source": f"ask_user:{calling_agent}",
                "calling_agent": calling_agent,
                "request_id": request_id,
                "room_id": calling_room_id,
                "time": now.isoformat(),
            },
            valid_hours=_TICKET_VALID_HOURS,
        )
        if not ticket:
            return make_tool_error(
                error_code="ask_user_ticket_create_failed",
                message="Failed to create ask_user ticket.",
                abort_policy="abort_task",
                retryable=False,
                details={"calling_agent": calling_agent},
            )

        ticket_id = ticket.ticket_id
        if not ticket_manager.mark_proposed(ticket_id):
            return make_tool_error(
                error_code="ask_user_ticket_propose_failed",
                message=f"Failed to propose ask_user ticket '{ticket_id}'.",
                abort_policy="abort_task",
                retryable=False,
                details={"ticket_id": ticket_id},
            )

        logger.info(
            "[ask_user] Ticket pending ticket_id=%s timeout=%.1fs calling_agent=%s",
            ticket_id, timeout_seconds, calling_agent,
        )

        elapsed = 0.0
        while elapsed < timeout_seconds:
            time.sleep(_TICKET_POLL_INTERVAL_SECONDS)
            elapsed += _TICKET_POLL_INTERVAL_SECONDS

            current = ticket_manager.get_ticket_by_id(ticket_id)
            if current is None:
                return make_tool_error(
                    error_code="ask_user_ticket_lost",
                    message=f"ask_user ticket disappeared: {ticket_id}",
                    abort_policy="abort_task",
                    retryable=False,
                    details={"ticket_id": ticket_id},
                )

            state = str(current.state or "").strip().lower()
            if state == "accepted":
                answer = str(current.user_text or "").strip()
                logger.info(
                    "[ask_user] Ticket answered ticket_id=%s answer_chars=%d",
                    ticket_id, len(answer),
                )
                return ToolResult(
                    result_type="ask_user_response",
                    content=answer,
                    data={
                        "question": question,
                        "ticket_id": ticket_id,
                        "answer": answer,
                        "request_id": request_id,
                    },
                    data_list=[{"ticket_id": ticket_id, "answer": answer}],
                )

            if state in ("dismissed", "rejected"):
                logger.info("[ask_user] Ticket dismissed ticket_id=%s state=%s", ticket_id, state)
                return make_tool_error(
                    error_code="ask_user_dismissed",
                    message=(
                        f"User dismissed the question (ticket {ticket_id}). "
                        "They chose not to answer — do not retry."
                    ),
                    abort_policy="abort_tool",
                    retryable=False,
                    details={"ticket_id": ticket_id, "state": state},
                )

            if state in ("expired", "failed"):
                return make_tool_error(
                    error_code="ask_user_timeout",
                    message=_UNREACHABLE_MESSAGE,
                    abort_policy="abort_tool",
                    retryable=False,
                    details={"ticket_id": ticket_id, "state": state},
                )

        # Polling loop exhausted — mark expired and return timeout.
        ticket_manager.mark_expired(ticket_id)
        logger.info(
            "[ask_user] Ticket timed out ticket_id=%s after %.1fs",
            ticket_id, timeout_seconds,
        )
        return make_tool_error(
            error_code="ask_user_timeout",
            message=_UNREACHABLE_MESSAGE,
            abort_policy="abort_tool",
            retryable=False,
            details={"ticket_id": ticket_id, "timeout_seconds": timeout_seconds},
        )

    # ------------------------------------------------------------------
    # Slack / SMS paths (existing QuestionService-based flow, unchanged)
    # ------------------------------------------------------------------
    def _ask_via_slack(
        self,
        *,
        question: str,
        request_id: str | None,
        room_id: str | None,
        reply_to: dict,
        user_identity: str | None,
        timeout_seconds: float,
    ) -> ToolResult:
        channel_id = str(reply_to.get("channel_id") or "").strip()
        if not channel_id:
            return make_tool_error(
                error_code="ask_user_slack_missing_channel",
                message="Slack ask_user requires non-empty reply_to.channel_id.",
                abort_policy="abort_tool",
                retryable=False,
                details={"reply_to": reply_to},
            )
        return self._inline_qa_flow(
            question=question,
            request_id=request_id,
            room_id=room_id,
            reply_to=reply_to,
            user_identity=user_identity,
            timeout_seconds=timeout_seconds,
            emitter=lambda q, qid: self._emit_question_to_slack(
                question=q, question_id=qid, request_id=request_id, channel_id=channel_id,
            ),
        )

    def _ask_via_sms(
        self,
        *,
        question: str,
        request_id: str | None,
        room_id: str | None,
        reply_to: dict,
        user_identity: str | None,
        timeout_seconds: float,
    ) -> ToolResult:
        to_number = str(reply_to.get("to") or "").strip()
        from_number = str(reply_to.get("from") or "").strip()
        if not to_number or not from_number:
            return make_tool_error(
                error_code="ask_user_sms_missing_addrs",
                message="SMS ask_user requires non-empty reply_to.to and reply_to.from.",
                abort_policy="abort_tool",
                retryable=False,
                details={"reply_to": reply_to},
            )
        return self._inline_qa_flow(
            question=question,
            request_id=request_id,
            room_id=room_id,
            reply_to=reply_to,
            user_identity=user_identity,
            timeout_seconds=timeout_seconds,
            emitter=lambda q, qid: self._emit_question_to_sms(
                question=q, question_id=qid, request_id=request_id,
                to_number=to_number, from_number=from_number,
            ),
        )

    def _inline_qa_flow(
        self,
        *,
        question: str,
        request_id: str | None,
        room_id: str | None,
        reply_to: dict,
        user_identity: str | None,
        timeout_seconds: float,
        emitter,
    ) -> ToolResult:
        question_service = DI.question_service
        question_id = question_service.request_question(
            text=question,
            request_id=request_id,
            room_id=room_id,
            reply_to=reply_to,
            user_identity=user_identity,
            timeout_seconds=timeout_seconds,
        )
        emitter(question, question_id)
        logger.info("[ask_user] Waiting for inline answer. question_id=%s request_id=%r", question_id, request_id)
        try:
            answer = question_service.wait_for_answer(question_id=question_id)
        except TimeoutError as e:
            logger.error("[ask_user] Inline timeout question_id=%s error=%s", question_id, e)
            return make_tool_error(
                error_code="ask_user_timeout",
                message=_UNREACHABLE_MESSAGE,
                abort_policy="abort_tool",
                retryable=False,
                details={
                    "question_id": question_id,
                    "timeout_seconds": timeout_seconds,
                    "request_id": request_id,
                },
            )
        except Exception as e:
            logger.error("[ask_user] Inline wait failed question_id=%s error=%s", question_id, e)
            return make_tool_error(
                error_code="ask_user_wait_failed",
                message=f"ask_user failed while waiting for response: {e}",
                abort_policy="abort_task",
                retryable=False,
                details={"question_id": question_id},
            )
        return ToolResult(
            result_type="ask_user_response",
            content=answer,
            data={
                "question": question,
                "question_id": question_id,
                "answer": answer,
                "request_id": request_id,
            },
            data_list=[{"question_id": question_id, "answer": answer}],
        )

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
