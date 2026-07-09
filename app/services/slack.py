"""Slack token resolution.

All Slack sends go through `SlackTool` (`app/assistant/lib/core_tools/slack/`),
which honors the `allow_real_slack_send` safety gate. The old `SlackService`
class here was the EmiEventRelay's gate-bypassing direct sender; the relay now
delegates to OutboundChatPublisher → SlackRoomTransport → SlackTool (2026-07-08
delivery audit), which left it with zero callers.
"""
from __future__ import annotations

import os


def resolve_slack_token() -> str:
    """Resolve the Slack bot token from env (first non-empty wins):
    SLACK_TOKEN, EMI_SLACK_TOKEN, SLACK_BOT_TOKEN. Returns "" when unset."""
    for key in ("SLACK_TOKEN", "EMI_SLACK_TOKEN", "SLACK_BOT_TOKEN"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""
