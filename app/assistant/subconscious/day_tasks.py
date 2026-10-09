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
the list is decided; a placement on the list keeps its entry unless the answer changes it. Until then every run
re-decided the whole day and code compared the text: of 43 changes on 2026-10-05 morning about 30 were rewordings,
dropped and restored covers, or "the assistant" become "assistant", and about 6 were re-judgments with nothing new.
From 2026-10-05 a change had to cite a fact newer than the entry, or the answer was refused. Removed 2026-10-08 (owner:
"you have a gate keeper not letting a decision through"): an entry re-decided at 06:12 because the architect had ended
its work as a duplicate of the day before's reminder could never be changed again, since the only fact about it, the
work's ending, was older than the entry; 40 answers were refused and 11 brain passes failed that day. The decider now
reads each entry with the reason it was decided and what became of its work, and its decision goes through. A
placement done elsewhere, or a clock-time one more than an hour past, is not offered again.

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
    read = {k: v for k, v in inputs.items()
            if k not in ("now", "correction", "items", "situation", "labels", "earlier", "source_labels", "own")}
    read["items"] = [{k: v for k, v in it.items() if k != "current"} for it in inputs["items"]]
    read["situation"] = fingerprint_view(inputs["situation"])
    return hashlib.sha256(json.dumps(read, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def with_current(day: str, items: List[Dict[str, Any]], *, connect=None) -> List[Dict[str, Any]]:
    """Each placement with its entry on the brain's list (its id, decision, the assistant's part, when, why, when it
    was decided, the entries it covers, and what its decision relies on as (kind, id)), or None."""
    from app.assistant.subconscious import brain_list
    now_on = {e["id"]: e for e in brain_list.entries(day=day, connect=connect)}
    out = []
    for it in items:
        e = now_on.get(brain_list.calendar_id(day, it["belief_key"]))
        out.append({**it, "current": None if e is None else {
            "entry_id": e["id"], "decision": e["decision"], "assistant_part": e["assistant_part"],
            "when": e["when_local"], "why": e["why"], "decided_at": e["updated_at"], "covers": e["covers"],
            "relies_on": [list(r) for r in brain_list.relies_on(e["id"], connect=connect)]}})
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
        rows = c.execute("SELECT id, created_at, decisions FROM brain_matters WHERE status = 'applied' AND created_at >= ? "
                         "AND created_at <= ? ORDER BY id", (start, now_utc.isoformat())).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["decisions"])
            events = []
            for ref, decided in d["events"].items():
                e = c.execute("SELECT source, occurred_at, text FROM brain_events WHERE source_ref = ?", (ref,)).fetchone()
                events.append({"source": e["source"], "at": e["occurred_at"], "text": e["text"],
                               "decision": decided["decision"], "reason": decided["reason"]})
            out.append({"id": r["id"], "at": r["created_at"], "events": events, "notes": d.get("concern_updates") or [],
                        "new_concerns": d.get("new_concerns") or []})
    return out


# The kinds of object the day's tasks reads, by label prefix (docs/design/objects_and_projections_2026-10-08.md).
_PREFIX = {"work": "W", "message": "M", "reminder": "R", "concern": "C", "list_entry": "E", "brain_matter": "B",
           "calendar_event": "K", "schedule_line": "K", "resource": "D"}
_DIRECTIVES = ("resource", "resource_owner_standing_directives")


def _schedule_ref(day: str, e: Dict[str, Any]):
    """A schedule line's object: its calendar event, or for a line the tracker added from chat or a concern (it has
    no id; docs/design/objects_and_projections_2026-10-08.md, the gap), the line itself by day, start and title."""
    if e.get("id"):
        return "calendar_event", e["id"]
    return "schedule_line", f"{day}|{e['start_local']}|{e['title']}"


def project(day: str, situation: Dict[str, Any], matters: List[Dict[str, Any]], items: List[Dict[str, Any]],
            own: List[Dict[str, Any]], *, connect=None) -> Dict[str, Any]:
    """The labels of this call, in place: each object the decider reads gets `ref` (placements keep their I labels;
    own entries O; work W; chat M; reminders sent R; concerns C; concerns' entries E; the brain's decisions B; the
    schedule's lines K; the standing directives D). An entry's work gets its label (`ref` on the work view); an entry
    that covers another shows it as `covered_by`; what an entry's decision relies on is shown by label
    (`relies_on_labels`), and each such object not otherwise in view is labelled too and listed in `earlier`, with
    its data. Work made from a list entry names that entry by its label (`from_list`). Returns `labels` (label ->
    [kind, id], for mapping the answer back), `earlier`, and `source_labels` (work id -> label, read by
    shared/brain/past_work.j2)."""
    from app.assistant.subconscious import brain_list
    from app.assistant.subconscious.projection import Projection
    p = Projection()
    for it in items:
        p.fixed(it["label"], "list_entry", brain_list.calendar_id(day, it["belief_key"]))
    p.fixed("D", *_DIRECTIVES)
    for e in own:
        e["ref"] = p.label("O", "list_entry", e["id"])
    for m in situation["chat"]:
        m["ref"] = p.label("M", "message", m["id"])
    for w in [*situation["work"]["in_motion"], *situation["work"]["ended"]]:
        w["ref"] = p.label("W", "work", w["work_id"])
    for r in situation["reminders"].get("records") or []:
        r["ref"] = p.label("R", "reminder", r["id"])
    for c in situation["concerns"]:
        c["ref"] = p.label("C", "concern", c["concern_id"])
    for e in [e for c in situation["concerns"] for e in c.get("entries") or []]:
        e["ref"] = p.label("E", "list_entry", e["id"])
    for m in matters:
        m["ref"] = p.label("B", "brain_matter", str(m["id"]))
    for e in situation["calendar_today"]:
        e["ref"] = p.label("K", *_schedule_ref(day, e))
    for line in situation["calendar_ahead"]:
        if line["anchor"]:
            line["ref"] = p.label("K", "calendar_event", line["anchor"][len("calendar:"):])
    entry_work = [it["current"].get("work") for it in items if it["current"]] + [e.get("work") for e in own] + \
        [e.get("work") for c in situation["concerns"] for e in c.get("entries") or []]
    for w in entry_work:
        if w:
            w["ref"] = p.label("W", "work", w["work_id"])
    earlier: List[Dict[str, Any]] = []

    def label_of(kind: str, obj_id: str) -> str:
        label = p.label_of(kind, obj_id)
        if label is None:
            label = p.label(_PREFIX[kind], kind, obj_id)
            earlier.append({"ref": label, "kind": kind, **describe(kind, obj_id, connect=connect)})
        return label

    by_entry = {it["current"]["entry_id"]: it["label"] for it in items if it["current"]}
    for it in items:
        cur = it["current"]
        if cur:
            cur["relies_on_labels"] = [label_of(k, i) for k, i in cur.get("relies_on") or []]
            cur["covered_by"] = next((by_entry[o["current"]["entry_id"]] for o in items if o["current"]
                                      and cur["entry_id"] in (o["current"].get("covers") or [])), None)
    for e in own:
        e["relies_on_labels"] = [label_of(k, i) for k, i in e.get("relies_on") or []]
        e["concern_ref"] = label_of("concern", e["concern_id"]) if e.get("concern_id") else None
    for w in [*situation["work"]["in_motion"], *situation["work"]["ended"]]:
        entry = w.get("list_entry")
        label = p.label_of("list_entry", entry) if entry else None
        w["from_list"] = None if not entry else (
            f"your entry for {label}" if label and label.startswith("I") else
            f"your own entry {label}" if label and label.startswith("O") else
            f"the concern's entry {label}" if label else
            f"an entry on the list of {entry.split(':')[1]}" if entry.startswith(("cal:", "day:")) else
            "a concern's entry not shown here")
    return {"labels": p.labels, "earlier": earlier,
            "source_labels": {i: lab for lab, (k, i) in p.labels.items() if k == "work"}}


def describe(kind: str, obj_id: str, *, connect=None) -> Dict[str, Any]:
    """An object an entry relies on that is not otherwise in the decider's view, as data for the prompt: `missing`
    when it is no longer stored."""
    from app.assistant.subconscious import work_links
    connect = connect or _connect
    if kind == "work":
        return {"work": work_links.work_view(obj_id, link="relied on")}
    if kind in ("message", "reminder"):
        with connect(False) as c:
            r = c.execute("SELECT timestamp, role, message FROM unified_log_2026 WHERE id = ?", (obj_id,)).fetchone()
        return {"missing": True} if r is None else {
            "at": str(r["timestamp"]).replace(" ", "T") + "+00:00",
            "speaker": "Owner" if r["role"] == "user" else "Assistant", "text": r["message"]}
    if kind == "concern":
        from app.assistant.subconscious.concern_store import load_register
        register = load_register()
        found = [(b, c) for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []
                 if c["concern_id"] == obj_id]
        return {"missing": True} if not found else {"title": found[0][1].get("title"), "bucket": found[0][0]}
    if kind == "list_entry":
        with connect(False) as c:
            r = c.execute("SELECT * FROM brain_list_entries WHERE id = ?", (obj_id,)).fetchone()
        return {"missing": True} if r is None else {"entry": {k: r[k] for k in (
            "kind", "day", "what", "decision", "when_local", "status", "updated_at")}}
    if kind == "brain_matter":
        with connect(False) as c:
            r = c.execute("SELECT created_at, decisions FROM brain_matters WHERE id = ?", (obj_id,)).fetchone()
        return {"missing": True} if r is None else {
            "at": r["created_at"], "reasons": [d["reason"] for d in json.loads(r["decisions"])["events"].values()]}
    if kind in ("calendar_event", "schedule_line"):
        return {"gone_from_schedule": True}
    if kind == "resource":
        return {}
    raise ValueError(f"day_tasks: no description for object kind {kind!r}")


# ── the answer ─────────────────────────────────────────────────────────────

def _in_prose(labels: Dict[str, Any], *texts: Any) -> List[str]:
    """Labels of this call written into prose. The single letter D is left out: it is also a word ("vitamin D")."""
    from app.assistant.subconscious.projection import labels_in
    return labels_in(" ".join(str(t or "") for t in texts), [x for x in labels if any(ch.isdigit() for ch in x)])


def own_problems(data: Dict[str, Any], own: List[Dict[str, Any]], labels: Dict[str, Any]) -> List[str]:
    """The own entries: a new one is entry "new"; a modify or cancel names one of today's own entries by its label;
    new and modify name the outcome, the assistant's part and when; every one says why, in words, and lists in
    relies_on the labels of what prompted it."""
    changes = data.get("own_entries")
    if changes is None:
        return []
    if not isinstance(changes, list):
        return ["own_entries is not a list"]
    by_label = {e["ref"]: e for e in own}
    out, seen = [], []
    for c in changes:
        entry, action = str((c or {}).get("entry") or "").strip(), (c or {}).get("action")
        if action not in ("new", "modify", "cancel"):
            out.append(f"own entry {entry!r}: action {action!r} is not new, modify or cancel")
            continue
        if action == "new" and entry != "new":
            out.append(f'a new own entry is written as entry "new", got {entry!r}')
        if action != "new":
            if entry not in by_label:
                out.append(f"{entry!r} is not one of your own entries today (O1, O2...)")
                continue
            seen.append(entry)
        if action in ("new", "modify"):
            missing = [f for f in ("what", "assistant_part", "when") if not str(c.get(f) or "").strip()]
            if missing:
                out.append(f"own entry {entry}: {action} names {', '.join(missing)}")
        if not str(c.get("why") or "").strip():
            out.append(f"own entry {entry}: why is empty")
        relied = [str(x).strip() for x in c.get("relies_on") or []]
        unknown = [x for x in relied if x not in labels]
        if not relied:
            out.append(f"own entry {entry}: relies_on names nothing; name what prompted it")
        elif unknown:
            out.append(f"own entry {entry}: relies_on {unknown}, which are not labels given")
        written = _in_prose(labels, c.get("what"), c.get("assistant_part"), c.get("why"))
        if written:
            out.append(f"own entry {entry}: {written} written into what, assistant_part or why; write it in words "
                       f"and put the reference in relies_on")
        concern = str(c.get("concern") or "").strip()
        if concern and (labels.get(concern) or [None])[0] != "concern":
            out.append(f"own entry {entry}: concern {concern!r} is not an open concern given (its C label)")
    twice = sorted({x for x in seen if seen.count(x) > 1})
    if twice:
        out.append(f"own entries changed more than once: {twice}")
    return out


def problems(data: Any, items: List[Dict[str, Any]], labels: Dict[str, Any]) -> List[str]:
    """Every item not yet on the list decided, once; needed names the part and when; covers name other items;
    relies_on names labels given; why and the assistant's part are words, with no label in them."""
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return ["no decisions list"]
    item_labels = [it["label"] for it in items]
    listed = {it["label"] for it in items if it["current"]}
    out, seen = [], []
    for d in data["decisions"]:
        label = str((d or {}).get("item") or "").strip()
        if label not in item_labels:
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
        bad = [x for x in d.get("covers") or [] if x not in item_labels or x == label]
        if bad:
            out.append(f"{label}: covers {bad}, which are not other items given")
        unknown = [x for x in d.get("relies_on") or [] if str(x).strip() not in labels]
        if unknown:
            out.append(f"{label}: relies_on {unknown}, which are not labels given")
        written = _in_prose(labels, d.get("why"), d.get("assistant_part"))
        if written:
            out.append(f"{label}: {written} written into why or assistant_part; write it in words and put the "
                       f"reference in relies_on")
    twice = sorted({x for x in seen if seen.count(x) > 1})
    if twice:
        out.append(f"decided more than once: {twice}")
    missing = [x for x in item_labels if x not in seen and x not in listed]
    if missing:
        out.append(f"not decided: {missing}")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="day_tasks", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::day_tasks", actor_id="subconscious::day_tasks")


def decided_placement(day: str, belief_key: str, *, connect=None) -> Dict[str, Any]:
    """The placement of `belief_key` as the newest run that decided it saw it (brain_day_decisions): its times,
    statement and related beliefs. What a calendar entry was decided from, read by the work made from it
    (dayflow_orchestrator/brain_list_intake.brief). Not the current plan: the day pass re-places the day as its
    inputs change and a belief can drop out of a run and return, and a brief read from the plan then raised and
    stopped every planning tick until it returned (found 2026-10-05, before the calendar lane went live). Raises
    when no run decided it."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        rows = c.execute("SELECT items, decisions FROM brain_day_decisions WHERE day = ? ORDER BY id DESC",
                         (day,)).fetchall()
    for r in rows:
        decided = {d["item"] for d in json.loads(r["decisions"])}
        for it in json.loads(r["items"]):
            if it["belief_key"] == belief_key and it["label"] in decided:
                return {k: it[k] for k in ("belief_key", "times", "statement", "related")}
    raise KeyError(f"no day's-tasks run on {day} decided {belief_key}")


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
    from app.assistant.dayflow_orchestrator.brain_list_intake import work_of
    # Its own entries today, and every earlier day's entry whose work is still in motion (2026-10-09): to change live
    # work the decider rewrites the entry that owns it, and intake carries the change to the work. Until then it could
    # reach only today's entries, and a change to work made from yesterday's entry became new work instead (a
    # worker asked to move a check-in that was a step of another work).
    own = brain_list.entries(day=day, kind="day")
    earlier = brain_list.open_before(day)
    own_work = work_of([e["id"] for e in own + earlier])
    own = own + [e for e in earlier if (own_work.get(e["id"]) or {}).get("status") == "active"]
    own = [{**e, "work": own_work.get(e["id"]), "relies_on": [list(r) for r in brain_list.relies_on(e["id"])]}
           for e in own]
    # On 2026-10-06 the decider called an item's own work "already covered by W8" and decided the item not needed,
    # which would have ended that work: work names the entry it was made from (project, `from_list`).
    return {"day": day, "now": now_utc.isoformat(), "items": items, "own": own, "situation": s, "matters": matters,
            **project(day, s, matters, items, own), "correction": None}


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
    own = inputs.get("own") or []

    def check(answer):
        found = problems(answer, inputs["items"], inputs["labels"])
        return found + (own_problems(answer, own, inputs["labels"]) if isinstance(answer, dict) else [])
    data = call(inputs)
    found = check(data)
    if found:
        step("day_tasks", "answer refused, one correction", subject=inputs["day"], problems=found)
        data = call({**inputs, "correction": "; ".join(found)})
        found = check(data)
        if found:
            raise ValueError(f"day_tasks answer still invalid after one correction: {found}")
    # The list changes land before the run (its fingerprint) is recorded: a run counts as done only once its entries
    # are on the list. Until 2026-10-05 the run was recorded first, and a failure applying the entries left the
    # next pass "unchanged" with the decisions lost; now it decides again, reading the entries that did land.
    # The answer's labels back to objects: the model referred only by labels of this call; code stores ids.
    from app.assistant.subconscious import brain_list
    from app.assistant.subconscious.projection import resolve
    call_labels = inputs["labels"]
    decisions = [{**d, "relies_on": [resolve(call_labels, x) for x in d.get("relies_on") or []]}
                 for d in data["decisions"]]
    changed = brain_list.apply_calendar(inputs["day"], inputs["items"], decisions,
                                        cause=f"day's tasks {now.isoformat()}", at=now, connect=connect)
    own_changes = [{**{k: str(c.get(k) or "").strip() for k in ("action", "what", "assistant_part", "when", "why")},
                    "entry": "new" if c.get("action") == "new" else resolve(call_labels, c["entry"])[1],
                    "concern_id": resolve(call_labels, c["concern"])[1] if str(c.get("concern") or "").strip() else None,
                    "relies_on": [resolve(call_labels, x) for x in c.get("relies_on") or []]}
                   for c in data.get("own_entries") or []]
    if own_changes:
        changed = {**changed, "own": brain_list.apply_day_own(inputs["day"], own_changes,
                                                             cause=f"day's tasks {now.isoformat()}", at=now,
                                                             connect=connect)}
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
