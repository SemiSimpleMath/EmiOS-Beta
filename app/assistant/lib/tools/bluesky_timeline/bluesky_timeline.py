"""bluesky_timeline — fetch the home timeline as a compact, ref-anchored list.

Returns ``[b1] @handle · "text" [image/quote markers]`` lines (the only thing the
planner sees) and stashes the ref-map ``{b1: {uri, cid, root_*, image_*}}`` in the
global blackboard. bluesky_reply / bluesky_like / bluesky_hydrate_post resolve a
``post_ref`` against that map — so the planner never retypes a URI and content can
never land on the wrong post. Mirrors the Playwright snapshot/ref pattern.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.bluesky.bluesky_client import BlueskyError, get_session, get_timeline
from app.assistant.lib.core_tools.bluesky.bluesky_core import compact_timeline
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

# Blackboard key holding the latest timeline ref-map (latest-only, like
# playwright_latest_snapshot). bluesky_reply/like/hydrate read this.
TIMELINE_REF_KEY = "bluesky_latest_timeline"


class BlueskyTimeline(BaseTool):
    def __init__(self):
        super().__init__("bluesky_timeline")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        limit = int(args.get("limit") or 20)

        try:
            feed = get_timeline(limit)
            own = get_session()["handle"]  # cached from get_timeline's auth — no extra call
        except BlueskyError as e:
            return make_tool_error(
                error_code="bluesky_error",
                message=f"bluesky_timeline: {e}",
                abort_policy="abort_tool",
                retryable=True,
                details={"limit": limit},
            )

        # Exclude our own posts so the planner can't pick itself to reply to.
        rendered, ref_map = compact_timeline(feed, max_posts=limit, own_handle=own)
        DI.global_blackboard.update_state_value(TIMELINE_REF_KEY, ref_map)
        logger.info("bluesky_timeline: %d posts, refs=%s", len(ref_map), list(ref_map))

        return ToolResult(
            result_type="bluesky_timeline",
            content=rendered,
            data={"ref_count": len(ref_map), "refs": list(ref_map)},
        )


def get_tool_class():
    return BlueskyTimeline
