from __future__ import annotations

from app.assistant.lib.core_tools.slack.slack import SlackTool
from app.assistant.utils.slack_format import to_slack_mrkdwn


class SlackRoomTransport:
    @staticmethod
    def send_reply(*, channel_id: str, body: str, thread_ts: str = "") -> str:
        """
        Send reply to Slack channel/thread.

        ``body`` is run through ``to_slack_mrkdwn`` first — LLMs write
        standard Markdown by default (``**bold**``, ``[text](url)``)
        which Slack renders literally. This is the single choke point
        for outbound Slack text, so the conversion lives here.

        NOTE: SlackTool has its own safety gate and will dry-run unless
        configs/slack_room.json has allow_real_slack_send=true.
        """
        tool = SlackTool()
        arguments = {"channel_id": channel_id, "text": to_slack_mrkdwn(body)}
        if isinstance(thread_ts, str) and thread_ts.strip():
            arguments["thread_ts"] = thread_ts.strip()
        result = tool.handle_send_message(arguments)
        content = getattr(result, "content", None)
        return str(content or "")
