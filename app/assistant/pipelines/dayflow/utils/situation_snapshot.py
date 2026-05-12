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

    # ── Active Plans & Tasks ──
    sections.append(_build_active_items())

    # ── Recent Actions ──
    sections.append(_build_recent_actions(now_utc))

    # ── Recently Closed Items ──
    sections.append(_build_recently_closed(now_utc))

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

    try:
        from app.assistant.ServiceLocator.service_locator import DI
        afk_monitor = getattr(DI, "afk_monitor", None)
        if afk_monitor:
            snapshot = afk_monitor.get_computer_activity()
            if isinstance(snapshot, dict):
                is_afk = bool(snapshot.get("is_afk", False))
                lines.append(f"**Computer:** {'AWAY FROM KEYBOARD' if is_afk else 'AT KEYBOARD'}")
                if not is_afk:
                    session_mins = snapshot.get("current_session_minutes") or snapshot.get("active_work_session_minutes") or 0
                    lines.append(f"**Active session:** {round(float(session_mins), 1)} min")
    except Exception as e:
        logger.debug("situation_snapshot: AFK check failed: %s", e)

    try:
        from app.assistant.location_manager.location_manager import get_location_manager
        loc = get_location_manager().get_current_location()
        if isinstance(loc, dict) and loc.get("label"):
            lines.append(f"**Location:** {loc['label']}")
    except Exception:
        pass

    return "\n".join(lines)


