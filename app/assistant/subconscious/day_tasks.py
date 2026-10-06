"""The day's tasks: the brain decides what today's calendar needs.

Design: docs/design/brain_delegates_work_2026-10-04.md. The brain decides what work exists and dayflow how. Until the
calendar lane went live (configs/brain_lanes.json, calendar.from_day: 2026-10-06) the calendar's placements reached
dayflow's steward by code (subconscious/belief_occurrences.py), and the steward invented an act for each; this ran in
shadow beside it. Replays on 2026-10-04 showed the brain deciding them better: no timesheets task when the owner had
said "I'll start at 5", the family-time heads-up kept once the steward's purpose was in its prompt. From the lane's
first day the decisions are the work: dayflow's list-item intake makes work from each needed entry
(dayflow_orchestrator/brain_list_intake.py). A day before it stays the steward's, and is still decided in shadow.

On each brain wake, when anything it reads has changed, the
`subconscious::day_tasks` agent (named the day decider until 2026-10-04) decides every placement of today's plan
(subconscious/day_plan.py): needed (who acts, the assistant's part, when), not needed (why), or done elsewhere. It
reads the placements with their related beliefs and their entries on the brain's list, and the brain's situation
(subconscious/situation.py): the schedule, presence, location, weather, health, the finished chat, the todo list,
the work in motion and what finished or was dropped, the reminders sent, the questions waiting, the open concerns.
On a day the steward still holds, the situation leaves out work made from the calendar: that work is the steward's,
and the brain must not copy it. On a live day it is the brain's own and is shown.

A run decides as the steward and the architect do (2026-10-04): once, then only what changes. A placement not yet on
the list is decided; a placement on the list keeps its entry unless the answer changes it, and a change must cite a
fact it read that is newer than the entry (a chat message, work, a reminder sent, a concern; each labelled in the
input). Until then every run re-decided the whole day and code compared the text: of 43 changes on 2026-10-05
morning about 30 were rewordings, dropped and restored covers, or "the assistant" become "assistant", and about 6
were re-judgments with nothing new. A placement done elsewhere, or a clock-time one more than an hour past, is not
offered again.

It reads what the brain knows, not a part of it (owner, 2026-10-04: it "sees less than brain"): besides the
situation, every open concern in full as the brain's agents read it (its record, brief, attempts and journal,
concern_planner.concern_text) with the concern's own entries on the list, and what the brain read and decided today
(brain_matters: each event with the brain's decision and reason, the notes it wrote, the concerns it raised). A
concern's entry is a fact a change can cite: the concern side runs first in the brain's pass, so an entry it makes
("we're eating out tonight") reaches the day's tasks in the same pass. Code checks the answer (one correction round) and records the run in `brain_day_decisions` (the
table keeps the name it was created with). `compare(day)` sets the brain's decisions beside what dayflow did with the
same beliefs, for the owner to judge.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::day_tasks"
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
    """The day's placements, one per belief with every time the plan places it ("all day" or clock times, in the
    plan's order), labelled I1, I2... in the plan's order. One entry per belief per day (2026-10-05): a placement the
    day pass moves changes the entry's times instead of cancelling one entry and making another."""
    by_belief: Dict[str, Dict[str, Any]] = {}
    for line in [*plan["flags"], *(line for s in plan["slots"] for line in s["lines"])]:
        it = by_belief.setdefault(line["belief_key"], {
            "belief_key": line["belief_key"], "times": [], "statement": line["statement"],
            "related": [r["statement"] for r in line.get("related") or []]})
        if line["time"] not in it["times"]:
            it["times"].append(line["time"])
    return [{"label": f"I{i}", **it} for i, it in enumerate(by_belief.values(), 1)]


