"""Brain events shown as the conversations they happened in.

An event is one message; what it means depends on the conversation around it. Before this, each
event carried `replying_to` — the latest assistant line in its room, however old — and readers saw
events as a flat list: in the Slack room every message, for three days, appeared to reply to one
days-old line.

Now each reader (gate, noticer) gets the events in groups, each under a line saying what it is:

- chat, by room, under the room's ROOM.md `description:`. Each event sits inside its whole
  conversation: the room's user and assistant turns, split wherever the room fell silent for
  CONVERSATION_GAP, walked back to where the conversation started and forward to where it ended.
- email, by account, under the account's `description` (configs/oauth_accounts.json). Each event
  sits inside its Gmail thread: every stored email of that thread, in time order.
- work, by work object: dayflow's reports on work attached to a concern (a finalizer judgment, the
  work's ending), in time order (subconscious/concern_feedback.py).

Nothing is cut. Events carry the reader's mark (the gate's E-label, the noticer's ref) and
optionally a note; the other turns are context only. Times are local, with a date header whenever
the day changes. Python prepares the structure; agents/shared/macros/conversations.j2 renders it
for every reader.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

CONVERSATION_GAP = timedelta(minutes=30)


def _utc(value: Any) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _fetch_turns(room_id: str, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """The room's user and assistant turns with start <= timestamp <= end, oldest first."""
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.models.base import get_session
    session = get_session()
    try:
        rows = (session.query(UnifiedLog2026)
                .filter(UnifiedLog2026.room_id == room_id, UnifiedLog2026.role.in_(("user", "assistant")),
                        UnifiedLog2026.timestamp >= start, UnifiedLog2026.timestamp <= end)
                .order_by(UnifiedLog2026.timestamp.asc()).all())
        out = []
        for r in rows:
            if r.role == "assistant":
                speaker = "assistant (subconscious)" if r.speaker_role == "subconscious" else "assistant"
            else:
                speaker = r.speaker_name or "user"
            out.append({"ref": f"message:{r.id}", "at": _utc(r.timestamp), "speaker": speaker,
                        "text": r.message or ""})
        return out
    finally:
        session.close()


def _describe(room_id: str) -> str:
    from app.assistant.rooms.room_resource_loader import load_room_description
    return load_room_description(room_id)


def _conversation_start(room_id: str, at: datetime) -> datetime:
    """Walk back from `at` while the room has a turn within CONVERSATION_GAP before it."""
    start = at
    while True:
        earlier = [t for t in _fetch_turns(room_id, start - CONVERSATION_GAP, start) if t["at"] < start]
        if not earlier:
            return start
        start = earlier[0]["at"]


def _conversation_end(room_id: str, at: datetime, now: datetime) -> datetime:
    """Walk forward from `at` while the room has a turn within CONVERSATION_GAP after it."""
    end = at
    while end < now:
        later = [t for t in _fetch_turns(room_id, end, end + CONVERSATION_GAP) if t["at"] > end]
        if not later:
            return end
        end = later[-1]["at"]
    return end


def _describe_account(account_id: str) -> str:
    from app.assistant.lib.google_auth.oauth_registry import get_description
    return get_description(account_id)


def _load_email(pod_id: str) -> Dict[str, Any]:
    from app.assistant.pod_store.email_pods import email_by_pod_id
    return email_by_pod_id(pod_id)


def _email_thread(account_id: str, thread_id: str) -> List[Dict[str, Any]]:
    from app.assistant.pod_store.email_pods import thread_emails
    return thread_emails(account_id, thread_id)


