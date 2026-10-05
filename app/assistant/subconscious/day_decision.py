"""The brain decides what today's calendar needs: SHADOW ONLY (2026-10-04).

Design: docs/design/brain_delegates_work_2026-10-04.md. The brain is to decide what work exists and dayflow how;
today the calendar's placements reach dayflow's steward by code (subconscious/belief_occurrences.py), and the
steward invents an act for each. Replays on 2026-10-04 showed the brain deciding them better: no timesheets task
when the owner had said "I'll start at 5", the family-time heads-up kept once the steward's purpose was in its
prompt.

This runs that decision live without acting on it. On each brain wake, when anything it reads has changed, the
`subconscious::day_decider` agent decides every placement of today's plan (subconscious/day_plan.py): needed (who
acts, the assistant's part, when), not needed (why), or done elsewhere. It reads the placements with their related
beliefs, the owner's todo list, today's chat, the work in motion that did not come from the calendar (with where it
stands), and the open concerns. Code checks that every placement is decided exactly once (one correction round)
and records the run in `brain_day_decisions`. Nothing is handed to dayflow: the steward goes on handling the
calendar, and `compare(day)` sets the brain's decisions beside what dayflow did with the same beliefs, for the
owner to judge.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::day_decider"
_DECISIONS = ("needed", "not_needed", "done_elsewhere")

SCHEMA = """CREATE TABLE IF NOT EXISTS brain_day_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    decided_at_utc TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    items TEXT NOT NULL,
    decisions TEXT NOT NULL)"""


def ensure_schema(connect=None) -> None:
    with (connect or _connect)(True) as c:
        c.execute(SCHEMA)


# ── what the decision reads ────────────────────────────────────────────────

def placements(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The day's placements, once each (belief and time), labelled I1, I2... in the plan's order."""
    seen, out = set(), []
    for line in [*plan["flags"], *(line for s in plan["slots"] for line in s["lines"])]:
        key = (line["belief_key"], line["time"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": f"I{len(out) + 1}", "belief_key": line["belief_key"], "time": line["time"],
                    "statement": line["statement"],
                    "related": [r["statement"] for r in line.get("related") or []]})
    return out


def todos() -> List[Dict[str, Any]]:
    """The owner's Google Tasks as the todo sync stored them: title, status, due date."""
    from app.assistant.event_repository.event_repository import EventRepositoryManager
    out = []
    for row in json.loads(EventRepositoryManager().search_events(data_type="todo_task")):
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        out.append({"title": data["title"], "status": data.get("status") or "",
                    "due": str(data.get("due") or "")[:10]})
    return sorted(out, key=lambda t: t["title"])


