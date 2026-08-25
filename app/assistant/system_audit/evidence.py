"""system_audit.evidence — the deterministic forensics pass (no LLM).

Given an OPEN case, assemble the dossier the investigation (and later a
Claude Code session) starts from: friction quotes, the chat window around the
anchor, tickets and work objects reached through the bound ids, and a bounded
live-log excerpt. Windows and filters are DECLARED in the dossier and full
sources referenced by path — bounded evidence, never silent truncation.

Id harvesting: tickets found through bound work ids (and vice versa) feed
their ids back into the case's bound_ids, widening the deterministic join
surface for dedup and for the deep audit.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now, utc_to_local

logger = get_logger(__name__)

INBOX_DIR = Path("data/claude_audit_inbox")
_CHAT_WINDOW_MIN = 30     # D3: chat +/-30 min
_LOG_WINDOW_MIN = 5       # D3: logs +/-5 min
_LOG_LINE_CAP = 400       # declared in the dossier
_LOG_FILTER = ("ERROR", "WARNING", "dayflow", "work_session", "dispatch", "work_")


def assemble(case_id: str, *, log_path: Optional[str] = None) -> str:
    """Build the dossier for an open (or regressed) case, write it to the inbox,
    and move the case to `assembled`. Returns the dossier path."""
    from app.assistant.system_audit import case_store

    rows = [c for c in case_store.list_cases(statuses=["open", "regressed"], limit=200)
            if c["id"] == case_id]
    if not rows:
        raise KeyError(f"evidence.assemble: no open/regressed case {case_id!r}")
    case = rows[0]

    bound: Dict[str, List[str]] = {k: list(v) for k, v in (case["bound_ids"] or {}).items()}
    tickets = _tickets_for(bound)
    _harvest_ids(bound, tickets)
    work = _work_objects_for(bound.get("work_ids", []))

    parts: List[str] = []
    parts.append(_frontmatter(case, bound))
    parts.append(f"# Audit case {case['id']}\n\n**Summary:** {case['summary']}\n")
    parts.append(_friction_section(case))
    parts.append(_chat_section(case))
    parts.append(_ticket_section(tickets))
    parts.append(_work_section(work))
    parts.append(_log_section(case, log_path))

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = INBOX_DIR / f"case_{case['id']}.md"
    path.write_text("\n\n".join(p for p in parts if p), encoding="utf-8")

    case_store.transition(case_id, "assembled", dossier_path=str(path), bound_ids=bound)
    logger.info("[evidence] case %s assembled -> %s", case_id, path)
    return str(path)


# ------------------------------------------------------------------ sections
def _frontmatter(case: Dict[str, Any], bound: Dict[str, Any]) -> str:
    import json
    return ("---\n"
            f"case_id: {case['id']}\n"
            f"status: assembled\n"
            f"trigger_kind: {case['trigger_kind']}\n"
            f"room_id: {case['room_id'] or ''}\n"
            f"opened_at: {case['opened_at']}\n"
            f"bound_ids: {json.dumps(bound, default=str)}\n"
            "---")


def _friction_section(case: Dict[str, Any]) -> str:
    quotes = case.get("friction_quotes") or []
    if not quotes:
        return ""
    lines = ["## User friction (verbatim)"]
    for q in quotes:
        lines.append(f"- [{q.get('at', '?')}] ({q.get('kind', '?')}) "
                     f"\"{q.get('quote', '')}\" — message {q.get('message_id', '?')}")
    return "\n".join(lines)


def _chat_section(case: Dict[str, Any]) -> str:
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.models.base import get_session

    room = case.get("room_id")
    anchor = case.get("anchor_at") or utc_now()
    if not room:
        return "## Chat window\n\n(no room bound — skipped)"
    lo = anchor - timedelta(minutes=_CHAT_WINDOW_MIN)
    hi = anchor + timedelta(minutes=_CHAT_WINDOW_MIN)
    session = get_session()
    try:
        rows = (session.query(UnifiedLog2026)
                .filter(UnifiedLog2026.room_id == room,
                        UnifiedLog2026.timestamp >= lo,
                        UnifiedLog2026.timestamp <= hi)
                .order_by(UnifiedLog2026.timestamp.asc()).all())
        lines = [f"## Chat window\n\nSource: unified_log_2026, room `{room}`, "
                 f"{lo.isoformat()} .. {hi.isoformat()} (±{_CHAT_WINDOW_MIN} min around the anchor)."]
        for r in rows:
            t = utc_to_local(r.timestamp).strftime("%H:%M") if r.timestamp else "?"
            lines.append(f"- [{t}] {r.speaker_name or r.role or '?'} "
                         f"(`{r.id[:8]}`): {(r.message or '').strip()}")
        if not rows:
            lines.append("(no messages in window)")
        return "\n".join(lines)
    finally:
        session.close()


def _tickets_for(bound: Dict[str, List[str]]) -> List[Any]:
    from app.assistant.ticket_manager import get_ticket_manager
    tm = get_ticket_manager()
    wanted_tickets = set(bound.get("ticket_ids", []))
    wanted_work = set(bound.get("work_ids", []))
    out = []
    recent = tm.get_tickets(limit=200)
    for t in recent or []:
        d = t.to_dict() if hasattr(t, "to_dict") else dict(t)
        tid = str(d.get("ticket_id") or "")
        trig = d.get("trigger_context") or {}
        work_ref = str(trig.get("work_node") or "")
        work_id = work_ref.split("::", 1)[0] if work_ref else ""
        if tid in wanted_tickets or (work_id and work_id in wanted_work):
            out.append(d)
    return out


def _harvest_ids(bound: Dict[str, List[str]], tickets: List[Dict[str, Any]]) -> None:
    """Widen bound_ids with ids reachable from the found tickets (id-join doctrine)."""
    for d in tickets:
        tid = str(d.get("ticket_id") or "")
        if tid and tid not in bound.setdefault("ticket_ids", []):
            bound["ticket_ids"].append(tid)
        work_ref = str((d.get("trigger_context") or {}).get("work_node") or "")
        if work_ref:
            wid = work_ref.split("::", 1)[0]
            if wid and wid not in bound.setdefault("work_ids", []):
                bound["work_ids"].append(wid)


def _ticket_section(tickets: List[Dict[str, Any]]) -> str:
    lines = ["## Tickets (by id join)"]
    if not tickets:
        lines.append("(none matched the bound ids)")
    for d in tickets:
        lines.append(f"- `{d.get('ticket_id')}` [{d.get('status')}] {d.get('title')!r} "
                     f"trigger={d.get('trigger_context')} response={d.get('response_action') or d.get('user_response') or ''}")
    return "\n".join(lines)


def _work_objects_for(work_ids: List[str]) -> List[str]:
    if not work_ids:
        return []
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    from work_objects.discharge import render_graph_view
    store = get_dayflow_work_store()
    out = []
    for wid in work_ids:
        try:
            wo = store.load(wid)
        except KeyError:
            out.append(f"### {wid}\n(not found)")
            continue
        events = store.events(wid)[-15:]
        ev_lines = [f"  - {e['ts']} {e['actor'] or '?'} {e['op']}" for e in events]
        out.append(f"### {wid} [{wo.status}]\n```\n{render_graph_view(wo)}\n```\n"
                   f"Last {len(events)} events:\n" + "\n".join(ev_lines))
    return out


def _work_section(work: List[str]) -> str:
    if not work:
        return "## Work objects (by id join)\n\n(none bound)"
    return "## Work objects (by id join)\n\n" + "\n\n".join(work)


def _live_log_path() -> Optional[str]:
    """The running process's main log file, from the module that OWNS the handler.

    This used to scan `logging.getLogger().handlers` for a `baseFilename`, which can
    never match: records reach the file through a QueueHandler → QueueListener, so no
    logger holds the RotatingFileHandler. Every dossier was therefore written with
    `path=None` and the investigator reasoned about each case with zero log evidence.
    None here means logging genuinely is not set up yet, and the dossier says so.
    """
    from app.assistant.utils.logging_config import get_main_log_path

    return get_main_log_path()


def _log_section(case: Dict[str, Any], log_path: Optional[str]) -> str:
    path = log_path or _live_log_path()
    header = (f"## Log excerpt\n\nWindow ±{_LOG_WINDOW_MIN} min around the anchor, filter "
              f"{list(_LOG_FILTER)}, cap {_LOG_LINE_CAP} lines.")
    if not path or not Path(path).exists():
        return header + f"\nSource: (no log file available — path={path!r})"
    anchor = case.get("anchor_at") or utc_now()
    lo = anchor - timedelta(minutes=_LOG_WINDOW_MIN)
    hi = anchor + timedelta(minutes=_LOG_WINDOW_MIN)
    prefixes = set()
    cur = lo
    while cur <= hi:
        prefixes.add(utc_to_local(cur).strftime("%Y-%m-%d %H:%M"))
        cur += timedelta(minutes=1)
    kept: List[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line[:16] in prefixes and any(k in line for k in _LOG_FILTER):
                kept.append(line.rstrip())
                if len(kept) >= _LOG_LINE_CAP:
                    kept.append(f"(cap {_LOG_LINE_CAP} reached — read the full window in the source)")
                    break
    body = "\n".join(kept) if kept else "(no matching lines in window)"
    return header + f"\nSource: `{path}`\n\n```\n{body}\n```"
