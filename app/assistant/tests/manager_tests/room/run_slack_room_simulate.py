import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace


# IDE-friendly defaults:
# Set USE_IDE_DEFAULTS=True and edit IDE_RUN_DEFAULTS below, then run the file.
# Set USE_IDE_DEFAULTS=False to use CLI flags normally.
USE_IDE_DEFAULTS = True
IDE_RUN_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8000,
    "room_id": str(os.environ.get("EMI_SLACK_DEFAULT_ROOM_ID", "justin")).strip() or "justin",
    "channel_id": str(os.environ.get("SLACK_CHANNEL_ID", "C08AB0R54HM")).strip() or "C08AB0R54HM",
    # Set speaker_name to "Jukka" or "Justin" for manual tests.
    "sender_name": "Justin",
    "speaker_name": "Justin",
    "sender_id": "U_SIM_TEST",
    "thread_ts": "",
    # When use_latest=True, body is ignored and latest Slack message is used.
    "body": "Emi is moon just cheese?",
    "use_latest": False,
    "history_count": 20,
    "send_reply": False,
}


def build_parser() -> argparse.ArgumentParser:
    default_room_id = str(os.environ.get("EMI_SLACK_DEFAULT_ROOM_ID", "justin")).strip() or "justin"
    default_channel_id = str(os.environ.get("SLACK_CHANNEL_ID", "C08AB0R54HM")).strip() or "C08AB0R54HM"
    parser = argparse.ArgumentParser(
        description="Send a simulated Slack inbound message to local room_manager."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Flask host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Flask port (default: 8000)")
    parser.add_argument("--room-id", default=default_room_id, help=f"Room id (default: {default_room_id})")
    parser.add_argument(
        "--channel-id",
        default=default_channel_id,
        help=(
            f"Slack channel id (default from env: {default_channel_id or '<empty>'}). "
            "If empty and --use-latest, server SLACK_CHANNEL_ID will be used."
        ),
    )
    parser.add_argument("--sender-name", default="Justin", help="Slack sender display name")
    parser.add_argument("--speaker-name", default="", help="Alias for sender-name")
    parser.add_argument("--sender-id", default="U_SIM_TEST", help="Slack sender id")
    parser.add_argument("--thread-ts", default="", help="Optional Slack thread_ts")
    parser.add_argument(
        "--body",
        default="",
        help="Inbound Slack message text (used with --manual)",
    )
    parser.add_argument(
        "--use-latest",
        dest="use_latest",
        action="store_true",
        default=True,
        help="Fetch latest real Slack user message from channel and use it as inbound body (default: on)",
    )
    parser.add_argument(
        "--manual",
        dest="use_latest",
        action="store_false",
        help="Use provided --body instead of pulling latest Slack message",
    )
    parser.add_argument(
        "--history-count",
        type=int,
        default=20,
        help="Load this many recent real Slack messages as context (default: 20)",
    )
    parser.add_argument(
        "--send-reply",
        action="store_true",
        help="Attempt outbound send (still safety-gated unless allow_real_slack_send=true in configs/slack_room.json)",
    )
    return parser


def main() -> int:
    if USE_IDE_DEFAULTS:
        args = SimpleNamespace(**IDE_RUN_DEFAULTS)
    else:
        args = build_parser().parse_args()
    url = f"http://{args.host}:{args.port}/slack/room/simulate"
    payload = {
        "channel_id": args.channel_id,
        "body": args.body,
        "room_id": args.room_id,
        "sender_name": args.sender_name,
        "speaker_name": args.speaker_name,
        "sender_id": args.sender_id,
        "thread_ts": args.thread_ts,
        "use_latest": bool(args.use_latest),
        "history_count": int(args.history_count),
        "send_reply": bool(args.send_reply),
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print("POST", url)
    print("Payload:")
    print(json.dumps(payload, indent=2))
    print("-" * 80)

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"HTTP {resp.status}")
            try:
                parsed = json.loads(body)
                print(json.dumps(parsed, indent=2))
            except Exception:
                print(body)
            return 0
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}")
        print(err_body)
        return 1
    except Exception as e:
        print("Request failed:", e)
        print(
            "Tip: start Flask first, then rerun this script. "
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

