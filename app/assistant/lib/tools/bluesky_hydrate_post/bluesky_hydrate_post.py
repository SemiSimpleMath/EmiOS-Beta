"""bluesky_hydrate_post — open ONE chosen post (by ref) with its image, isolated.

Given a ``post_ref`` from the latest bluesky_timeline, returns that single post's
full text + image-alt, and downloads the image, emitting an ``[emi_image: <path>]``
marker so the prompt builder feeds the REAL pixels to a vision model. This is the
"select one, everything else goes away, see the image" step — it removes both the
timeline noise and the unviewable-image blind spot that drove the confabulation.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.bluesky.bluesky_client import BlueskyError, download_image
from app.assistant.lib.core_tools.bluesky.bluesky_core import target_from_ref
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.bluesky_timeline.bluesky_timeline import TIMELINE_REF_KEY
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class BlueskyHydratePost(BaseTool):
    def __init__(self):
        super().__init__("bluesky_hydrate_post")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        ref = str(args.get("post_ref") or "").strip()

        ref_map = DI.global_blackboard.get_state_value(TIMELINE_REF_KEY)
        try:
            target = target_from_ref(ref_map, ref)
        except KeyError as e:
            return make_tool_error(
                error_code="unknown_post_ref",
                message=f"bluesky_hydrate_post: {e}",
                abort_policy="abort_tool",
                retryable=False,
                details={"post_ref": ref},
            )

        parts = [
            f"Bluesky post {ref} by @{target.get('author') or '?'}:",
            "",
            target.get("text") or "(no text)",
        ]
        if target.get("has_image") and target.get("image_url"):
            alt = target.get("image_alt") or ""
            try:
                img_path = download_image(target["image_url"])
                parts.append("")
                parts.append(f"[image alt: {alt}]" if alt else "[image attached — no alt text]")
                # This marker is parsed by prompt_builder -> the real image is shown to a vision model.
                parts.append(f"[emi_image: {img_path}]")
            except BlueskyError as e:
                logger.warning("bluesky_hydrate_post: image download failed: %s", e)
                parts.append("")
                parts.append(f"[image present but could not be downloaded: {e}]")

        return ToolResult(
            result_type="bluesky_post",
            content="\n".join(parts),
            data={
                "post_ref": ref,
                "uri": target.get("uri"),
                "has_image": bool(target.get("has_image")),
            },
        )


def get_tool_class():
    return BlueskyHydratePost
