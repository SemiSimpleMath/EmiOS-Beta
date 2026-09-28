"""One day's inputs: the insights noticed and the numbered timeline items, with their source ids kept."""
from __future__ import annotations

import json
from pathlib import Path

from app.assistant.utils.path_utils import get_repo_root


def day_dir(day: str) -> Path:
    return get_repo_root() / "day_context" / day[:4] / day[5:7] / day


def available_days() -> list[str]:
    """Every day that has both an insights file and a timeline, in date order."""
    root = get_repo_root() / "day_context"
    return sorted(p.parent.name for p in root.glob("*/*/*/timeline_merged.json")
                  if (p.parent / "resource_daily_insights.json").exists())


def insights(day: str, chronic_only: bool = False, with_sentence: bool = False) -> list[dict]:
    """The day's insights as pointers: the writer's durability label and the evidence it noted.

    Defaults chosen on 2026-09-26 by running single days: every insight is passed (the writer's
    labels are too rough to be a gate — it files month-relevant decisions under `historical`), and
    the insight's sentence is left out (given the sentence, the extractor restates it, including
    the writer's overreach; given only the evidence, it writes from the user's words). The options
    remain for experiments.
    """
    data = json.loads((day_dir(day) / "resource_daily_insights.json").read_text(encoding="utf-8"))
    out = []
    for n, a in enumerate(data.get("actionable_information") or [], start=1):
        scope = a.get("temporal_scope") or "chronic"
        if chronic_only and scope != "chronic":
            continue
        entry = {"n": n, "scope": scope, "evidence_noted": a.get("evidence") or []}
        if with_sentence:
            entry["insight"] = a.get("fact_summary")
        out.append(entry)
    return out


# Text the ticket system writes into user_text for a button press. It is not the user's words.
_SYSTEM_REPLIES = (
    "User has completed this activity.",
    "User has acknowledged this advice but has not committed to action yet.",
    "User has committed to doing this right now.",
    "User has approved this tool action.",
    "User wants to be reminded later.",
    "User has declined this suggestion.",
    "User has indicated this suggestion is not applicable or not wanted.",
    "User has denied this tool action.",
)
_NOT_ACCEPTED_BUTTONS = {"skip", "no", "dismiss", "later"}
_NOT_ACCEPTED_STATES = {"dismissed", "snoozed"}


def _ticket(e: dict) -> dict | None:
    """A ticket as evidence, or None when it carries no signal.

    The signal in a ticket is the user NOT accepting it (declined, skipped, deferred, denied) or the
    user typing something. An accepted suggestion with no words says only that the suggestion was
    followed; an expired one with no answer is not a decision. Neither is kept.
    """
    text = (e.get("user_text") or "").strip()
    words = None
    if "Additional from user:" in text:
        words = text.split("Additional from user:", 1)[1].strip() or None
    elif text and not text.startswith(_SYSTEM_REPLIES):
        words = text
    button, state = e.get("user_action"), e.get("state")
    if button == "later" or state == "snoozed":
        response = "deferred"
    elif button in _NOT_ACCEPTED_BUTTONS or state in _NOT_ACCEPTED_STATES or text.startswith("User has denied"):
        response = "declined"
    else:
        response = None
    if response is None and words is None:
        return None
    item = {"type": "prompt / ticket", "time": e.get("start_time_local"),
            "prompt": e.get("title"), "prompt_text": e.get("message")}
    if response:
        item["response"] = response
    if words:
        item["said"] = words
    return item


def timeline_items(day: str) -> tuple[list[dict], dict[int, dict]]:
    """(items the model sees, n -> provenance code restores). The person's own messages and the
    prompts they declined, deferred, or answered in their own words. The model cites numbers only."""
    raw = json.loads((day_dir(day) / "timeline_merged.json").read_text(encoding="utf-8")).get("timeline") or []
    raw = sorted(raw + assistant_turns(day), key=lambda e: e.get("start_time_utc") or "")
    items, provenance = [], {}
    for e in raw:
        kind = e.get("type")
        if kind == "chat":
            item = {"type": "chat message", "time": e.get("start_time_local"), "said": e.get("message")}
            ref = f"message:{e.get('message_id')}" if e.get("message_id") else None
        elif kind == "assistant":
            item = {"type": "assistant reply", "time": e.get("start_time_local"), "assistant_said": e.get("message")}
            ref = f"message:{e.get('message_id')}" if e.get("message_id") else None
        elif kind == "ticket":
            item = _ticket(e)
            if item is None:
                continue
            ref = f"ticket:{e.get('ticket_id')}" if e.get("ticket_id") else None
        else:
            continue
        n = len(items) + 1
        item = {"n": n, **item}
        items.append(item)
        provenance[n] = {"item": item, "source_ref": ref,
                         "kind": "said" if item.get("said") else ("assistant" if item.get("assistant_said") else "did")}
    return items, provenance


def item_text(item: dict) -> str:
    return (item.get("said") or item.get("assistant_said") or item.get("response") or item.get("label") or item.get("prompt") or "")


# Where the assistant's conversational turns come from. The other master_room assistant sources
# (scheduler_reminder, subconscious_digest, room_summary::*) are reminders, digests and cluster
# summaries the assistant wrote to itself, not replies the person was answering.
_CONVERSATION_SOURCES = ("room_ui", "chat", "slack")


def assistant_turns(day: str) -> list[dict]:
    """The assistant's replies in the main room that day, in the timeline's shape. Read-only.

    The stored timeline carries only the user's side of the conversation (archive_daily_timeline
    filters role='user'), so a reply like "my favorite is poached" arrived with no trace of the
    list of breakfast places it answered (2026-09-27 review). The assistant's turns make the
    exchange readable; beliefs still rest on the user's words.

    The day is the pipeline's day — from the boundary hour (05:00 local) to the next — the same
    window the timeline's own items were archived with. unified_log_2026.timestamp is naive UTC
    written as "YYYY-MM-DD HH:MM:SS.ffffff"; the bounds are formatted the same way so the string
    comparison is a time comparison.
    """
    import sqlite3
    from datetime import datetime, timezone
    from belief_engine.db.paths import belief_db_path
    from app.assistant.pipelines.context import day_window_local
    from app.assistant.utils.time_utils import utc_to_local
    start_local, end_local = day_window_local(day)
    start, end = start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    conn = sqlite3.connect(Path(belief_db_path()).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, timestamp, message FROM unified_log_2026 WHERE room_id='master_room' AND role='assistant' "
            f"AND source IN ({','.join('?' * len(_CONVERSATION_SOURCES))}) "
            "AND timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (*_CONVERSATION_SOURCES, start.strftime(fmt), end.strftime(fmt))).fetchall()
    finally:
        conn.close()
    out = []
    for mid, ts, message in rows:
        message = _reply_text(message)
        if not message:
            continue
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        out.append({"type": "assistant", "start_time_local": utc_to_local(t).strftime("%Y-%m-%d %H:%M"),
                    "start_time_utc": t.astimezone(timezone.utc).isoformat(), "message_id": mid, "message": message})
    return out


def _reply_text(message: str | None) -> str:
    """The words the person saw. About 1% of assistant rows are a tool report the UI rendered from
    JSON: {"chat": <html>, "feed": <one-line summary>} — the feed is that report's text. Other JSON
    shapes are structured outputs that leaked into the room and carry nothing the person read."""
    import json, re
    text = (message or "").strip()
    if not text.startswith("{"):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict) and "feed" in data:
        return re.sub(r"<[^>]+>", "", str(data["feed"])).strip()
    return ""
