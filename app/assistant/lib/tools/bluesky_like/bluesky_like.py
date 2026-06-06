"""bluesky_like — like a post chosen by ref (ID-anchored).

The planner passes ``post_ref`` (from bluesky_timeline). The like record is built
from the selected ref's {uri, cid}; no URI is retyped, so the like cannot target the
wrong post. The result echoes back which post was liked.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.bluesky.bluesky_client import BlueskyError, create_record, now_iso
from app.assistant.lib.core_tools.bluesky.bluesky_core import build_like_record, target_from_ref
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.bluesky_timeline.bluesky_timeline import TIMELINE_REF_KEY
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class BlueskyLike(BaseTool):
    def __init__(self):
        super().__init__("bluesky_like")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        ref = str(args.get("post_ref") or "").strip()

        ref_map = DI.global_blackboard.get_state_value(TIMELINE_REF_KEY)
        try:
            target = target_from_ref(ref_map, ref)
        except KeyError as e:
            return make_tool_error(
                error_code="unknown_post_ref",
                message=f"bluesky_like: {e}",
                abort_policy="abort_tool", retryable=False, details={"post_ref": ref},
            )

        record = build_like_record(target, created_at=now_iso())
        try:
            resp = create_record("app.bsky.feed.like", record)
        except BlueskyError as e:
            return make_tool_error(
                error_code="bluesky_error",
                message=f"bluesky_like: {e}",
                abort_policy="abort_tool", retryable=True, details={"post_ref": ref},
            )

        target_text = (target.get("text") or "")[:140]
        content = f'Liked {ref} (@{target.get("author")}: "{target_text}")\nlike uri: {resp.get("uri")}'
        logger.info("bluesky_like: liked %s (%s)", ref, target.get("uri"))
        return ToolResult(
            result_type="bluesky_like",
            content=content,
            data={"like_uri": resp.get("uri"), "liked_ref": ref, "liked_uri": target.get("uri")},
        )


def get_tool_class():
    return BlueskyLike
