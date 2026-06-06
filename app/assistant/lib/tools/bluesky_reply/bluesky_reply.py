"""bluesky_reply — reply to a post chosen by ref (ID-anchored).

The planner passes ``post_ref`` (from bluesky_timeline) + ``text``. The reply record
is BUILT from the selected ref's canonical {uri, cid, thread-root}; the model never
types a URI, so the reply cannot land on a different post (the original bug). The
result echoes back what was replied to AND with what, so a wrong-ref selection is
visible to the critic instead of silently shipping.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.bluesky.bluesky_client import BlueskyError, create_record, now_iso
from app.assistant.lib.core_tools.bluesky.bluesky_core import build_reply_record, target_from_ref
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.bluesky_timeline.bluesky_timeline import TIMELINE_REF_KEY
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

# atproto post text limit is 300 graphemes; len() in code units is a safe-enough guard.
_MAX_LEN = 300


class BlueskyReply(BaseTool):
    def __init__(self):
        super().__init__("bluesky_reply")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        ref = str(args.get("post_ref") or "").strip()
        text = str(args.get("text") or "").strip()

        if not text:
            return make_tool_error(
                error_code="invalid_arguments",
                message="bluesky_reply: `text` is required.",
                abort_policy="abort_tool", retryable=False, details={"post_ref": ref},
            )
        if len(text) > _MAX_LEN:
            return make_tool_error(
                error_code="invalid_arguments",
                message=f"bluesky_reply: text is {len(text)} chars; Bluesky limit is {_MAX_LEN}.",
                abort_policy="abort_tool", retryable=False, details={"length": len(text)},
            )

        ref_map = DI.global_blackboard.get_state_value(TIMELINE_REF_KEY)
        try:
            target = target_from_ref(ref_map, ref)
        except KeyError as e:
            return make_tool_error(
                error_code="unknown_post_ref",
                message=f"bluesky_reply: {e}",
                abort_policy="abort_tool", retryable=False, details={"post_ref": ref},
            )

        record = build_reply_record(text, target, created_at=now_iso())
        try:
            resp = create_record("app.bsky.feed.post", record)
        except BlueskyError as e:
            return make_tool_error(
                error_code="bluesky_error",
                message=f"bluesky_reply: {e}",
                abort_policy="abort_tool", retryable=True, details={"post_ref": ref},
            )

        reply_uri = resp.get("uri")
        target_text = (target.get("text") or "")[:140]
        content = (
            f'Replied to {ref} (@{target.get("author")}: "{target_text}")\n'
            f'with: "{text}"\n'
            f"new post uri: {reply_uri}"
        )
        logger.info("bluesky_reply: replied to %s (%s)", ref, target.get("uri"))
        return ToolResult(
            result_type="bluesky_reply",
            content=content,
            data={"reply_uri": reply_uri, "in_reply_to_ref": ref, "in_reply_to_uri": target.get("uri")},
        )


def get_tool_class():
    return BlueskyReply
