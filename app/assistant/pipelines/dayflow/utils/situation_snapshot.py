"""
Situation Snapshot Builder

Gathers a whole-picture snapshot of the user's current state
from all available data sources. Used by the situation auditor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone

logger = get_logger(__name__)


def build_situation_snapshot() -> str:
    """Build a text snapshot of everything currently active.

    Returns a formatted string suitable for passing to the situation auditor agent.
    """
    now_utc = datetime.now(timezone.utc)
    local_tz = get_local_timezone()
    now_local = now_utc.astimezone(local_tz)
    sections = []

    # ── Time & Presence ──
    sections.append(_build_time_and_presence(now_local))

    # ── Calendar ──
    sections.append(_build_calendar(now_utc))

    # ── Requests from chat the assistant carried out directly ──
    sections.append(_build_chat_requests())

    # ── Work objects: in progress, what they did, and what ended ──
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    store = get_dayflow_work_store()
    sections.append(_build_work_objects(store))
    recent = _recent_work_objects(store, now_utc)
    sections.append(_build_work_activity(recent, now_utc))
    sections.append(_build_work_ended(recent, now_utc))

    # ── Ticket Responses ──
    sections.append(_build_ticket_responses(now_utc))

    # ── Routine Expectations ──
    sections.append(_build_routine())

    # ── User Directives ──
    sections.append(_build_user_directives())

    return "\n\n".join(s for s in sections if s)


def _build_time_and_presence(now_local: datetime) -> str:
    lines = [
        "### Current Time & Presence",
        f"**Time:** {now_local.strftime('%I:%M %p %Z')}",
        f"**Day:** {now_local.strftime('%A, %B %d, %Y')}",
    ]

    lines.extend(_presence_lines(now_local))

    try:
        from app.assistant.location_manager.location_manager import get_location_manager
        loc = get_location_manager().get_current_location()
        if isinstance(loc, dict) and loc.get("label"):
            lines.append(f"**Location:** {loc['label']}")
    except Exception:
        pass

    return "\n".join(lines)


def _presence_lines(now_local: datetime) -> List[str]:
    """At the keyboard or away, and since when, from the AFK monitor's live reading. The session's length is
    measured from the monitor's `active_start_utc`; until 2026-11-15 this read `current_session_minutes` /
    `active_work_session_minutes`, keys of the AFK statistics resource that the monitor's reading never carries,
    so every session showed as 0.0 min."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.time_utils import local_time_text, parse_iso_utc_strict

    afk_monitor = getattr(DI, "afk_monitor", None)
    if afk_monitor is None:
        return ["**Computer:** unknown; the AFK monitor is not running."]
    snapshot = afk_monitor.get_computer_activity()
    if not snapshot:
        return ["**Computer:** unknown; the AFK monitor has taken no reading yet."]
    if snapshot["is_afk"]:
        return ["**Computer:** AWAY FROM KEYBOARD"]
    lines = ["**Computer:** AT KEYBOARD"]
    start = snapshot.get("active_start_utc")
    if not start:
        lines.append("**Active session:** the AFK monitor has not recorded when this session began.")
        return lines
    minutes = (now_local - parse_iso_utc_strict(start, label="active_start_utc")).total_seconds() / 60.0
    lines.append(f"**Active session:** at the keyboard since {local_time_text(start)} ({round(minutes)} min)")
    return lines