def chat_today(day: str, now_utc: datetime) -> List[Dict[str, Any]]:
    """Today's master-room conversation (both sides), oldest first, from the day's boundary to now."""
    from app.assistant.pipelines.context import day_window_local
    start_local, _ = day_window_local(day)
    start = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _connect(False) as c:
        rows = c.execute("SELECT id, timestamp, role, message FROM unified_log_2026 WHERE room_id = 'master_room' "
                         "AND source != 'dayflow_item' AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                         (start, now_utc.strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchall()
    return [{"id": r["id"], "at": str(r["timestamp"]).replace(" ", "T") + "+00:00",
             "speaker": "Owner" if r["role"] == "user" else "Assistant", "text": r["message"]} for r in rows]


def work_in_motion() -> List[Dict[str, Any]]:
    """Active work that was not made from a calendar placement, with where it stands."""
    from app.assistant.subconscious import work_links
    return [{"work_id": w["work_id"], "objective": w["objective"] or w["title"], "progress": w["progress"]}
            for w in work_links.active_work() if not w["from_calendar"]]


def open_concerns() -> List[Dict[str, Any]]:
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    return [{"title": c.get("title"), "severity": c.get("severity")}
            for b in ("active", "addressing") for c in register.get(b) or []]


def fingerprint(inputs: Dict[str, Any]) -> str:
    """What the decision read, minus the clock: a run is repeated only when one of these changed."""
    return hashlib.sha256(json.dumps({k: v for k, v in inputs.items() if k not in ("now", "correction")},
                                     sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ── the answer ─────────────────────────────────────────────────────────────

def problems(data: Any, labels: List[str]) -> List[str]:
    """Every placement decided exactly once; needed names who, the part and when; covers name other items."""
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return ["no decisions list"]
    out, seen = [], []
    for d in data["decisions"]:
        label = str((d or {}).get("item") or "").strip()
        if label not in labels:
            out.append(f"{label!r} is not one of the items given")
            continue
        seen.append(label)
        if d.get("decision") not in _DECISIONS:
            out.append(f"{label}: decision {d.get('decision')!r} is not one of {list(_DECISIONS)}")
        if d.get("decision") == "needed":
            missing = [f for f in ("who_acts", "assistant_part", "when") if not str(d.get(f) or "").strip()]
            if missing:
                out.append(f"{label}: needed names {', '.join(missing)}")
        elif d.get("covers"):
            out.append(f"{label}: only a needed item covers others")
        bad = [x for x in d.get("covers") or [] if x not in labels or x == label]
        if bad:
            out.append(f"{label}: covers {bad}, which are not other items given")
    twice = sorted({x for x in seen if seen.count(x) > 1})
    if twice:
        out.append(f"decided more than once: {twice}")
    missing = [x for x in labels if x not in seen]
    if missing:
        out.append(f"not decided: {missing}")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="day_decision", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::day_decider", actor_id="subconscious::day_decider")


def _last_fingerprint(day: str, connect) -> Optional[str]:
    with connect(False) as c:
        row = c.execute("SELECT fingerprint FROM brain_day_decisions WHERE day = ? ORDER BY id DESC LIMIT 1",
                        (day,)).fetchone()
    return row["fingerprint"] if row else None


def gather(now_utc: datetime, *, plan_connect=None) -> Optional[Dict[str, Any]]:
    """Everything the decision reads for the current day, or None when the day has no plan yet."""
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.subconscious import day_plan
    from app.assistant.utils.time_utils import get_local_timezone
    day = boundary_date_local_str(now_utc.astimezone(get_local_timezone()))
    plan = day_plan.load_plan(day, connect=plan_connect)
    if plan is None:
        return None
    return {"day": day, "now": now_utc.isoformat(), "items": placements(plan), "todos": todos(),
            "chat": chat_today(day, now_utc), "work": work_in_motion(), "concerns": open_concerns(),
            "correction": None}


def run_day_decision(*, now_utc: Optional[datetime] = None, connect=None,
                     gather_inputs: Callable[[datetime], Optional[Dict[str, Any]]] = None,
                     call: Callable[[Dict[str, Any]], Any] = None) -> Dict[str, Any]:
    """Decide today's placements when anything read has changed since the last run; record the run. The
    keyword arguments are test seams. Raises when the answer is still invalid after one correction."""
    from app.assistant.subconscious.brain_trace import step
    now = now_utc or datetime.now(timezone.utc)
    connect = connect or _connect
    ensure_schema(connect)
    inputs = (gather_inputs or gather)(now)
    if inputs is None:
        return {"day_decision": "no plan"}
    if not inputs["items"]:
        return {"day_decision": "no placements"}
    fp = fingerprint(inputs)
    if fp == _last_fingerprint(inputs["day"], connect):
        return {"day_decision": "unchanged"}
    call = call or _agent_call
    labels = [it["label"] for it in inputs["items"]]
    data = call(inputs)
    found = problems(data, labels)
    if found:
        step("day_decision", "answer refused, one correction", subject=inputs["day"], problems=found)
        data = call({**inputs, "correction": "; ".join(found)})
        found = problems(data, labels)
        if found:
            raise ValueError(f"day decider answer still invalid after one correction: {found}")
    with connect(True) as c:
        c.execute("INSERT INTO brain_day_decisions (day, decided_at_utc, fingerprint, items, decisions) "
                  "VALUES (?,?,?,?,?)",
                  (inputs["day"], now.isoformat(), fp, json.dumps(inputs["items"], ensure_ascii=False),
                   json.dumps(data["decisions"], ensure_ascii=False)))
    needed = [d for d in data["decisions"] if d["decision"] == "needed"]
    step("day_decision", "decided (shadow)", subject=inputs["day"], items=len(labels), needed=len(needed),
         decisions=data["decisions"])
    logger.info("[day_decision] %s: %d placement(s), %d needed (shadow; nothing handed to dayflow)",
                inputs["day"], len(labels), len(needed))
    return {"day_decision": "decided", "day_items": len(labels), "day_needed": len(needed)}


# ── the comparison ─────────────────────────────────────────────────────────

def latest(day: str, *, connect=None) -> Optional[Dict[str, Any]]:
    """The newest recorded decision for a day: its items and decisions, joined by label."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        row = c.execute("SELECT * FROM brain_day_decisions WHERE day = ? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
    if row is None:
        return None
    items = {it["label"]: it for it in json.loads(row["items"])}
    return {"decided_at_utc": row["decided_at_utc"],
            "decisions": [{**items[d["item"]], **d} for d in json.loads(row["decisions"])]}


def dayflow_handling(day: str) -> Dict[str, List[str]]:
    """What dayflow did with each belief's placements that day: belief key -> each occurrence's outcome
    (converted to work, or the steward's no-action reason)."""
    from app.assistant.pipelines.context import day_window_local
    start_local, _ = day_window_local(day)
    since = start_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out: Dict[str, List[str]] = {}
    with _connect(False) as c:
        rows = c.execute("SELECT metadata_json FROM unified_log_2026 WHERE source = 'dayflow_item' AND timestamp >= ?",
                         (since,)).fetchall()
    for (raw,) in rows:
        meta = json.loads(raw)
        if meta.get("source_type") == "belief" and meta.get("occurrence_day") == day:
            out.setdefault(meta["belief_key"], []).append(
                f"{meta.get('occurrence_time')}: {meta.get('state_reason') or meta.get('state')}")
    return out


def compare(day: str, *, connect=None) -> List[Dict[str, Any]]:
    """The brain's newest decisions for a day beside what dayflow did with the same beliefs."""
    decided = latest(day, connect=connect)
    if decided is None:
        return []
    handled = dayflow_handling(day)
    return [{**d, "dayflow": handled.get(d["belief_key"], [])} for d in decided["decisions"]]


if __name__ == "__main__":
    import sys
    for row in compare(sys.argv[1]):
        print(f"[{row['label']}] {row['time']} {row['statement'][:90]}")
        print(f"    brain: {row['decision']}"
              + (f" | {row['who_acts']}: {row['assistant_part']} at {row['when']}" if row["decision"] == "needed" else "")
              + f" | {row['why'][:160]}")
        print(f"    dayflow: {row['dayflow'] or '(nothing)'}")
