import os
from typing import List

from app.assistant.lib.core_tools.slack.slack import SlackTool
from app.assistant.utils.pydantic_classes import ToolMessage


def _get_messages() -> List[dict]:
    channel_id = os.getenv("SLACK_CHANNEL_ID")
    channel_id = os.getenv("SLACK_CHANNEL_ID", "C08AB0R54HM")
    if not channel_id:
        raise RuntimeError("SLACK_CHANNEL_ID env var is required")

    tool = SlackTool()
    msg = ToolMessage(
        tool_name="get_messages",
        tool_data={
            "tool_name": "get_messages",
            "arguments": {
                "channel_id": channel_id,
                "limit": 50,
            },
        },
    )
    result = tool.execute(msg)
    if getattr(result, "data_list", None):
        return result.data_list
    if isinstance(getattr(result, "data", None), list):
        return result.data
    return []


def main() -> int:
    messages = _get_messages()
    images = []
    for m in messages:
        imgs = m.get("images") if isinstance(m, dict) else None
        if isinstance(imgs, list):
            images.extend([p for p in imgs if isinstance(p, str) and p.strip()])

    if not images:
        print("No images downloaded from recent Slack messages.")
        return 0

    print("Downloaded images:")
    for p in images:
        print(f"- {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