def _build_calendar(now_utc: datetime) -> str:
    try:
        # Overlap, not "starts in window": a meeting in progress is the calendar fact that
        # matters most to "is now a bad moment", and the starts-only query hid it the moment
        # it began (the auditor then read "no upcoming events" beside an ongoing standup).
        from app.assistant.pipelines.dayflow.utils.context_sources import get_calendar_events_in_window
        events = get_calendar_events_in_window(hours=8)
        if not events:
            return "### Calendar\nNothing on the calendar now or in the next 8 hours."

        lines = ["### Calendar (in progress + next 8 hours)"]
        for ev in events:
            start = ev.get("start_time", "")
            end = ev.get("end_time", "")
            name = ev.get("event_name", "")
            tag = " (in progress)" if ev.get("in_progress") else ""
            lines.append(f"- {start} - {end}: {name}{tag}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("situation_snapshot: calendar failed: %s", e)
        return ""


def _build_chat_requests() -> str:
    """What the owner asked for in chat and the assistant carried out directly (master_room's dispatch markers,
    `source_type='master_room_dispatch'`): still being done, or done recently (the item view keeps a finished
    item 2 hours). This section was "Active Plans & Tasks" and "Recently Closed Items" until 2026-11-15, read from
    every item by state; plan synopses and action logs stopped being written in 2026-06, and work runs on work
    objects (the sections below), so these markers were all that still reached it. It read the result under
    `execution_result_summary`; master_room_tool_caller writes `execution_result`."""
    from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
    from app.assistant.dayflow_orchestrator.contracts import get_meta
    from app.assistant.utils.time_utils import local_time_text

    lines = []
    for item in get_dayflow_items():
        meta = get_meta(item)
        if meta.get("source_type") != "master_room_dispatch":
            continue
        state = meta.get("state")
        if state == "dispatched":
            lines.append(f"- being done now (asked {local_time_text(meta['dispatched_at'])}): {meta['summary']}")
        elif state == "closed":
            lines.append(f"- done {local_time_text(meta['executed_at'])}: {meta['summary']}")
            lines.append(f"  result: {meta['execution_result']}")
    if not lines:
        return "### Requests From Chat\nNone being done, and none done in the last 2 hours."
    return ("### Requests From Chat (being done, or done in the last 2 hours)\n"
            "*What the owner asked for in chat and the assistant carried out directly, outside the work objects.*\n"
            + "\n".join(lines))


def _build_work_objects(store) -> str:
    """Active work objects — multi-step work executing on the work graph."""
    from app.assistant.dayflow_orchestrator.work_portfolio import render_active_portfolio

    portfolio, count, _ = render_active_portfolio(store, store.list_work_objects())
    if not count:
        return "### Active Work Objects\nNo work in progress."
    return (
        "### Active Work Objects (work IN PROGRESS — not finished)\n"
        "*Ongoing work the assistant carries until every step is done, or abandons with a reason. Flag a work "
        "object's scheduled action (e.g. a delayed email) only if it collides with something needing the user.*\n"
        + portfolio
    )


_WINDOW_HOURS = 4


def _recent_work_objects(store, now_utc: datetime) -> list:
    """Every work object changed in the window, loaded. Every act, judgment and ending is a store write, which
    stamps the work object's updated_at; the summaries are newest-updated first."""
    from app.assistant.utils.time_utils import parse_iso_utc_strict

    cutoff = now_utc - timedelta(hours=_WINDOW_HOURS)
    out = []
    for s in store.list_work_objects():
        if parse_iso_utc_strict(s["updated_at"], label=f"{s['id']} updated_at") < cutoff:
            break
        out.append(store.load(s["id"]))
    return out


def _build_work_activity(work_objects: list, now_utc: datetime) -> str:
    """What the work did in the window: each outward act from its ledger (a ticket, an email), and each step's
    result in the finalizer's own words, oldest first, under the work it belongs to. Until 2026-11-15 this section
    ("Recent Actions") read dayflow action-log items, which stopped being written in 2026-06, so it always said
    "No actions" while the work turned off lights and sent tickets."""
    from app.assistant.utils.time_utils import local_time_text, parse_iso_utc_strict

    cutoff = now_utc - timedelta(hours=_WINDOW_HOURS)
    blocks = []
    for wo in work_objects:
        events = [(a.ts, f"{a.channel} to {a.target} ({a.outcome}): {a.summary}")
                  for a in wo.actions if a.ts >= cutoff]
        for node in wo.nodes.values():
            fin = node.payload.get("finalizer") if wo.is_work_unit(node) else None
            if not fin:
                continue
            at = parse_iso_utc_strict(fin["at"], label=f"{wo.id}::{node.id} finalizer at")
            if at >= cutoff:
                events.append((at, f'step "{node.title}", its result: {fin["outcome"]}'))
        if not events:
            continue
        lines = [f"- {wo.id}: {(wo.constraints or {}).get('objective') or wo.title}"]
        lines += [f"  - {local_time_text(at)}: {text}" for at, text in sorted(events, key=lambda e: e[0])]
        blocks.append((min(at for at, _ in events), "\n".join(lines)))
    if not blocks:
        return f"### What The Work Did (last {_WINDOW_HOURS} hours)\nNo step ran and nothing was sent."
    return (f"### What The Work Did (last {_WINDOW_HOURS} hours)\n"
            "*Each act the work performed and each step's result as the finalizer judged it, by work object.*\n"
            + "\n".join(text for _, text in sorted(blocks, key=lambda b: b[0])))


def _build_work_ended(work_objects: list, now_utc: datetime) -> str:
    """Work objects that ended in the window, completed or dropped, with when and why (recent_work's view, the
    one the architect reads). Until 2026-11-15 this section ("Recently Closed Items") read dayflow items, so a work
    object finished or abandoned that morning never reached the auditor."""
    from app.assistant.dayflow_orchestrator.recent_work import ended_item, render_recent_ended
    from app.assistant.utils.time_utils import parse_iso_utc_strict

    cutoff = now_utc - timedelta(hours=_WINDOW_HOURS)
    done, dropped = [], []
    for wo in work_objects:
        if wo.status not in ("done", "abandoned"):
            continue
        terminal = wo.nodes[wo.goal_node_id].payload["terminal"]
        if parse_iso_utc_strict(terminal["at"], label=f"{wo.id} ended at") < cutoff:
            continue
        (done if wo.status == "done" else dropped).append(ended_item(wo, finalizers=False))
    if not done and not dropped:
        return f"### Work That Ended (last {_WINDOW_HOURS} hours)\nNone."
    completed, abandoned = render_recent_ended(done, dropped, window_hours=_WINDOW_HOURS,
                                               labels={item["id"]: item["id"] for item in done + dropped})
    lines = [f"### Work That Ended (last {_WINDOW_HOURS} hours)", "**Completed:**", completed]
    if abandoned:
        lines += ["**Dropped:**", abandoned]
    return "\n".join(lines)


def _build_ticket_responses(now_utc: datetime) -> str:
    try:
        from app.assistant.pipelines.dayflow.utils.context_sources import get_responded_tickets_categorized

        responses = get_responded_tickets_categorized(since_utc=now_utc - timedelta(hours=4))
        lines = [
            "### Recent Ticket Responses (last 4 hours)",
            "*The user's quoted words are the authoritative signal; the category is just the button they clicked.*",
        ]
        has_any = False

        for category in ("accepted", "acknowledged", "declined", "snoozed"):
            items = responses.get(category, [])
            if items:
                has_any = True
                lines.append(f"**{category.upper()}:**")
                for t in items:
                    title = t.get("title") or t.get("message") or ""
                    comment = t.get("user_comment") or ""
                    lines.append(f"- {title}")
                    if comment:
                        lines.append(f"  user said: \"{comment}\"")

        if not has_any:
            return "### Recent Ticket Responses\nNone in last 4 hours."
        return "\n".join(lines)
    except Exception as e:
        logger.debug("situation_snapshot: ticket responses failed: %s", e)
        return ""


def _build_routine() -> str:
    """Today's plan, rendered from its rows (subconscious/day_plan.py). It read
    resources/resource_dayflow_routine.md until 2026-10-01, a path the routine was never written to,
    so this section was always empty."""
    from app.assistant.subconscious.day_plan import render_day
    return f"### Today's Routine\n{render_day().strip()}"


def _build_user_directives() -> str:
    """The owner's standing directives with their personal overlay, read as every agent reads a resource. Until
    2026-10-08 this read the old combined file's shared template directly: never the overlay, and any error returned
    nothing; the split of that file by role (2026-10-08) left it holding only the retired steward's copy."""
    from app.assistant.subconscious.situation import _resource, _scope
    content = str(_resource(_scope(), "resource_owner_standing_directives") or "").strip()
    return f"### User Standing Directives\n{content}" if content else ""
