from __future__ import annotations

import re
from typing import Any, Callable, Dict

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class QuestionAnswerIngressService:
    @staticmethod
    def _parse_question_answer_message(body: str) -> tuple[str, str | None, str] | None:
        """
        Parse channel answer payload:
        ANSWER <question_id> <request_id|-> <answer text>
        """
        raw = str(body or "").strip()
        if not raw:
            return None
        match = re.match(
            r"^\s*ANSWER\s+([A-Za-z0-9\-]{8,80})\s+(\S+)\s+(.+)$",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        question_id = str(match.group(1) or "").strip()
        request_id_token = str(match.group(2) or "").strip()
        answer_text = str(match.group(3) or "").strip()
        if not question_id or not answer_text:
            return None
        request_id = None if request_id_token in {"-", "_", "none", "null"} else request_id_token
        return question_id, request_id, answer_text

    def try_resolve(
        self,
        *,
        request_id: str,
        room_id: str,
        room_surface: str,
        room_context_id: str,
        body: str,
        send_reply: bool,
        message_persistence_mode: str,
        include_dry_run: bool,
        user_identity: str,
        reply_to: Dict[str, Any],
        send_text: Callable[[str], Any],
        outbound_result_key: str,
        allow_binding_resolution: bool,
        build_short_circuit_response: Callable[..., Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        parsed_answer = self._parse_question_answer_message(body)
        if parsed_answer is not None:
            question_id, answer_request_id, answer_text = parsed_answer
            try:
                DI.question_service.resolve_question(
                    question_id=question_id,
                    answer=answer_text,
                    request_id=answer_request_id,
                    room_id=room_id,
                    reply_to=reply_to,
                    user_identity=user_identity,
                )
                ack = "Thanks - got your answer."
                outbound_result = None
                if send_reply:
                    try:
                        outbound_result = send_text(ack)
                    except Exception as e:
                        logger.error(
                            "Failed sending ask_user %s acknowledgment for room_id=%s: %s",
                            room_surface,
                            room_id,
                            e,
                        )
                        logger.debug("ask_user %s acknowledgment exception details", room_surface, exc_info=True)
                return build_short_circuit_response(
                    request_id=request_id,
                    room_id=room_id,
                    room_surface=room_surface,
                    room_context_id=room_context_id,
                    send_reply=send_reply,
                    reply_text=ack,
                    message_persistence_mode=message_persistence_mode,
                    unified_log_persistence_reason="ask_user_answer_resolution",
                    include_dry_run=include_dry_run,
                    extra_fields={
                        outbound_result_key: outbound_result,
                        "resolved_question": True,
                        "resolved_question_id": question_id,
                    },
                )
            except Exception as e:
                logger.error("Failed resolving ask_user answer from %s for room_id=%s: %s", room_surface, room_id, e)
                logger.debug("ask_user %s resolve exception details", room_surface, exc_info=True)

        if not allow_binding_resolution:
            return None

        try:
            resolved_question_id = DI.question_service.resolve_single_pending_by_binding(
                answer=body,
                request_id=None,
                room_id=room_id,
                reply_to=reply_to,
                user_identity=user_identity,
            )
            if isinstance(resolved_question_id, str) and resolved_question_id.strip():
                ack = "Thanks - got your answer."
                outbound_result = None
                if send_reply:
                    try:
                        outbound_result = send_text(ack)
                    except Exception as e:
                        logger.error(
                            "Failed sending ask_user %s acknowledgment for room_id=%s: %s",
                            room_surface,
                            room_id,
                            e,
                        )
                        logger.debug("ask_user %s acknowledgment exception details", room_surface, exc_info=True)
                return build_short_circuit_response(
                    request_id=request_id,
                    room_id=room_id,
                    room_surface=room_surface,
                    room_context_id=room_context_id,
                    send_reply=send_reply,
                    reply_text=ack,
                    message_persistence_mode=message_persistence_mode,
                    unified_log_persistence_reason="ask_user_answer_resolution",
                    include_dry_run=include_dry_run,
                    extra_fields={
                        outbound_result_key: outbound_result,
                        "resolved_question": True,
                        "resolved_question_id": resolved_question_id.strip(),
                    },
                )
        except Exception as e:
            message = str(e)
            if "Multiple pending questions match this binding context" in message:
                guidance = "I have multiple pending questions. Please reply with: ANSWER <question_id> <request_id|-> <your answer>"
                outbound_result = None
                if send_reply:
                    try:
                        outbound_result = send_text(guidance)
                    except Exception as send_err:
                        logger.error(
                            "Failed sending ask_user %s disambiguation guidance for room_id=%s: %s",
                            room_surface,
                            room_id,
                            send_err,
                        )
                        logger.debug("ask_user %s disambiguation exception details", room_surface, exc_info=True)
                return build_short_circuit_response(
                    request_id=request_id,
                    room_id=room_id,
                    room_surface=room_surface,
                    room_context_id=room_context_id,
                    send_reply=send_reply,
                    reply_text=guidance,
                    message_persistence_mode=message_persistence_mode,
                    unified_log_persistence_reason="ask_user_answer_ambiguous",
                    include_dry_run=include_dry_run,
                    extra_fields={
                        outbound_result_key: outbound_result,
                        "resolved_question": False,
                    },
                )
            logger.error(
                "Failed resolving natural-language ask_user answer from %s for room_id=%s: %s",
                room_surface,
                room_id,
                e,
            )
            logger.debug("ask_user %s natural-language resolve exception details", room_surface, exc_info=True)
        return None
