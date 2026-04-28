from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class QuestionService:
    """
    Canonical question request/resolve contract.

    Blocking wait is supported (current manager model), but bounded by timeout.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}

    def request_question(
        self,
        *,
        text: str,
        request_id: str | None,
        room_id: str | None,
        reply_to: dict[str, Any] | None,
        user_identity: str | None,
        timeout_seconds: float,
    ) -> str:
        q_text = str(text or "").strip()
        if not q_text:
            raise ValueError("Question text must be non-empty.")
        if not isinstance(timeout_seconds, (int, float)) or float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be > 0.")

        question_id = str(uuid.uuid4())
        rec = {
            "question_id": question_id,
            "text": q_text,
            "request_id": str(request_id).strip() if isinstance(request_id, str) and request_id.strip() else None,
            "room_id": str(room_id).strip() if isinstance(room_id, str) and room_id.strip() else None,
            "reply_to": dict(reply_to) if isinstance(reply_to, dict) else None,
            "user_identity": str(user_identity).strip() if isinstance(user_identity, str) and user_identity.strip() else None,
            "timeout_seconds": float(timeout_seconds),
            "created_at": time.time(),
            "event": threading.Event(),
            "answer": None,
            "status": "pending",
            "cancel_reason": None,
        }
        with self._lock:
            self._pending[question_id] = rec
        logger.info(
            "[QuestionService] requested question_id=%s request_id=%r room_id=%r user_identity=%r timeout=%.1fs",
            question_id,
            rec["request_id"],
            rec["room_id"],
            rec["user_identity"],
            rec["timeout_seconds"],
        )
        return question_id

    def wait_for_answer(self, *, question_id: str) -> str:
        qid = str(question_id or "").strip()
        if not qid:
            raise ValueError("question_id is required.")
        with self._lock:
            rec = self._pending.get(qid)
        if not isinstance(rec, dict):
            raise RuntimeError(f"Unknown question_id: {qid}")

        event = rec.get("event")
        if not isinstance(event, threading.Event):
            raise RuntimeError(f"Invalid pending record for question_id={qid}")
        timeout_s = float(rec.get("timeout_seconds") or 0)
        if timeout_s <= 0:
            raise RuntimeError(f"Invalid timeout for question_id={qid}")

        has_answer = event.wait(timeout=timeout_s)
        with self._lock:
            final = self._pending.pop(qid, None)
        if not isinstance(final, dict):
            raise RuntimeError(f"Pending question disappeared: {qid}")

        status = str(final.get("status") or "").strip().lower()
        if not has_answer and status == "pending":
            raise TimeoutError(f"Question timed out after {timeout_s:.1f}s (question_id={qid})")
        if status == "cancelled":
            reason = str(final.get("cancel_reason") or "cancelled").strip()
            raise RuntimeError(f"Question cancelled (question_id={qid}): {reason}")
        if status != "answered":
            raise RuntimeError(f"Question ended in invalid status '{status}' (question_id={qid})")

        answer = final.get("answer")
        if not isinstance(answer, str):
            raise RuntimeError(f"Question answered without string payload (question_id={qid})")
        return answer

    def resolve_question(
        self,
        *,
        question_id: str,
        answer: str,
        request_id: str | None,
        room_id: str | None,
        reply_to: dict[str, Any] | None,
        user_identity: str | None,
    ) -> None:
        qid = str(question_id or "").strip()
        if not qid:
            raise ValueError("question_id is required.")
        ans = str(answer or "").strip()
        if not ans:
            raise ValueError("answer must be a non-empty string.")
        with self._lock:
            rec = self._pending.get(qid)
            if not isinstance(rec, dict):
                raise RuntimeError(f"Unknown or expired question_id: {qid}")
            self._validate_binding(
                rec=rec,
                request_id=request_id,
                room_id=room_id,
                reply_to=reply_to,
                user_identity=user_identity,
            )
            rec["answer"] = ans
            rec["status"] = "answered"
            event = rec.get("event")
            if not isinstance(event, threading.Event):
                raise RuntimeError(f"Invalid pending record for question_id={qid}")
            event.set()
        logger.info("[QuestionService] resolved question_id=%s", qid)

    def resolve_single_pending_by_binding(
        self,
        *,
        answer: str,
        request_id: str | None,
        room_id: str | None,
        reply_to: dict[str, Any] | None,
        user_identity: str | None,
    ) -> str | None:
        """
        Resolve exactly one pending question that matches binding context.

        Returns resolved question_id when exactly one match exists.
        Returns None when no matching pending question exists.
        Raises RuntimeError when multiple matches exist (ambiguous).
        """
        ans = str(answer or "").strip()
        if not ans:
            raise ValueError("answer must be a non-empty string.")

        with self._lock:
            matches: list[tuple[str, dict[str, Any]]] = []
            for qid, rec in self._pending.items():
                status = str(rec.get("status") or "").strip().lower()
                if status != "pending":
                    continue
                try:
                    self._validate_binding(
                        rec=rec,
                        request_id=request_id,
                        room_id=room_id,
                        reply_to=reply_to,
                        user_identity=user_identity,
                    )
                except Exception as e:
                    logger.debug("Skipping question %s during binding validation: %s", qid, e, exc_info=True)
                    continue
                matches.append((qid, rec))

            if not matches:
                return None
            if len(matches) > 1:
                ids = [qid for qid, _ in matches]
                raise RuntimeError(
                    f"Multiple pending questions match this binding context: {ids}. "
                    "Use explicit question_id answer format to disambiguate."
                )

            qid, rec = matches[0]
            rec["answer"] = ans
            rec["status"] = "answered"
            event = rec.get("event")
            if not isinstance(event, threading.Event):
                raise RuntimeError(f"Invalid pending record for question_id={qid}")
            event.set()

        logger.info("[QuestionService] resolved by binding question_id=%s", qid)
        return qid

    def cancel_question(self, *, question_id: str, reason: str | None = None) -> None:
        qid = str(question_id or "").strip()
        if not qid:
            raise ValueError("question_id is required.")
        with self._lock:
            rec = self._pending.get(qid)
            if not isinstance(rec, dict):
                raise RuntimeError(f"Unknown or expired question_id: {qid}")
            if str(rec.get("status") or "").strip().lower() != "pending":
                raise RuntimeError(f"Question is not pending: {qid}")
            rec["status"] = "cancelled"
            rec["cancel_reason"] = str(reason or "cancelled").strip()
            event = rec.get("event")
            if not isinstance(event, threading.Event):
                raise RuntimeError(f"Invalid pending record for question_id={qid}")
            event.set()
        logger.info("[QuestionService] cancelled question_id=%s reason=%r", qid, reason)

    @staticmethod
    def _normalize_reply_to(raw: dict[str, Any] | None) -> tuple[str | None, str | None]:
        if not isinstance(raw, dict):
            return None, None
        rtype = str(raw.get("type") or "").strip().lower() or None
        target = str(raw.get("room") or raw.get("to") or raw.get("channel_id") or "").strip() or None
        return rtype, target

    def _validate_binding(
        self,
        *,
        rec: dict[str, Any],
        request_id: str | None,
        room_id: str | None,
        reply_to: dict[str, Any] | None,
        user_identity: str | None,
    ) -> None:
        expected_request_id = rec.get("request_id")
        provided_request_id = str(request_id).strip() if isinstance(request_id, str) and request_id.strip() else None
        if expected_request_id and provided_request_id != expected_request_id:
            raise RuntimeError(
                f"request_id mismatch while resolving question: expected={expected_request_id!r} got={provided_request_id!r}"
            )

        expected_room_id = rec.get("room_id")
        provided_room_id = str(room_id).strip() if isinstance(room_id, str) and room_id.strip() else None
        if expected_room_id and provided_room_id != expected_room_id:
            raise RuntimeError(
                f"room_id mismatch while resolving question: expected={expected_room_id!r} got={provided_room_id!r}"
            )

        expected_reply_type, expected_reply_target = self._normalize_reply_to(rec.get("reply_to"))
        provided_reply_type, provided_reply_target = self._normalize_reply_to(reply_to)
        if expected_reply_type and provided_reply_type != expected_reply_type:
            raise RuntimeError(
                f"reply_to type mismatch while resolving question: expected={expected_reply_type!r} got={provided_reply_type!r}"
            )
        if expected_reply_target and provided_reply_target != expected_reply_target:
            raise RuntimeError(
                f"reply_to target mismatch while resolving question: expected={expected_reply_target!r} got={provided_reply_target!r}"
            )

        expected_user = rec.get("user_identity")
        provided_user = str(user_identity).strip() if isinstance(user_identity, str) and user_identity.strip() else None
        if expected_user and provided_user != expected_user:
            raise RuntimeError(
                f"user_identity mismatch while resolving question: expected={expected_user!r} got={provided_user!r}"
            )