def _render_turns(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Local times, with the date on the first turn and wherever the day changes."""
    from app.assistant.utils.time_utils import utc_to_local
    day, out = None, []
    for t in turns:
        local = utc_to_local(t["at"].isoformat())
        today = local.strftime("%a %Y-%m-%d")
        out.append({"day": today if today != day else None, "time": local.strftime("%H:%M"),
                    "speaker": t["speaker"], "text": t["text"], "mark": t["mark"], "note": t["note"]})
        day = today
    return out


def _event_turn(e: Dict[str, Any]) -> Dict[str, Any]:
    return {"at": _utc(e["occurred_at"]), "speaker": e.get("speaker") or "user", "text": e["text"],
            "mark": e["mark"], "note": e.get("note")}


def _chat_groups(events: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    by_room: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        if not e.get("room_id"):
            raise ValueError(f"brain event {e['source_ref']} has no room")
        by_room.setdefault(e["room_id"], []).append(e)
    groups = []
    for room_id, evs in by_room.items():
        times = [_utc(e["occurred_at"]) for e in evs]
        start = _conversation_start(room_id, min(times))
        end = _conversation_end(room_id, max(times), now)
        marked = {e["source_ref"] for e in evs}
        turns = [_event_turn(e) for e in evs]
        turns += [{**t, "mark": None, "note": None} for t in _fetch_turns(room_id, start, end)
                  if t["ref"] not in marked]
        turns.sort(key=lambda t: t["at"])
        segments: List[List[Dict[str, Any]]] = []
        for t in turns:
            if segments and t["at"] - segments[-1][-1]["at"] < CONVERSATION_GAP:
                segments[-1].append(t)
            else:
                segments.append([t])
        groups.append({"heading": f"Room {room_id}", "description": _describe(room_id), "first_at": min(times),
                       "conversations": [{"label": "Conversation", "turns": _render_turns(seg)}
                                         for seg in segments if any(t["mark"] for t in seg)]})
    return groups


def _email_groups(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from app.assistant.pod_store.email_pods import email_sender_line, email_text, email_time
    by_account: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for e in events:
        record = _load_email(e["source_ref"])
        account = str(record.get("account_id") or "").strip()
        if not account:
            raise ValueError(f"email {e['source_ref']} has no account")
        thread = str(record.get("thread_id") or "").strip()
        by_account.setdefault(account, {}).setdefault(thread or e["source_ref"], []).append(
            {**e, "_thread": thread})
    groups = []
    for account, threads in by_account.items():
        conversations = []
        for key, evs in threads.items():
            marked = {e["source_ref"] for e in evs}
            turns = [_event_turn(e) for e in evs]
            thread = evs[0]["_thread"]
            if thread:                              # a Gmail thread: its other stored emails
                turns += [{"at": _utc(email_time(r)), "speaker": email_sender_line(r), "text": email_text(r),
                           "mark": None, "note": None}
                          for r in _email_thread(account, thread) if r["pod_id"] not in marked]
            turns.sort(key=lambda t: t["at"])
            conversations.append({"label": "Email thread" if len(turns) > 1 else "Email",
                                  "first_at": min(_utc(e["occurred_at"]) for e in evs),
                                  "turns": _render_turns(turns)})
        conversations.sort(key=lambda c: c["first_at"])
        groups.append({"heading": f"Email inbox {account}", "description": _describe_account(account),
                       "first_at": conversations[0]["first_at"],
                       "conversations": [{"label": c["label"], "turns": c["turns"]} for c in conversations]})
    return groups


def _work_groups(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_work: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        by_work.setdefault(e["source_ref"].split(":")[1], []).append(e)
    groups = []
    for work_id, evs in by_work.items():
        turns = sorted((_event_turn(e) for e in evs), key=lambda t: t["at"])
        groups.append({"heading": f"Work {work_id}",
                       "description": "Dayflow's work on the concerns it is attached to: each entry is a "
                                      "finalizer judgment of one of its tasks, or the work's ending.",
                       "first_at": turns[0]["at"],
                       "conversations": [{"label": "Progress", "turns": _render_turns(turns)}]})
    return groups


def build(events: List[Dict[str, Any]], *, now_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Group events (brain_events rows, each with a `mark` and optionally a `note`) into chat rooms,
    email inboxes and work objects, each with its conversations; groups ordered by their first event. Raises on
    an unknown source, a chat event with no room, or a room or account with no description: the
    reader must know where something was said and to whom."""
    now = now_utc or datetime.now(timezone.utc)
    by_source: Dict[str, List[Dict[str, Any]]] = {"chat": [], "email": [], "work": []}
    for e in events:
        if e["source"] not in by_source:
            raise ValueError(f"brain event {e['source_ref']} has unknown source {e['source']!r}")
        by_source[e["source"]].append(e)
    groups = (_chat_groups(by_source["chat"], now) + _email_groups(by_source["email"])
              + _work_groups(by_source["work"]))
    groups.sort(key=lambda g: g["first_at"])
    for g in groups:
        del g["first_at"]
    return groups


def render(rooms: List[Dict[str, Any]]) -> str:
    """The rooms as text, through the same macro the agent templates import."""
    from app.assistant.agent_runtime.services.prompt_builder import _strict_jinja_env as env
    return env.from_string('{% from "shared/macros/conversations.j2" import conversations %}'
                           '{{ conversations(rooms) }}').render(rooms=rooms)
