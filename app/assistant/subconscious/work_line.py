"""The one line an ended work object is known by (2026-10-11).

Owner, 2026-10-11: "we have really a lot of text for things that have finished", and the end result should be one
line, "set AC to COOL 75F at 6AM. Done successfully or something like that". Code cannot write that line: telling
"done as asked" from "done, with something the owner should know", or saying what a cancelled work left undone, takes
reading the outcome. So once a work ends, `subconscious::work_line` reads its full report (what it was for, each
task's outcome in the finalizer's words, how and when it ended) and writes the line. The full report stays in the
work store for any reader that looks closer.

The brain's pass writes the lines (brain_wake, after the concern summaries): every work that ended in the last
WINDOW_HOURS and has no row yet, so the day's tasks and the day's record later in the same pass read them. The lines
live in `brain_work_lines` (emi.db), one row per work: the line, or the error when it could not be written (logged,
not tried again: an ended work does not change). Read by the look-back (day_tasks.matters_today).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::work_line"
# Work that ended this long ago or less gets its line: the look-back covers one day.
WINDOW_HOURS = 30

SCHEMA = """CREATE TABLE IF NOT EXISTS brain_work_lines (
    work_id TEXT PRIMARY KEY,
    ended_at TEXT NOT NULL,
    line TEXT,
    error TEXT,
    written_at TEXT NOT NULL)"""


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)


def build_payload(wo) -> Dict[str, Any]:
    """The ended work as the agent reads it: its full objective, each main task with the finalizer's account (none
    for a task never judged), and how it ended. Raises for work that has not ended."""
    goal = wo.nodes[wo.goal_node_id]
    terminal = (goal.payload or {}).get("terminal") or {}
    if not terminal.get("at"):
        raise ValueError(f"work {wo.id} has not ended")
    tasks = [n for n in wo.nodes.values() if wo.is_work_unit(n)]
    tasks.sort(key=lambda n: str(n.created_at or ""))
    return {"purpose": (goal.content or goal.title or "").strip(),
            "tasks": [{"title": n.title, "status": n.status,
                       "outcome": ((n.payload or {}).get("finalizer") or {}).get("outcome"),
                       "at": ((n.payload or {}).get("finalizer") or {}).get("at")} for n in tasks],
            "ended": {"status": terminal.get("status") or "", "at": terminal["at"],
                      "reason": str(terminal.get("reason") or "").strip()}}


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="work_line", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::work_line", actor_id="subconscious::work_line")


def write_line(wo, *, call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> str:
    """The line of one ended work; raises when the answer has no line after one correction."""
    payload = build_payload(wo)
    call = call or _agent_call
    data = call(payload)
    if not isinstance(data, dict) or not str(data.get("line") or "").strip():
        logger.warning("[work_line] no line for %s, one correction", wo.id)
        data = call({**payload, "correction": "the answer had no line"})
        if not isinstance(data, dict) or not str(data.get("line") or "").strip():
            raise ValueError("work line still empty after one correction")
    return " ".join(str(data["line"]).split())


def lines(work_ids: Iterable[str], *, connect=None) -> Dict[str, str]:
    """The written line of each of these works that has one."""
    connect = connect or _connect
    ensure_schema(connect)
    ids = list(dict.fromkeys(work_ids))
    if not ids:
        return {}
    with connect(False) as c:
        rows = c.execute(f"SELECT work_id, line FROM brain_work_lines WHERE line IS NOT NULL AND work_id IN "
                         f"({','.join('?' * len(ids))})", ids).fetchall()
    return {r["work_id"]: r["line"] for r in rows}


def _recently_ended(now_utc: datetime) -> List[Any]:
    """Every work object that ended in the last WINDOW_HOURS, loaded."""
    from app.assistant.subconscious import work_links
    from app.assistant.utils.time_utils import parse_iso_utc
    cutoff = now_utc - timedelta(hours=WINDOW_HOURS)
    store = work_links._store()
    return [store.load(r["id"]) for r in work_links._rows()
            if r["status"] in ("done", "abandoned") and parse_iso_utc(r["updated_at"]) >= cutoff]


def run_lines(*, now_utc: Optional[datetime] = None, call=None, connect=None,
              ended: Optional[Callable[[datetime], List[Any]]] = None) -> Dict[str, Any]:
    """Write the line of every recently ended work that has no row yet. Free when there is none."""
    from app.assistant.subconscious.brain_trace import step, trace
    connect = connect or _connect
    ensure_schema(connect)
    now_utc = now_utc or datetime.now(timezone.utc)
    with connect(False) as c:
        done = {r["work_id"] for r in c.execute("SELECT work_id FROM brain_work_lines").fetchall()}
    todo = [wo for wo in (ended or _recently_ended)(now_utc) if wo.id not in done]
    written = failed = 0
    for wo in todo:
        ended_at = wo.nodes[wo.goal_node_id].payload["terminal"]["at"]
        line = error = None
        try:
            with trace(stage="work_line", work_id=wo.id):
                line = write_line(wo, call=call)
            written += 1
            step("work_line", "written", subject=wo.id, line=line)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error("[work_line] could not write the line of %s; not tried again: %s", wo.id, exc, exc_info=True)
            step("work_line", "failed: not tried again", subject=wo.id, error=error)
            failed += 1
        with connect(True) as c:
            c.execute("INSERT INTO brain_work_lines (work_id, ended_at, line, error, written_at) VALUES (?,?,?,?,?)",
                      (wo.id, ended_at, line, error, datetime.now(timezone.utc).isoformat()))
    if todo:
        logger.info("[work_line] written=%d failed=%d", written, failed)
    return {"work_lines_written": written, "work_lines_failed": failed}
