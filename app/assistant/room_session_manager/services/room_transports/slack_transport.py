from __future__ import annotations

from app.assistant.lib.core_tools.slack.slack import SlackTool


class SlackRoomTransport:
    @staticmethod
    def send_reply(*, channel_id: str, body: str, thread_ts: str = "") -> str:
        """
        Send reply to Slack channel/thread.
        NOTE: SlackTool has its own safety gate and will dry-run unless
        configs/slack_room.json has allow_real_slack_send=true.
        """
        tool = SlackTool()
        arguments = {"channel_id": channel_id, "text": body}
        if isinstance(thread_ts, str) and thread_ts.strip():
            arguments["thread_ts"] = thread_ts.strip()
        result = tool.handle_send_message(arguments)
        content = getattr(result, "content", None)
        return str(content or "")
