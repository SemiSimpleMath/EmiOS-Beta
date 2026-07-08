"""Mint a pod from content provided in the call, and return its pod_id.

This is the persistence step the agent layer was missing (see the phantom-mint
diagnosis, 2026-06-13): it turns a structured request — "save this list as a
datapod" — into a durable ``pod_store`` row with a real ``pod_id`` that threads
forward to later turns and can be reopened with ``pod_fetch``. Mirrors
``mint_pod_from_path`` but takes in-prompt content instead of a filesystem path.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.pod_store import pod_utils
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

# Authored-content kinds this tool may mint. Freeform user/agent content ONLY —
# never identity/auth/secret kinds (those come from deterministic materializers,
# not an LLM tool). Registered in configs/pod_kinds.json.
_ALLOWED_KINDS = {"note"}
_DEFAULT_KIND = "note"


class MintPodTool(BaseTool):
    def __init__(self):
        super().__init__("mint_pod")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else tool_data
        if not isinstance(args, dict):
            return ToolResult(result_type="error", content="Missing arguments for mint_pod.")

        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        if not title:
            return ToolResult(result_type="error", content="mint_pod requires a non-empty `title`.")
        if not body:
            return ToolResult(result_type="error", content="mint_pod requires a non-empty `body`.")

        kind = str(args.get("kind") or _DEFAULT_KIND).strip() or _DEFAULT_KIND
        if kind not in _ALLOWED_KINDS:
            return ToolResult(
                result_type="error",
                content=f"mint_pod cannot mint kind '{kind}'. Allowed kinds: {sorted(_ALLOWED_KINDS)}.",
            )

        raw_tags = args.get("tags")
        tags = [str(t).strip() for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else []

        min_authority = args.get("min_authority")
        try:
            min_authority = int(min_authority) if min_authority is not None else None
        except (TypeError, ValueError):
            min_authority = None

        importance = args.get("importance")
        try:
            importance = float(importance) if importance is not None else None
            if importance is not None:
                importance = max(0.0, min(10.0, importance))
        except (TypeError, ValueError):
            importance = None

        # THE mint scope rule: a pod minted from a room context carries that
        # room_id; a caller without a room mints owner-only (None — readable
        # by the all-scope surfaces). The full scope_id string is an identity
        # token, not a pod scope — stamping it produced pods no room's
        # allowed_scopes could ever match.
        scope = getattr(tool_message, "scope_context", None)
        scope_id = None
        actor = None
        if scope is not None:
            scope_id = getattr(scope, "room_id", None) or None
            actor = getattr(scope, "actor_id", None)

        try:
            # The unified minter: one id builder, one tag assembly, one
            # write path (put()'s format/registry gate included).
            pod_id = pod_utils.mint_pod(
                kind=kind,
                one_liner=title,
                body=body,
                tags=tags,
                scope_id=scope_id,
                created_by=actor or "mint_pod",
                metadata={"source_kind": "manual_mint"},
                min_authority=min_authority,
                importance=importance,
            )
        except Exception as e:
            logger.exception("[mint_pod] failed to persist pod")
            return ToolResult(
                result_type="error",
                content=f"Failed to persist pod: {e}",
                data={"ok": False},
            )

        logger.info("[mint_pod] minted %s kind=%s scope=%s tags=%s", pod_id, kind, scope_id, tags)
        return ToolResult(
            result_type="mint_pod_result",
            content=(
                f"Minted pod {pod_id}\n"
                f"  kind:  {kind}\n"
                f"  title: {title}\n"
                f"  tags:  {tags}\n"
                "Echo this pod_id back to the user; pass it to pod_fetch to read it, "
                "or to send_email as pod_ids=[...]."
            ),
            data={
                "ok": True,
                "pod_id": pod_id,
                "kind": kind,
                "importance": importance,
                "min_authority": min_authority,
                "tags": tags,
            },
        )


def get_tool_class():
    return MintPodTool
