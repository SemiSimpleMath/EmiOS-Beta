from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.pipeline_state import get_resume_target, set_resume_target
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class SummaryContextNode(ControlNode):
    """
    Context compaction node for long-running manager loops.

    - Marks older non-critical messages as context-suppressed.
    - Pins high-value older messages.
    - Adds one compact summary message that survives suppression.
    - Resumes to the delegator-provided resume target.
    """

    DEFAULT_THRESHOLD_MESSAGES = 140
    DEFAULT_KEEP_RECENT_MESSAGES = 60
    DEFAULT_MAX_SUMMARY_LINES = 18
    DEFAULT_MAX_SUMMARY_CHARS = 1800
    DEFAULT_PIN_KEYWORDS = (
        "must",
        "important",
        "critical",
        "constraint",
        "deadline",
        "error",
        "failed",
        "blocked",
        "cannot",
        "don't",
        "do not",
        "never",
        "always",
    )
    DEFAULT_RESUME_FALLBACK = "shared::tool_arguments"

    def action_handler(self, message: Message):
        try:
            self.blackboard.update_state_value("next_agent", None)
        except Exception as e:
            logger.error("[%s] Failed clearing next_agent at start: %s", self.name, e)
            logger.debug("[%s] clear next_agent exception details", self.name, exc_info=True)

        threshold = self._read_int_state("summary_context_threshold_messages", self.DEFAULT_THRESHOLD_MESSAGES)
        keep_recent = self._read_int_state("summary_context_keep_recent_messages", self.DEFAULT_KEEP_RECENT_MESSAGES)
        max_lines = self._read_int_state("summary_context_max_summary_lines", self.DEFAULT_MAX_SUMMARY_LINES)
        max_chars = self._read_int_state("summary_context_max_summary_chars", self.DEFAULT_MAX_SUMMARY_CHARS)
        pin_keywords = self._read_pin_keywords()

        if threshold <= 0:
            raise ValueError("summary_context_threshold_messages must be > 0")
        if keep_recent <= 0:
            raise ValueError("summary_context_keep_recent_messages must be > 0")
        if max_lines <= 0:
            raise ValueError("summary_context_max_summary_lines must be > 0")
        if max_chars <= 0:
            raise ValueError("summary_context_max_summary_chars must be > 0")

        messages = list(self.blackboard.get_messages() or [])
        eligible = [m for m in messages if self._is_eligible_message(m)]
        if len(eligible) <= threshold:
            logger.debug("[%s] Context below threshold (%s <= %s); no compaction.", self.name, len(eligible), threshold)
            self._resume()
            self.blackboard.update_state_value("last_agent", self.name)
            return

        old = eligible[:-keep_recent] if len(eligible) > keep_recent else []
        if not old:
            self._resume()
            self.blackboard.update_state_value("last_agent", self.name)
            return

        pinned_count = 0
        suppressed_count = 0
        suppressed_snippets: list[str] = []

        for m in old:
            try:
                if self._should_pin(m, pin_keywords):
                    self._set_meta_flag(m, "context_pinned", True)
                    self._set_meta_flag(m, "context_suppressed", False)
                    pinned_count += 1
                else:
                    self._set_meta_flag(m, "context_suppressed", True)
                    self._set_meta_flag(m, "context_suppressed_by", self.name)
                    suppressed_count += 1
                    snippet = self._snippet_for_summary(m)
                    if snippet:
                        suppressed_snippets.append(snippet)
            except Exception as e:
                logger.error("[%s] Failed processing message for context compaction: %s", self.name, e)
                logger.debug("[%s] per-message compaction exception details", self.name, exc_info=True)
                raise

        if suppressed_count > 0 and suppressed_snippets:
            summary_text = self._build_summary_text(
                snippets=suppressed_snippets,
                max_lines=max_lines,
                max_chars=max_chars,
            )
            summary_msg = Message(
                data_type="agent_result",
                sub_data_type=["context_summary", "history_summary"],
                sender=self.name,
                receiver="Blackboard",
                role="assistant",
                content=summary_text,
                metadata={
                    "context_pinned": True,
                    "context_summary": {
                        "pinned_messages": pinned_count,
                        "suppressed_messages": suppressed_count,
                        "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
            self.blackboard.add_msg(summary_msg)

        self.blackboard.update_state_value("context_summary_last_run_count", len(eligible))
        self.blackboard.update_state_value("context_summary_last_run_pinned", pinned_count)
        self.blackboard.update_state_value("context_summary_last_run_suppressed", suppressed_count)

        logger.info(
            "[%s] Context compaction complete. eligible=%s pinned=%s suppressed=%s",
            self.name,
            len(eligible),
            pinned_count,
            suppressed_count,
        )

        self._resume()
        self.blackboard.update_state_value("last_agent", self.name)

    @staticmethod
    def _is_eligible_message(m: Any) -> bool:
        data_type = str(getattr(m, "data_type", "") or "").strip().lower()
        if not data_type:
            return False
        if data_type in {"agent_activation"}:
            return False
        sub = {str(x).strip().lower() for x in (getattr(m, "sub_data_type", []) or []) if isinstance(x, str)}
        if "history_summary" in sub and "context_summary" not in sub:
            return False
        return True

    @staticmethod
    def _get_meta(m: Any) -> dict[str, Any]:
        meta = getattr(m, "metadata", None)
        if isinstance(meta, dict):
            return meta
        meta = {}
        setattr(m, "metadata", meta)
        return meta

    def _set_meta_flag(self, m: Any, key: str, value: Any) -> None:
        meta = self._get_meta(m)
        meta[key] = value
        m.metadata = meta

    def _should_pin(self, m: Any, pin_keywords: tuple[str, ...]) -> bool:
        meta = self._get_meta(m)
        if bool(meta.get("context_pinned", False)):
            return True
        if bool(meta.get("safety_critical", False)):
            return True

        data_type = str(getattr(m, "data_type", "") or "").strip().lower()
        if data_type in {"tool_result_summary"}:
            return True
        if data_type == "agent_result":
            sub = {str(x).strip().lower() for x in (getattr(m, "sub_data_type", []) or []) if isinstance(x, str)}
            if "context_summary" in sub:
                return True

        text = str(getattr(m, "content", "") or "").strip().lower()
        if not text:
            return False
        return any(k in text for k in pin_keywords)

    @staticmethod
    def _snippet_for_summary(m: Any) -> str:
        sender = str(getattr(m, "sender", "") or "").strip() or "Unknown"
        data_type = str(getattr(m, "data_type", "") or "").strip() or "message"
        content = str(getattr(m, "content", "") or "").strip()
        if not content:
            return ""
        compact = " ".join(content.split())
        if len(compact) > 180:
            compact = compact[:177] + "..."
        return f"{sender} [{data_type}]: {compact}"

    @staticmethod
    def _build_summary_text(*, snippets: list[str], max_lines: int, max_chars: int) -> str:
        lines = [
            "Context summary of earlier steps (older details were compacted):",
        ]
        for s in snippets[-max_lines:]:
            lines.append(f"- {s}")
        out = "\n".join(lines).strip()
        if len(out) > max_chars:
            out = out[: max_chars - 3].rstrip() + "..."
        return out

    def _resume(self) -> None:
        resume_target = get_resume_target(self.blackboard)
        if not isinstance(resume_target, str) or not resume_target.strip():
            resume_target = str(
                self.blackboard.get_state_value("summary_context_resume_fallback", self.DEFAULT_RESUME_FALLBACK)
                or self.DEFAULT_RESUME_FALLBACK
            ).strip()
        self.blackboard.update_state_value("next_agent", resume_target)
        try:
            set_resume_target(self.blackboard, None)
        except Exception as e:
            logger.error("[%s] Failed clearing resume target: %s", self.name, e)
            logger.debug("[%s] clear resume target exception details", self.name, exc_info=True)
            raise

    def _read_int_state(self, key: str, default: int) -> int:
        try:
            raw = self.blackboard.get_state_value(key, default)
        except Exception as e:
            logger.error("[%s] Failed reading state key '%s': %s", self.name, key, e)
            logger.debug("[%s] state read exception details for '%s'", self.name, key, exc_info=True)
            raw = default
        try:
            return int(raw)
        except Exception as e:
            logger.error("[%s] Invalid int value for '%s': %r (%s)", self.name, key, raw, e)
            logger.debug("[%s] state parse exception details for '%s'", self.name, key, exc_info=True)
            raise

    def _read_pin_keywords(self) -> tuple[str, ...]:
        try:
            raw = self.blackboard.get_state_value("summary_context_pin_keywords", list(self.DEFAULT_PIN_KEYWORDS))
        except Exception as e:
            logger.error("[%s] Failed reading summary_context_pin_keywords: %s", self.name, e)
            logger.debug("[%s] pin keywords read exception details", self.name, exc_info=True)
            raw = list(self.DEFAULT_PIN_KEYWORDS)
        if not isinstance(raw, list):
            raise ValueError("summary_context_pin_keywords must be a list[str]")
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            val = item.strip().lower()
            if val:
                out.append(val)
        if not out:
            return self.DEFAULT_PIN_KEYWORDS
        return tuple(out)
