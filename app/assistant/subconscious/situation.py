"""The brain's situation: one picture of the moment, gathered once per brain pass for every brain decider (2026-10-04).

Design: docs/design/brain_delegates_work_2026-10-04.md, section 2 ("What every brain decider sees: the situation").
Owner, 2026-10-04: the agent that turns the day's calendar into tasks "better get all the context in the world so it
can make good decisions. also brain better get the calendar too and if user is afk and users health and all of
that." The steward saw most of this and the brain's deciders did not; one gatherer, shared by all of them, keeps
them from each pulling a partial view of its own.

No model call: code reads what is already stored, through the readers the rest of the system uses (the resource
manager, the presence status, the location manager, the key-dates builder, the steward's builders for reminders
and tickets, the brain's own readers for the calendar and for work in motion and past work). Every time stays UTC here; the
template shared/brain/situation.j2 shows it local. A reader that fails raises: a decider must not judge the moment
from a picture with holes it cannot see.

Chat is read as the gate reads it: a conversation still going (the owner's last message less than
brain_inbox.QUIET ago) is left out until the room goes quiet, so no decider acts on half of it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _scope():
    from app.assistant.scope.loader import load_scope_for_source
    return load_scope_for_source(kind="subsystem", source_id="subconscious", actor_id="subconscious::situation",
                                 identity_overrides={"owner_id": "system", "scope_id": "subconscious::situation",
                                                     "surface": "internal"})


def _resource(scope, resource_id: str) -> Any:
    from app.assistant.ServiceLocator.service_locator import DI
    return DI.resource_manager.get_resource(scope_context=scope, resource_id=resource_id, required=True)


# ── the parts ──────────────────────────────────────────────────────────────

def calendar_today(scope) -> List[Dict[str, str]]:
    """Today's schedule as the daily context tracker keeps it (the steward's TODAY'S SCHEDULE), long blocks such
    as work hours included: they say what kind of day it is. `id` is the line's calendar event, empty for a line the
    tracker added from chat or a concern."""
    return [{"id": str(e.get("calendar_item_id") or ""), "title": str(e.get("title") or ""),
             "start_local": str(e.get("start_local") or ""),
             "end_local": str(e.get("end_local") or ""), "status": str(e.get("status") or "")}
            for e in _resource(scope, "resource_expected_calendar").get("expected_schedule") or []]


def chat_today(day: str, now_utc: datetime) -> List[Dict[str, Any]]:
    """Today's master-room conversation (both sides), oldest first, from the day's boundary to now."""
    from app.assistant.pipelines.context import day_window_local
    from app.assistant.subconscious.db import connect
    start_local, _ = day_window_local(day)
    start = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with connect(False) as c:
        rows = c.execute("SELECT id, timestamp, role, message FROM unified_log_2026 WHERE room_id = 'master_room' "
                         "AND source != 'dayflow_item' AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                         (start, now_utc.strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchall()
    return [{"id": r["id"], "at": str(r["timestamp"]).replace(" ", "T") + "+00:00",
             "speaker": "Owner" if r["role"] == "user" else "Assistant", "text": r["message"]} for r in rows]


def finished_conversations(chat: List[Dict[str, Any]], now_utc: datetime) -> List[Dict[str, Any]]:
    """The chat without a conversation still going: when the owner's last message is less than QUIET old, the
    messages since the last silence of QUIET or more are left out (the gate's readiness rule, brain_inbox.ready)."""
    from app.assistant.subconscious.brain_inbox import QUIET
    at = [datetime.fromisoformat(m["at"]) for m in chat]
    owner = [t for t, m in zip(at, chat) if m["speaker"] == "Owner"]
    if not owner or max(owner) + QUIET <= now_utc:
        return chat
    start = 0
    for i in range(len(chat) - 1, 0, -1):
        if at[i] - at[i - 1] >= QUIET:
            start = i
            break
    return chat[:start]


def todos() -> List[Dict[str, Any]]:
    """The owner's Google Tasks as the todo sync stored them: title, status, due date."""
    from app.assistant.event_repository.event_repository import EventRepositoryManager
    out = []
    for row in json.loads(EventRepositoryManager().search_events(data_type="todo_task")):
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        out.append({"title": data["title"], "status": data.get("status") or "",
                    "due": str(data.get("due") or "")[:10]})
    return sorted(out, key=lambda t: t["title"])


ENDED_HOURS = 18   # the window of ended work the steward reads


def work(now_utc: datetime, *, calendar_work: bool) -> Dict[str, Any]:
    """Work in motion with where each stands, and the work that ended in the last ENDED_HOURS, each as the brain
    reads past work (work_links.work_view: why it ended and each task's outcome in the finalizer's own words, never
    its verdict or next step, which are dayflow's). `calendar_work=False` leaves out work made from a calendar
    placement: on a day the steward still holds that work is the steward's, and the brain must not copy it."""
    from datetime import timedelta
    from app.assistant.subconscious import work_links
    from app.assistant.utils.time_utils import parse_iso_utc

    cutoff = now_utc - timedelta(hours=ENDED_HOURS)
    active = [w for w in work_links.active_work() if calendar_work or not w["from_calendar"]]
    ended = [r for r in sorted(work_links._rows(), key=lambda r: r["updated_at"] or "", reverse=True)
             if r["status"] in ("done", "abandoned") and parse_iso_utc(r["updated_at"]) >= cutoff
             and (calendar_work or not work_links.from_calendar(r["constraints"]))]
    return {"in_motion": active, "ended": [work_links.work_view(r["id"], link="ended") for r in ended]}


def open_concerns() -> List[Dict[str, Any]]:
    """Each open concern: its id and bucket, title, severity, and when its record last changed (its last note, or its
    brief, which is written again whenever the record changes)."""
    from app.assistant.subconscious.concern_store import load_register
    from app.assistant.utils.time_utils import parse_iso_utc
    register = load_register()
    return [{"concern_id": c["concern_id"], "bucket": b, "title": c.get("title"), "severity": c.get("severity"),
             "changed_at": max(parse_iso_utc(t) for t in (c.get("last_reinforced_utc"),
                                                          (c.get("brief") or {}).get("written_at"),
                                                          c.get("first_observed")) if t).isoformat()}
            for b in ("active", "addressing") for c in register.get(b) or []]


# ── the whole ──────────────────────────────────────────────────────────────

def gather(now_utc: datetime, *, calendar_work: bool = True) -> Dict[str, Any]:
    """Everything a brain decider reads about the moment. Raises when a part cannot be read."""
    from app.assistant.control_nodes.strategic_planner_wo_prep_node import _load_active_tickets
    from app.assistant.dayflow_orchestrator.communication_context import scheduled_reminder_history
    from app.assistant.location_manager.location_manager import get_user_location_summary
    from app.assistant.pipelines.context import boundary_date_local_str, day_window_local
    from app.assistant.pipelines.dayflow.utils.context_sources import get_current_presence_status
    from app.assistant.subconscious.brain_step import _calendar_lines
    from app.assistant.subconscious.romantic_context_builder import _build_key_dates
    from app.assistant.utils.time_utils import get_local_timezone
    scope = _scope()
    now_local = now_utc.astimezone(get_local_timezone())
    day = boundary_date_local_str(now_local)
    daily = _resource(scope, "resource_daily_context_generator_output")
    weather = _resource(scope, "resource_weather")
    return {
        "now": now_utc.isoformat(), "day": day, "weekday": now_local.strftime("%A"),
        "key_dates": _build_key_dates(now_local=now_local),
        "calendar_today": calendar_today(scope),
        # From the day's start, not now: the text's header names its window, and a window starting now changed every
        # minute, so the day's tasks saw new input on every pass (2026-10-05).
        "calendar_ahead": _calendar_lines(day_window_local(day)[0].astimezone(timezone.utc)),
        "day_theme": str(daily.get("day_theme") or ""), "current_status": str(daily.get("current_status") or ""),
        "presence": get_current_presence_status(),
        "location": get_user_location_summary(hours_ahead=24),
        "weather": {"city": (weather.get("location") or {}).get("city"),
                    "temperature": (weather.get("current") or {}).get("temperature"),
                    "condition": (weather.get("current") or {}).get("condition")},
        "health": str(_resource(scope, "resource_health_inference_output").get("general_health_assessment") or ""),
        "chat": finished_conversations(chat_today(day, now_utc), now_utc),
        "todos": todos(),
        "work": work(now_utc, calendar_work=calendar_work),
        "reminders": scheduled_reminder_history(now_utc),
        "tickets": _load_active_tickets(),
        "concerns": open_concerns(),
    }


def fingerprint_view(s: Dict[str, Any]) -> Dict[str, Any]:
    """The situation minus what moves with the clock alone (the time, minutes away or at the keyboard, the
    temperature, the reminder window's bounds) and minus the health assessment, so a decider runs again when something
    changed, not every pass. The health prose is reworded on every inference run; a change in health alone waits for
    the next change of anything else, and the decider then reads the whole assessment (owner, 2026-10-05: health out
    of the fingerprint, no field list of the inference's output). Until 2026-10-05 the calendar's window and the
    health prose changed every pass, and the day's tasks called the model about every ten minutes to decide
    nothing."""
    presence = s["presence"]
    reminders = s["reminders"]
    return {**{k: v for k, v in s.items() if k not in ("now", "presence", "weather", "reminders", "health")},
            "presence": presence.get("status_label"),
            "weather": s["weather"].get("condition"),
            "reminders": {"status": reminders.get("status"),
                          "records": [(r["id"], r["text"]) for r in reminders.get("records") or []]}}
