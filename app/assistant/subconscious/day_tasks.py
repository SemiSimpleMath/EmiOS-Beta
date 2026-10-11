"""The day's tasks: the brain decides what today's calendar needs, and since 2026-10-09 the whole list.

One decider (owner, 2026-10-09; docs/design/hub_and_spokes_2026-10-09.md): this decides every entry on the brain's
list, the day's own and every concern's (each concern's planner, subconscious/concern_planner.py, looks things up and
proposes them). It is woken only by the stages before it, the concerns with their plans (fingerprint), and what was
said reaches it through the concerns the brain made of it. Since 2026-10-09 the day's calendar arrives as concerns
too (subconscious/calendar_concerns.py, rule 7 of docs/design/concerns_as_state_2026-10-09.md): each placed belief is
an open concern with what it asks for today, and its entry is a concern's entry. Until then each placement was an
item decided here separately (needed, not needed, done elsewhere) onto a calendar entry (`cal:<day>:<belief_key>`);
the paragraphs below describe that time.
Until then the concern planner wrote a concern's entries and this the rest, neither could change the other's, and
chat the brain had already turned into concerns woke this a second time.

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
situation, every open concern in full as the brain's agents read it (its summary and the notes since, its plan and the work
attached to it, concern_planner.concern_text) with the concern's own entries on the list, and what the brain read and decided today
(brain_matters: each event with the brain's decision and reason, the notes it wrote, the concerns it raised). A
concern's entry is a fact a change can cite: the concern side runs first in the brain's pass, so an entry it makes
("we're eating out tonight") reaches the day's tasks in the same pass. Code checks the answer (one correction round) and records the run in `brain_day_decisions` (the
table keeps the name it was created with). Until 2026-10-09 `compare(day)` set the brain's shadow decisions beside
what the steward did with the same beliefs.
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

def fingerprint(inputs: Dict[str, Any]) -> str:
    """What wakes the decider: the stages before it (docs/design/hub_and_spokes_2026-10-09.md, "each stage is woken
    only by the stage before it"): each open concern's record and plan (the brain step, the day pass's calendar
    concerns and the concern planner); and, the one exception (owner, 2026-10-10: "yes add the wake rule"), the end of
    work one of its decisions relied on (`ended_relied_on`). Everything else it reads (the situation, the chat, what
    the brain decided today, the list's own entries and their work in progress) is context, never a reason to run.
    Until 2026-10-09 the situation counted: chat the brain had already turned into concerns woke this decider a
    second time, and the work its own entries made changed the situation and woke it again. Until 2026-10-10 an ending
    did not count: at 8:02 that day it cancelled three entries as covered by work already scheduled, the work was then
    ended, and nothing would have woken it to give the concerns their entries again."""
    read = {"day": inputs["day"],
            "concerns": [(c["concern_id"], c["basis"], (c.get("plan") or {}).get("written_at"))
                         for c in inputs["situation"]["concerns"]],
            "ended_relied_on": inputs["ended_relied_on"]}
    return hashlib.sha256(json.dumps(read, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def with_concerns(situation: Dict[str, Any], *, connect=None) -> None:
    """Each open concern of the situation, in place, with its whole record as the brain's agents read it (`text`,
    concern_planner.concern_text), the record's basis (`basis`, concern_view.basis: what the fingerprint counts), its
    planner's plan (`plan`: the decision, why, until when, when it was written, and the changes it proposes, or None),
    its entries on the brain's list (`entries`) and the ones cancelled, with why (`cancelled`; 2026-10-06: a decider
    re-created a question it had cancelled the night before, not seeing it)."""
    from app.assistant.subconscious import brain_list
    from app.assistant.subconscious.concern_view import basis
    from app.assistant.subconscious.concern_planner import concern_text
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    by_id = {c["concern_id"]: c for b in ("active", "addressing") for c in register.get(b) or []}
    for c in situation["concerns"]:
        concern = by_id[c["concern_id"]]
        c["text"] = concern_text(concern, c["bucket"])
        c["basis"] = basis(concern, c["bucket"])
        plan = concern.get("plan")
        c["plan"] = None if not plan else {
            **{k: plan.get(k) for k in ("decision", "why", "hold_until", "written_at")},
            "proposals": [dict(x) for x in plan.get("proposals") or []]}
        every = brain_list.entries(concern_id=c["concern_id"], include_cancelled=True, connect=connect)
        c["entries"] = [{k: e[k] for k in ("id", "kind", "what", "decision", "assistant_part", "when_local", "status",
                                           "updated_at")}
                        for e in every if e["status"] != "cancelled"]
        c["cancelled"] = [{k: e[k] for k in ("what", "why", "updated_at")} for e in every if e["status"] == "cancelled"]
    from app.assistant.dayflow_orchestrator.brain_list_intake import work_of
    work = work_of([e["id"] for c in situation["concerns"] for e in c["entries"]])
    for c in situation["concerns"]:
        for e in c["entries"]:
            e["work"] = work.get(e["id"])


def matters_today(day: str, now_utc: datetime, *, connect=None, work_brief=None) -> List[Dict[str, Any]]:
    """What the brain read and decided today (brain_matters, applied): when, each event with the brain's decision and
    reason, the notes it wrote on concerns no longer open, the concerns it raised.

    A look-back, so what is shown in full elsewhere is shown here briefly (owner, 2026-10-11: "a lot of text for
    things that have finished"). A message or an email is shown in its own words. The work events of one work, its
    judgments and its ending, are one item: the work as it stands now (`work`, work_brief: title, status, when it last
    changed, why it ended) with the brain's decision on each; the full report was read when it arrived, and a reader
    that looks closer has the work in full (the day's tasks: its label, project). A note on a concern still open is
    left out: every reader shows an open concern's notes with it."""
    from app.assistant.pipelines.context import day_window_local
    from app.assistant.subconscious.concern_store import load_register
    connect = connect or _connect
    work_brief = work_brief or _work_brief
    register = load_register()
    still_open = {c["concern_id"] for b in ("active", "addressing") for c in register.get(b) or []}
    start = day_window_local(day)[0].astimezone(timezone.utc).isoformat()
    with connect(False) as c:
        rows = c.execute("SELECT id, created_at, decisions FROM brain_matters WHERE status = 'applied' AND created_at >= ? "
                         "AND created_at <= ? ORDER BY id", (start, now_utc.isoformat())).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["decisions"])
            events, works = [], {}
            for ref, decided in d["events"].items():
                e = c.execute("SELECT source, occurred_at, text FROM brain_events WHERE source_ref = ?", (ref,)).fetchone()
                said = {"at": e["occurred_at"], "decision": decided["decision"], "reason": decided["reason"]}
                if e["source"] != "work":
                    events.append({"source": e["source"], "at": e["occurred_at"], "text": e["text"], **said})
                    continue
                work_id = ref.split(":")[1]   # work:<work_id>:<receipt id> (brain_inbox.add_work_event)
                if work_id not in works:
                    works[work_id] = {"source": "work", "at": e["occurred_at"], "work": work_brief(work_id),
                                      "decisions": []}
                    events.append(works[work_id])
                works[work_id]["decisions"].append(said)
            out.append({"id": r["id"], "at": r["created_at"], "events": events,
                        "notes": [n for n in d.get("concern_updates") or [] if n["concern_id"] not in still_open],
                        "new_concerns": d.get("new_concerns") or []})
    return out


def _work_brief(work_id: str) -> Dict[str, Any]:
    """One work as the look-back shows it: what it is, how it stands now, and why it ended."""
    from app.assistant.subconscious import work_links
    w = work_links.work_view(work_id, link="look-back")
    return {k: w[k] for k in ("work_id", "title", "status", "updated", "ended_because")}


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


def project(day: str, situation: Dict[str, Any], matters: List[Dict[str, Any]], own: List[Dict[str, Any]], *,
            connect=None) -> Dict[str, Any]:
    """The labels of this call, in place: each object the decider reads gets `ref` (own entries O; work W; chat M;
    reminders sent R; concerns C; concerns' entries E; the brain's decisions B; the schedule's lines K; the standing
    directives D). An entry's work gets its label (`ref` on the work view); what an entry's decision relies on is
    shown by label (`relies_on_labels`), and each such object not otherwise in view is labelled too and listed in
    `earlier`, with its data. Work made from a list entry names that entry by its label (`from_list`). Returns
    `labels` (label -> [kind, id], for mapping the answer back), `earlier`, and `source_labels` (work id -> label,
    read by shared/brain/past_work.j2)."""
    from app.assistant.subconscious.projection import Projection
    p = Projection()
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
    # A planner's proposal to change an entry names it by its label (None: the entry is no longer open).
    for x in [x for c in situation["concerns"] for x in (c.get("plan") or {}).get("proposals") or []]:
        x["ref"] = None if x["entry"] == "new" else p.label_of("list_entry", x["entry"])
    for m in matters:
        m["ref"] = p.label("B", "brain_matter", str(m["id"]))
    for e in situation["calendar_today"]:
        e["ref"] = p.label("K", *_schedule_ref(day, e))
    for line in situation["calendar_ahead"]:
        if line["anchor"]:
            line["ref"] = p.label("K", "calendar_event", line["anchor"][len("calendar:"):])
    entry_work = [e.get("work") for e in own] + [e.get("work") for c in situation["concerns"] for e in c.get("entries") or []]
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

    for e in own:
        e["relies_on_labels"] = [label_of(k, i) for k, i in e.get("relies_on") or []]
        e["concern_ref"] = label_of("concern", e["concern_id"]) if e.get("concern_id") else None
    # The look-back shows each work briefly (matters_today); its label leads to the work in full, in the situation's
    # work or, when not there, among the earlier things.
    for x in [x for m in matters for x in m["events"] if "work" in x]:
        x["work"]["ref"] = label_of("work", x["work"]["work_id"])
    for w in [*situation["work"]["in_motion"], *situation["work"]["ended"]]:
        entry = w.get("list_entry")
        label = p.label_of("list_entry", entry) if entry else None
        w["from_list"] = None if not entry else (
            f"your own entry {label}" if label and label.startswith("O") else
            f"the concern's entry {label}" if label else
            f"an entry on the list of {entry.split(':')[1]}" if entry.startswith(("cal:", "day:")) else
            _unshown_entry(entry, connect=connect))
    return {"labels": p.labels, "earlier": earlier,
            "source_labels": {i: lab for lab, (k, i) in p.labels.items() if k == "work"}}


def _unshown_entry(entry_id: str, *, connect=None) -> str:
    """Where work made from a concern's entry not in view came from. A cancelled entry's work is being ended
    (dayflow_orchestrator/brain_list_intake.py): on 2026-10-10 the evening air conditioner's work, its concern closed
    and its entry cancelled, read here only as "a concern's entry not shown here" while still in motion, and the
    decider gave the concern that replaced it no entry, as if that work covered it."""
    from app.assistant.subconscious import brain_list
    from app.assistant.utils.time_utils import local_time_text
    last = (brain_list.changes(entry_id, connect=connect) or [{}])[-1]
    if last.get("change") == "cancelled":
        return (f"a concern's entry cancelled {local_time_text(last['at'])}: this work is being ended and "
                "covers nothing now")
    return "a concern's entry not shown here"


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

def changeable(own: List[Dict[str, Any]], concerns: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """The entries an answer may modify or cancel by label: its own (O) and every open concern's (E). Label -> the
    label of the concern a concern's own entry belongs to (it stays with it), or None for a day's entry."""
    out: Dict[str, Optional[str]] = {e["ref"]: None for e in own}
    for c in concerns:
        for e in c.get("entries") or []:
            if e["ref"] not in out:
                out[e["ref"]] = c["ref"] if e["kind"] == "concern" else None
    return out


def entries_problems(data: Dict[str, Any], entries: Dict[str, Optional[str]], labels: Dict[str, Any]) -> List[str]:
    """The entries beyond the calendar: a new one is entry "new"; a modify or cancel names one of the entries given
    (`changeable`) by its label; new and modify name the outcome, the assistant's part and when; every one says why,
    in words, and lists in relies_on the labels of what prompted it; the concern an entry serves is an open concern's
    C label, and a concern's own entry stays with its concern."""
    changes = data.get("entries")
    if changes is None:
        return []
    if not isinstance(changes, list):
        return ["entries is not a list"]
    out, seen = [], []
    for c in changes:
        entry, action = str((c or {}).get("entry") or "").strip(), (c or {}).get("action")
        if action not in ("new", "modify", "cancel"):
            out.append(f"entry {entry!r}: action {action!r} is not new, modify or cancel")
            continue
        if action == "new" and entry != "new":
            out.append(f'a new entry is written as entry "new", got {entry!r}')
        if action != "new":
            if entry not in entries:
                out.append(f"{entry!r} is not one of the entries given (your own O, a concern's E)")
                continue
            seen.append(entry)
        if action in ("new", "modify"):
            missing = [f for f in ("what", "assistant_part", "when") if not str(c.get(f) or "").strip()]
            if missing:
                out.append(f"entry {entry}: {action} names {', '.join(missing)}")
        if not str(c.get("why") or "").strip():
            out.append(f"entry {entry}: why is empty")
        relied = [str(x).strip() for x in c.get("relies_on") or []]
        unknown = [x for x in relied if x not in labels]
        if not relied:
            out.append(f"entry {entry}: relies_on names nothing; name what prompted it")
        elif unknown:
            out.append(f"entry {entry}: relies_on {unknown}, which are not labels given")
        concern = str(c.get("concern") or "").strip()
        if concern and (labels.get(concern) or [None])[0] != "concern":
            out.append(f"entry {entry}: concern {concern!r} is not an open concern given (its C label)")
        owner = entries.get(entry) if action == "modify" else None
        if owner and concern and concern != owner:
            out.append(f"entry {entry}: it is {owner}'s entry and stays with it; leave concern empty or name {owner}")
    twice = sorted({x for x in seen if seen.count(x) > 1})
    if twice:
        out.append(f"entries changed more than once: {twice}")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="day_tasks", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::day_tasks", actor_id="subconscious::day_tasks")


def decided_placement(day: str, belief_key: str, *, connect=None) -> Dict[str, Any]:
    """The placement of `belief_key` as the newest run that decided it saw it (brain_day_decisions): its times,
    statement and related beliefs. What a calendar entry (before 2026-10-10) was decided from, read by the work made
    from it (dayflow_orchestrator/brain_list_intake.brief). Not the current plan: the day pass re-places the day as its
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


def gather(now_utc: datetime, *, plan_connect=None) -> Dict[str, Any]:
    """Everything the decision reads for the current day: the situation with every open concern (the day's calendar
    concerns among them), its own entries, and what the brain decided today."""
    from app.assistant.pipelines.context import boundary_date_local_str
    from app.assistant.utils.time_utils import get_local_timezone
    day = boundary_date_local_str(now_utc.astimezone(get_local_timezone()))
    from app.assistant.subconscious import brain_list, situation
    s = situation.gather(now_utc)
    with_concerns(s)
    matters = matters_today(day, now_utc)
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
    return {"day": day, "now": now_utc.isoformat(), "own": own, "situation": s, "matters": matters,
            "beliefs": concern_beliefs(s["concerns"]), "ended_relied_on": ended_relied_on(own, s["concerns"]),
            **project(day, s, matters, own), "correction": None}


def ended_relied_on(own: List[Dict[str, Any]], concerns: List[Dict[str, Any]], *, connect=None) -> List[str]:
    """The work that a decision on the list relied on and that has since ended (done or abandoned): its own entries'
    decisions and every open concern's, the cancelled ones included. Its ending is a reason to decide again
    (fingerprint)."""
    from app.assistant.subconscious import brain_list, work_links
    entry_ids = [e["id"] for e in own] + [e["id"] for c in concerns for e in brain_list.entries(
        concern_id=c["concern_id"], include_cancelled=True, connect=connect)]
    relied = {i for eid in entry_ids for kind, i in brain_list.relies_on(eid, connect=connect) if kind == "work"}
    if not relied:
        return []
    status = {r["id"]: r["status"] for r in work_links._rows()}
    return sorted(w for w in relied if status.get(w) in ("done", "abandoned"))


def concern_beliefs(concerns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The household's beliefs closest in meaning to the open concerns (their titles and the notes they were raised
    with; subconscious/belief_links.py, 2026-10-09): who usually does what, what the owner prefers, as context. The
    calendar's own beliefs come as its concerns."""
    from app.assistant.subconscious import belief_links
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    raised = {c["concern_id"]: c.get("notes") for b in ("active", "addressing") for c in register.get(b) or []}
    return belief_links.beliefs_by_meaning([t for c in concerns for t in (c.get("title"), raised.get(c["concern_id"]))])


def run_day_tasks(*, now_utc: Optional[datetime] = None, connect=None,
                  gather_inputs: Callable[[datetime], Dict[str, Any]] = None,
                  call: Callable[[Dict[str, Any]], Any] = None) -> Dict[str, Any]:
    """Decide the list (every open concern's entries, the day's own) when a stage before it changed since the last run
    (fingerprint); record the run. The keyword arguments are test seams. Raises when the answer is still invalid after
    one correction."""
    from app.assistant.subconscious.brain_trace import step
    now = now_utc or datetime.now(timezone.utc)
    connect = connect or _connect
    ensure_schema(connect)
    inputs = (gather_inputs or gather)(now)
    if not (inputs["situation"]["concerns"] or inputs.get("own")):
        return {"day_tasks": "nothing to decide"}
    fp = fingerprint(inputs)
    if fp == _last_fingerprint(inputs["day"], connect):
        return {"day_tasks": "unchanged"}
    call = call or _agent_call
    entries = changeable(inputs.get("own") or [], inputs["situation"]["concerns"])

    def check(answer):
        if not isinstance(answer, dict) or not isinstance(answer.get("entries"), list):
            return ["no entries list"]
        return entries_problems(answer, entries, inputs["labels"])
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
    entry_changes = [{**{k: str(c.get(k) or "").strip() for k in ("action", "what", "assistant_part", "when", "why")},
                      "entry": "new" if c.get("action") == "new" else resolve(call_labels, c["entry"])[1],
                      "concern_id": resolve(call_labels, c["concern"])[1] if str(c.get("concern") or "").strip()
                      else None,
                      "relies_on": [resolve(call_labels, x) for x in c.get("relies_on") or []]}
                     for c in data.get("entries") or []]
    changed = brain_list.apply_entries(inputs["day"], entry_changes, cause=f"day's tasks {now.isoformat()}", at=now,
                                       connect=connect) if entry_changes else {}
    # The run, recorded once its entries are on the list (the table's `items` held the day's placements until
    # 2026-10-09; `decisions` holds the answer's entries).
    with connect(True) as c:
        c.execute("INSERT INTO brain_day_decisions (day, decided_at_utc, fingerprint, items, decisions) "
                  "VALUES (?,?,?,?,?)",
                  (inputs["day"], now.isoformat(), fp, "[]", json.dumps(data["entries"], ensure_ascii=False)))
    step("day_tasks", "decided", subject=inputs["day"], list_changes=changed, entries=data["entries"])
    logger.info("[day_tasks] %s: list %s", inputs["day"], changed)
    return {"day_tasks": "decided", "day_list_changes": changed}
