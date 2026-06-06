"""bluesky_post — publish an original top-level post.

No ref needed (an original post has no target). Auth is handled internally by the
shared client. For replies use bluesky_reply (ref-anchored); this tool is only for
brand-new posts.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.bluesky.bluesky_client import BlueskyError, create_record, now_iso
from app.assistant.lib.core_tools.bluesky.bluesky_core import build_post_record
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

_MAX_LEN = 300  # atproto post text limit (graphemes).


class BlueskyPost(BaseTool):
    def __init__(self):
        super().__init__("bluesky_post")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        text = str(args.get("text") or "").strip()

        if not text:
            return make_tool_error(
                error_code="invalid_arguments",
                message="bluesky_post: `text` is required.",
                abort_policy="abort_tool", retryable=False,
            )
        if len(text) > _MAX_LEN:
            return make_tool_error(
                error_code="invalid_arguments",
                message=f"bluesky_post: text is {len(text)} chars; Bluesky limit is {_MAX_LEN}.",
                abort_policy="abort_tool", retryable=False, details={"length": len(text)},
            )

        record = build_post_record(text, created_at=now_iso())
        try:
            resp = create_record("app.bsky.feed.post", record)
        except BlueskyError as e:
            return make_tool_error(
                error_code="bluesky_error",
                message=f"bluesky_post: {e}",
                abort_policy="abort_tool", retryable=True,
            )

        uri = resp.get("uri")
        logger.info("bluesky_post: posted %s", uri)
        return ToolResult(
            result_type="bluesky_post",
            content=f'Posted: "{text}"\nnew post uri: {uri}',
            data={"post_uri": uri},
        )


def get_tool_class():
    return BlueskyPost