def fingerprint(inputs: Dict[str, Any]) -> str:
    """What the decision read, minus the clock and minus the list's own entries (they change because of a run, so
    counting them would make every run trigger the next): a run is repeated only when one of these changed. The
    situation counts without what moves with the clock alone (situation.fingerprint_view)."""
    from app.assistant.subconscious.situation import fingerprint_view
    read = {k: v for k, v in inputs.items() if k not in ("now", "correction", "items", "situation", "refs")}
    read["items"] = [{k: v for k, v in it.items() if k != "current"} for it in inputs["items"]]
    read["situation"] = fingerprint_view(inputs["situation"])
    return hashlib.sha256(json.dumps(read, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def with_current(day: str, items: List[Dict[str, Any]], *, connect=None) -> List[Dict[str, Any]]:
    """Each placement with its entry on the brain's list (decision, the assistant's part, when, why, when it was
    decided), or None."""
    from app.assistant.subconscious import brain_list
    now_on = {e["id"]: e for e in brain_list.entries(day=day, connect=connect)}
    out = []
    for it in items:
        e = now_on.get(brain_list.calendar_id(day, it["belief_key"]))
        out.append({**it, "current": None if e is None else {
            "decision": e["decision"], "assistant_part": e["assistant_part"], "when": e["when_local"],
            "why": e["why"], "decided_at": e["updated_at"]}})
    return out


def with_work(day: str, items: List[Dict[str, Any]]) -> None:
    """Each listed item's entry, in place, with the work made from it, read live from the work store by the entry id
    (`current.work`, or None): what dayflow did with it, in the finalizer's own words."""
    from app.assistant.dayflow_orchestrator.brain_list_intake import work_of
    from app.assistant.subconscious import brain_list
    ids = {it["label"]: brain_list.calendar_id(day, it["belief_key"]) for it in items if it["current"]}
    work = work_of(list(ids.values()))
    for it in items:
        if it["current"]:
            it["current"]["work"] = work.get(ids[it["label"]])


def offered(day: str, items: List[Dict[str, Any]], now_utc: datetime) -> List[Dict[str, Any]]:
    """The placements still open to a decision, labelled I1, I2... again: not one done elsewhere, not one whose every
    time is a clock time more than an hour past (subconscious/belief_occurrences.passed, the handoff's own rule)."""
    from app.assistant.subconscious.belief_occurrences import passed
    still = [it for it in items if not ((it["current"] or {}).get("decision") == "done_elsewhere"
                                        or all(passed(day, t, now_utc) for t in it["times"]))]
    return [{**it, "label": f"I{i}"} for i, it in enumerate(still, 1)]


def with_concerns(situation: Dict[str, Any], *, connect=None) -> None:
    """Each open concern of the situation, in place, with its whole record as the brain's agents read it (`text`,
    concern_planner.concern_text) and its entries on the brain's list (`entries`)."""
    from app.assistant.subconscious import brain_list
    from app.assistant.subconscious.concern_planner import concern_text
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    by_id = {c["concern_id"]: c for b in ("active", "addressing") for c in register.get(b) or []}
    for c in situation["concerns"]:
        c["text"] = concern_text(by_id[c["concern_id"]], c["bucket"])
        c["entries"] = [{k: e[k] for k in ("id", "what", "decision", "assistant_part", "when_local", "status",
                                           "updated_at")}
                        for e in brain_list.entries(concern_id=c["concern_id"], connect=connect)]
    from app.assistant.dayflow_orchestrator.brain_list_intake import work_of
    work = work_of([e["id"] for c in situation["concerns"] for e in c["entries"]])
    for c in situation["concerns"]:
        for e in c["entries"]:
            e["work"] = work.get(e["id"])


def matters_today(day: str, now_utc: datetime, *, connect=None) -> List[Dict[str, Any]]:
    """What the brain read and decided today (brain_matters, applied): when, each event in its own words with the
    brain's decision and reason, the notes it wrote on concerns, the concerns it raised."""
    from app.assistant.pipelines.context import day_window_local
    connect = connect or _connect
    start = day_window_local(day)[0].astimezone(timezone.utc).isoformat()
    with connect(False) as c:
        rows = c.execute("SELECT created_at, decisions FROM brain_matters WHERE status = 'applied' AND created_at >= ? "
                         "AND created_at <= ? ORDER BY id", (start, now_utc.isoformat())).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["decisions"])
            events = []
            for ref, decided in d["events"].items():
                e = c.execute("SELECT source, occurred_at, text FROM brain_events WHERE source_ref = ?", (ref,)).fetchone()
                events.append({"source": e["source"], "at": e["occurred_at"], "text": e["text"],
                               "decision": decided["decision"], "reason": decided["reason"]})
            out.append({"at": r["created_at"], "events": events, "notes": d.get("concern_updates") or [],
                        "new_concerns": d.get("new_concerns") or []})
    return out


def label_refs(situation: Dict[str, Any], matters: List[Dict[str, Any]] = ()) -> Dict[str, str]:
    """Labels the facts a change can cite, in place (each gets `ref`, shown by shared/brain/situation.j2 and the
    day's tasks prompt): today's chat messages M1.., work W1.. (in motion, then ended), reminders sent R1.., open
    concerns C1.., the concerns' entries on the list E1.., what the brain decided today B1... Returns label -> when
    it happened (UTC ISO): for work, when it last changed; for a concern, when its record last changed; for an
    entry, when it was last decided."""
    from app.assistant.subconscious import work_links
    changed = {r["id"]: r["updated_at"] for r in work_links._rows()}
    refs: Dict[str, str] = {}

    def mark(things, prefix, at):
        for i, thing in enumerate(things, 1):
            thing["ref"] = f"{prefix}{i}"
            refs[thing["ref"]] = at(thing)

    mark(situation["chat"], "M", lambda m: m["at"])
    mark([*situation["work"]["in_motion"], *situation["work"]["ended"]], "W", lambda w: changed[w["work_id"]])
    mark(situation["reminders"].get("records") or [], "R", lambda r: r["timestamp"])
    mark(situation["concerns"], "C", lambda c: c["changed_at"])
    mark([e for c in situation["concerns"] for e in c.get("entries") or []], "E", lambda e: e["updated_at"])
    mark(list(matters), "B", lambda m: m["at"])
    return refs


# ── the answer ─────────────────────────────────────────────────────────────

def problems(data: Any, items: List[Dict[str, Any]], refs: Dict[str, str]) -> List[str]:
    """Every item not yet on the list decided, once; an item on the list decided only with a fact newer than its
    entry (new_since, by label); needed names the part and when; covers name other items."""
    from app.assistant.utils.time_utils import local_time_text, parse_iso_utc
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return ["no decisions list"]
    labels = [it["label"] for it in items]
    listed = {it["label"]: it["current"]["decided_at"] for it in items if it["current"]}
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
            missing = [f for f in ("assistant_part", "when") if not str(d.get(f) or "").strip()]
            if missing:
                out.append(f"{label}: needed names {', '.join(missing)}")
        elif d.get("covers"):
            out.append(f"{label}: only a needed item covers others")
        bad = [x for x in d.get("covers") or [] if x not in labels or x == label]
        if bad:
            out.append(f"{label}: covers {bad}, which are not other items given")
        if label in listed:
            cited = [str(x).strip() for x in d.get("new_since") or []]
            unknown = [x for x in cited if x not in refs]
            since = parse_iso_utc(listed[label])
            if unknown:
                out.append(f"{label}: new_since names {unknown}, which are not labels given")
            elif not any(parse_iso_utc(refs[x]) > since for x in cited):
                out.append(f"{label} is on the list since {local_time_text(listed[label])} and nothing cited is newer: "
                           f"leave it out to keep it, or name in new_since what you read that is new since then")
    twice = sorted({x for x in seen if seen.count(x) > 1})
    if twice:
        out.append(f"decided more than once: {twice}")
    missing = [x for x in labels if x not in seen and x not in listed]
    if missing:
        out.append(f"not decided: {missing}")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="day_tasks", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::day_tasks", actor_id="subconscious::day_tasks")


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
    from app.assistant.dayflow_orchestrator.brain_list_intake import calendar_live
    from app.assistant.subconscious import brain_list, situation
    every = placements(plan)
    # Calendar-made work is the brain's own on a live day; on a steward day it is left out (module docstring).
    s = situation.gather(now_utc, calendar_work=calendar_live(day))
    with_concerns(s)
    matters = matters_today(day, now_utc)
    items = offered(day, with_current(day, every), now_utc)
    with_work(day, items)
    return {"day": day, "now": now_utc.isoformat(), "items": items,
            "situation": s, "matters": matters, "refs": label_refs(s, matters), "correction": None}


def run_day_tasks(*, now_utc: Optional[datetime] = None, connect=None,
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
        return {"day_tasks": "no plan"}
    if not inputs["items"]:
        return {"day_tasks": "no placements"}
    fp = fingerprint(inputs)
    if fp == _last_fingerprint(inputs["day"], connect):
        return {"day_tasks": "unchanged"}
    call = call or _agent_call
    labels = [it["label"] for it in inputs["items"]]
    data = call(inputs)
    found = problems(data, inputs["items"], inputs["refs"])
    if found:
        step("day_tasks", "answer refused, one correction", subject=inputs["day"], problems=found)
        data = call({**inputs, "correction": "; ".join(found)})
        found = problems(data, inputs["items"], inputs["refs"])
        if found:
            raise ValueError(f"day_tasks answer still invalid after one correction: {found}")
    # The list changes land before the run (its fingerprint) is recorded: a run counts as done only once its entries
    # are on the list. Until 2026-10-05 the run was recorded first, and a failure applying the entries left the
    # next pass "unchanged" with the decisions lost; now it decides again, reading the entries that did land.
    from app.assistant.subconscious import brain_list
    changed = brain_list.apply_calendar(inputs["day"], inputs["items"], data["decisions"],
                                        cause=f"day's tasks {now.isoformat()}", at=now, connect=connect)
    with connect(True) as c:
        c.execute("INSERT INTO brain_day_decisions (day, decided_at_utc, fingerprint, items, decisions) "
                  "VALUES (?,?,?,?,?)",
                  (inputs["day"], now.isoformat(), fp, json.dumps(inputs["items"], ensure_ascii=False),
                   json.dumps(data["decisions"], ensure_ascii=False)))
    from app.assistant.dayflow_orchestrator.brain_list_intake import calendar_live
    mode = "live" if calendar_live(inputs["day"]) else "shadow"
    step("day_tasks", f"decided ({mode})", subject=inputs["day"], items=len(labels), decided=len(data["decisions"]),
         list_changes=changed, decisions=data["decisions"])
    logger.info("[day_tasks] %s: %d placement(s) offered, %d decided; list %s (%s)",
                inputs["day"], len(labels), len(data["decisions"]), changed, mode)
    return {"day_tasks": "decided", "day_items": len(labels), "day_decided": len(data["decisions"]),
            "day_list_changes": changed}


# ── the comparison ─────────────────────────────────────────────────────────

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
    """The day's entries on the brain's list beside what dayflow did with the same beliefs. (A run records only
    what it decided, so the list, not the newest run, holds the day.)"""
    from app.assistant.subconscious import brain_list
    handled = dayflow_handling(day)
    return [{**e, "dayflow": handled.get(e["belief_key"], [])}
            for e in brain_list.entries(day=day, connect=connect)]


if __name__ == "__main__":
    import sys
    for row in compare(sys.argv[1]):
        print(f"{row['placement_time']} {row['belief_key']}")
        print(f"    brain: {row['decision']}"
              + (f" | {row['assistant_part']} at {row['when_local']}" if row["decision"] == "needed" else "")
              + f" | {row['why']}")
        print(f"    dayflow: {row['dayflow'] or '(nothing)'}")