def _build_calendar(now_utc: datetime) -> str:
    try:
        from app.assistant.pipelines.dayflow.utils.context_sources import get_calendar_events_for_orchestrator
        events = get_calendar_events_for_orchestrator(hours=8)
        if not events:
            return "### Calendar\nNo upcoming events in next 8 hours."

        lines = ["### Calendar (next 8 hours)"]
        for ev in events:
            start = ev.get("start_time", "")
            end = ev.get("end_time", "")
            name = ev.get("event_name", "")
            lines.append(f"- {start} - {end}: {name}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("situation_snapshot: calendar failed: %s", e)
        return ""


def _build_active_items() -> str:
    try:
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        from app.assistant.dayflow_orchestrator.contracts import get_meta

        all_items = get_dayflow_items()
        plans = []
        active = []
        dispatched = []
        waiting = []

        for item in all_items:
            meta = get_meta(item)
            source_type = str(meta.get("source_type") or "").strip().lower()
            state = str(meta.get("state") or "").strip().lower()

            if source_type == "plan_synopsis":
                plans.append(meta)
                continue
            if source_type in ("action_dispatch", "action_result", "action_log", "chat"):
                continue

            if state in ("important_open", "actionable"):
                active.append(meta)
            elif state == "dispatched":
                dispatched.append(meta)
            elif state in ("waiting", "watching"):
                waiting.append(meta)

        lines = ["### Active Plans & Tasks"]

        if plans:
            lines.append("**Plans:**")
            for p in plans:
                plan_id = p.get("plan_id", "")
                synopsis = str(p.get("synopsis") or p.get("summary") or "")
                lines.append(f"- [{plan_id}] {synopsis}")

        if active:
            lines.append(f"**Actionable tasks ({len(active)}):**")
            for m in active:
                sid = m.get("short_id", "")
                summary = str(m.get("summary") or "")
                lines.append(f"- task {sid}: {summary}")

        if dispatched:
            lines.append(f"**Dispatched tasks ({len(dispatched)}):**")
            for m in dispatched:
                sid = m.get("short_id", "")
                summary = str(m.get("summary") or "")
                result = str(m.get("execution_result_summary") or "")
                lines.append(f"- task {sid}: {summary}")
                if result:
                    lines.append(f"  result: {result}")

        if waiting:
            lines.append(f"**Waiting tasks ({len(waiting)}):**")
            for m in waiting:
                sid = m.get("short_id", "")
                summary = str(m.get("summary") or "")
                wait_reason = str(m.get("wait_reason") or m.get("state_reason") or "")
                lines.append(f"- task {sid}: {summary}")
                if wait_reason:
                    lines.append(f"  waiting for: {wait_reason}")

        if not plans and not active and not dispatched and not waiting:
            lines.append("No active plans or tasks.")

        return "\n".join(lines)
    except Exception as e:
        logger.debug("situation_snapshot: active items failed: %s", e)
        return ""


def _build_recent_actions(now_utc: datetime) -> str:
    try:
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        from app.assistant.dayflow_orchestrator.contracts import get_meta
        from app.assistant.utils.time_utils import parse_iso_utc

        cutoff = now_utc - timedelta(hours=4)
        local_tz = get_local_timezone()
        all_items = get_dayflow_items()

        actions = []
        for item in all_items:
            meta = get_meta(item)
            if str(meta.get("source_type") or "").strip().lower() != "action_log":
                continue
            created = parse_iso_utc(str(meta.get("created_at") or ""))
            if created and created >= cutoff:
                time_local = created.astimezone(local_tz).strftime("%I:%M %p")
                summary = str(meta.get("summary") or "")
                actions.append(f"- [{time_local}] {summary}")

        if not actions:
            return "### Recent Actions\nNo actions in last 4 hours."

        return "### Recent Actions (last 4 hours)\n" + "\n".join(actions)
    except Exception as e:
        logger.debug("situation_snapshot: recent actions failed: %s", e)
        return ""


def _build_recently_closed(now_utc: datetime) -> str:
    """Items that closed recently — completions the auditor otherwise wouldn't see.

    Without this section, the auditor sees active/dispatched/waiting items but has
    no evidence of what finished. That leads to false positives like "the wrap-up
    task is still open" when in fact it closed 20 minutes ago.
    """
    try:
        from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
        from app.assistant.dayflow_orchestrator.contracts import get_meta
        from app.assistant.utils.time_utils import parse_iso_utc

        cutoff = now_utc - timedelta(hours=4)
        local_tz = get_local_timezone()
        all_items = get_dayflow_items()

        closed_entries = []
        for item in all_items:
            meta = get_meta(item)
            state = str(meta.get("state") or "").strip().lower()
            if state != "closed":
                continue
            source_type = str(meta.get("source_type") or "").strip().lower()
            if source_type in ("action_dispatch", "action_result", "action_log", "chat"):
                continue

            # Prefer the most recent timestamp we have for this item.
            ts_candidates = [
                meta.get("updated_at"),
                meta.get("dispatched_at"),
                meta.get("created_at"),
            ]
            ts = None
            for raw in ts_candidates:
                if not raw:
                    continue
                parsed = parse_iso_utc(str(raw))
                if parsed is not None:
                    ts = parsed
                    break
            if ts is None or ts < cutoff:
                continue

            time_local = ts.astimezone(local_tz).strftime("%I:%M %p")
            sid = meta.get("short_id", "")
            summary = str(meta.get("summary") or "")
            result = str(meta.get("execution_result_summary") or "")
            closed_entries.append((ts, sid, time_local, summary, result))

        if not closed_entries:
            return "### Recently Closed Items\nNone in last 4 hours."

        closed_entries.sort(key=lambda x: x[0])
        lines = [f"### Recently Closed Items (last 4 hours) ({len(closed_entries)})"]
        for _, sid, time_local, summary, result in closed_entries:
            lines.append(f"- [{time_local}] task {sid}: {summary}")
            if result:
                lines.append(f"  result: {result}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("situation_snapshot: recently closed failed: %s", e)
        return ""


def _build_ticket_responses(now_utc: datetime) -> str:
    try:
        from app.assistant.pipelines.dayflow.utils.context_sources import get_responded_tickets_categorized

        responses = get_responded_tickets_categorized(since_utc=now_utc - timedelta(hours=4))
        lines = ["### Recent Ticket Responses (last 4 hours)"]
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
    try:
        from pathlib import Path
        from app.assistant.utils.path_utils import get_resources_dir
        routine_path = Path(get_resources_dir()) / "resource_dayflow_routine.md"
        if routine_path.exists():
            content = routine_path.read_text(encoding="utf-8").strip()
            if content:
                return f"### Today's Routine\n{content}"
    except Exception:
        pass
    return ""


def _build_user_directives() -> str:
    try:
        from pathlib import Path
        from app.assistant.utils.path_utils import get_repo_root
        directives_path = get_repo_root() / "resources" / "instructions" / "resource_orchestrator_user_prefs.md"
        if directives_path.exists():
            content = directives_path.read_text(encoding="utf-8").strip()
            if content:
                return f"### User Standing Directives\n{content}"
    except Exception:
        pass
    return ""
