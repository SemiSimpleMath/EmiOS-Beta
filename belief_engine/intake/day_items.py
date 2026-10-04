"""One day's inputs, with provenance kept from the timeline to the belief.

The day's entries (the merged timeline plus the assistant's replies) are numbered once, by
`day_entries`. The insights writer reads them numbered and cites the entries each insight rests on;
the archive step stores those entries on the insight (`sources`: number, source ref, the entry
itself). The belief extractor reads each insight with exactly its own sources (`insights`), and each
belief cites entries from its insight's sources, restored to their source refs by code.
Owner, 2026-10-03: "We should give insight + provenance messages for the insight. Everything needs
to have provenance." Until then the insights carried only quoted evidence strings, and the extractor
read the whole day's timeline beside them with nothing linking an insight to its messages.
"""
from __future__ import annotations

import json
from pathlib import Path

def day_dir(day: str) -> Path:
    """The day's folder, where the daily pipeline writes it (one resolver: it follows EMI_DATA_DIR)."""
    from app.assistant.pipelines.context import _day_context_dir
    return _day_context_dir(day)


def available_days() -> list[str]:
    """Every day that has both an insights file and a timeline, in date order."""
    from app.assistant.pipelines.context import _day_context_root_dir
    root = _day_context_root_dir()
    return sorted(p.parent.name for p in root.glob("*/*/*/timeline_merged.json")
                  if (p.parent / "resource_daily_insights.json").exists())


def entry_ref(entry: dict) -> str | None:
    """The source ref of one day entry: the message or ticket it is. A context entry (AFK, an activity)
    has no id of its own."""
    kind = entry.get("type")
    if kind in ("chat", "assistant") and entry.get("message_id"):
        return f"message:{entry['message_id']}"
    if kind == "ticket" and entry.get("ticket_id"):
        return f"ticket:{entry['ticket_id']}"
    return None


def day_entries(day: str) -> tuple[dict, dict[int, dict]]:
    """The day's merged timeline with the assistant's replies added, every entry numbered `n` in time
    order: (the timeline document the insights writer reads, n -> the entry)."""
    doc = json.loads((day_dir(day) / "timeline_merged.json").read_text(encoding="utf-8"))
    raw = sorted((doc.get("timeline") or []) + assistant_turns(day), key=lambda e: e.get("start_time_utc") or "")
    numbered = [{"n": n, **e} for n, e in enumerate(raw, start=1)]
    return {**doc, "timeline": numbered}, {e["n"]: e for e in numbered}


def insights(day: str) -> tuple[list[dict], dict[int, dict]]:
    """The day's insights, each with the entries it rests on, for the belief extractor:
    ([{n, insight, scope, sources: [items]}], entry n -> provenance code restores). Raises for an insight
    stored without sources: it was written before insights kept their provenance."""
    data = json.loads((day_dir(day) / "resource_daily_insights.json").read_text(encoding="utf-8"))
    out, provenance = [], {}
    for n, a in enumerate(data.get("actionable_information") or [], start=1):
        if not a.get("sources"):
            raise ValueError(f"{day} insight {n} has no sources: it was written before insights kept their "
                             "provenance; run the insights writer for that day again")
        items = []
        for src in a["sources"]:
            item = {"n": src["n"], **_extractor_item(src["entry"])}
            items.append(item)
            provenance[src["n"]] = {"item": item, "source_ref": src["ref"],
                                    "kind": "said" if item.get("said") else
                                            ("assistant" if item.get("assistant_said") else "did")}
        out.append({"n": n, "insight": a.get("fact_summary"), "scope": a.get("temporal_scope") or "chronic",
                    "sources": items})
    return out, provenance


def _extractor_item(e: dict) -> dict:
    """One cited entry as the extractor reads it: what was said, by whom, or what was done."""
    kind = e.get("type")
    if kind == "chat":
        return {"type": "chat message", "time": e.get("start_time_local"), "said": e.get("message")}
    if kind == "assistant":
        return {"type": "assistant reply", "time": e.get("start_time_local"), "assistant_said": e.get("message")}
    if kind == "ticket":
        return _ticket(e) or {"type": "prompt / ticket", "time": e.get("start_time_local"), "prompt": e.get("title"),
                              "prompt_text": e.get("message"), "response": e.get("state")}
    return {"type": kind or "entry", "time": e.get("start_time_local"), "until": e.get("end_time_local"),
            "label": e.get("label")}


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
