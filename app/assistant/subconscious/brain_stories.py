"""The brain's work told to the owner: the stories behind the /mind page (2026-10-02,
docs/design/brain_insight_2026-10-02.md).

Owner, 2026-10-02: "you see the high overview summary of all the things coming to Emis brain then you
see summary of the reasoning with each item like what created a new concern and then for each concern
that has been sent somewhere you see summary of what happened/is happening to that concern item and
then ... the summary about what tasks are proposed and what will be the concerns faith going forward.
you will need auxilary agents to write all the summaries."

Three agents write them, each from records that are already kept:
- `subconscious::story_matter`, per matter the brain read (`brain_matters`): a headline and one line
  per event, from the events, the gate's routes, the brain's decisions and the concern door's outcome;
- `subconscious::story_concern`, per open concern with a brief: a state label, the story so far, the
  things proposed now and its likely fate, from the brief, the work done for it and the latest plan;
- `subconscious::story_day`, per local day: a headline and an overview, from the day's matter stories,
  counts by source, concerns opened and settled, and how much was handed on.

Each story is stored in `brain_stories` with the fingerprint of the input it was written from and
written again only when that changes; one that failed waits for a change, as briefs do. The brain's wake
runs this last (subconscious/brain_wake.py): matter stories first, since the day is told from them. Only
the last STORY_DAYS local days are told; older matters keep the stories they had.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

STORY_DAYS = 2
STORY_VERSION = 1          # part of every story's basis: raising it tells every story again
SCHEMA = """CREATE TABLE IF NOT EXISTS brain_stories (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    basis TEXT NOT NULL,
    written_at TEXT NOT NULL,
    body TEXT,
    error TEXT,
    PRIMARY KEY (kind, key))"""
# kind: matter (key: brain_matters.id) | concern (key: concern_id) | day (key: local YYYY-MM-DD)

_AGENTS = {"matter": "subconscious::story_matter", "concern": "subconscious::story_concern",
           "day": "subconscious::story_day"}


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)


def _basis(value: Any) -> str:
    return hashlib.sha256(json.dumps([STORY_VERSION, value], sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def load(kind: str, connect=None) -> Dict[str, Dict[str, Any]]:
    """Every stored story of one kind, by key: {basis, written_at, body, error}."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        rows = c.execute("SELECT key, basis, written_at, body, error FROM brain_stories WHERE kind = ?", (kind,)).fetchall()
    return {r[0]: {"basis": r[1], "written_at": r[2], "body": json.loads(r[3]) if r[3] else None, "error": r[4]}
            for r in rows}


def _store(kind: str, key: str, basis: str, *, body: Optional[Dict[str, Any]] = None,
           error: Optional[str] = None, connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute("INSERT INTO brain_stories (kind, key, basis, written_at, body, error) VALUES (?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(kind, key) DO UPDATE SET basis = excluded.basis, written_at = excluded.written_at, "
                  "body = CASE WHEN excluded.error IS NULL THEN excluded.body ELSE brain_stories.body END, "
                  "error = excluded.error",
                  (kind, key, basis, datetime.now(timezone.utc).isoformat(),
                   json.dumps(body, ensure_ascii=False) if body is not None else None, error))


# ── the records ─────────────────────────────────────────────────────────────

def _local(iso: str) -> datetime:
    from app.assistant.utils.time_utils import utc_to_local
    return utc_to_local(iso)


def _day_bounds(day: str) -> Tuple[str, str]:
    """A local day as [start, end) UTC isoformats."""
    from app.assistant.utils.time_utils import get_local_timezone
    start = datetime.fromisoformat(day).replace(tzinfo=get_local_timezone())
    return start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()


def _window_days(now_utc: datetime) -> List[str]:
    today = _local(now_utc.isoformat()).date()
    return [(today - timedelta(days=k)).isoformat() for k in range(STORY_DAYS - 1, -1, -1)]


def _matters(start_iso: str, end_iso: str, connect) -> List[Dict[str, Any]]:
    """Matters created in [start, end), oldest first, each with its events."""
    from app.assistant.subconscious import brain_inbox, brain_step
    brain_inbox.ensure_schema(connect)
    brain_step.ensure_schema(connect)
    with connect(False) as c:
        matters = [dict(r) for r in c.execute("SELECT * FROM brain_matters WHERE created_at >= ? AND created_at < ? "
                                              "ORDER BY created_at, id", (start_iso, end_iso))]
        for m in matters:
            ids = json.loads(m["event_ids"])
            m["events"] = [dict(r) for r in c.execute(
                f"SELECT * FROM brain_events WHERE id IN ({','.join('?' * len(ids))}) ORDER BY occurred_at, id", ids)]
            m["decisions"] = json.loads(m["decisions"]) if m["decisions"] else None
            m["admitted"] = json.loads(m["admitted"]) if m["admitted"] else {}
    # Since 2026-10-08 an update's events are its relations (subconscious/relations.py), read back onto it here.
    from app.assistant.subconscious import relations
    for m in matters:
        for u in (m["decisions"] or {}).get("concern_updates") or []:
            if "events" not in u:
                key = ("brain_update", f"{m['id']}:{u['concern_id']}")
                relied = [list(r) for r in relations.targets(key, "relies_on", connect=connect)]
                attached = [list(r) for r in relations.targets(key, "attach", connect=connect)]
                if relied or attached:          # a matter recorded before either kept neither
                    u["relies_on"], u["attach"] = relied, attached
    return matters


def _events(start_iso: str, end_iso: str, connect) -> List[Dict[str, Any]]:
    from app.assistant.subconscious import brain_inbox
    brain_inbox.ensure_schema(connect)
    with connect(False) as c:
        return [dict(r) for r in c.execute("SELECT * FROM brain_events WHERE occurred_at >= ? AND occurred_at < ? "
                                           "ORDER BY occurred_at, id", (start_iso, end_iso))]


def _every_concern(register: Dict[str, Any]) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    return {c["concern_id"]: (b, c) for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []}


def _handed_on(start_iso: str, end_iso: str) -> int:
    """Concern handoffs written to dayflow in [start, end)."""
    from app.models.db_manager import get_db_manager
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID
    from app.assistant.subconscious.concern_handoff import SOURCE_TYPE
    with get_db_manager().read_session() as session:
        raw = session.connection().connection.driver_connection
        return raw.execute("SELECT count(*) FROM unified_log_2026 WHERE source=? AND room_id=? "
                           "AND json_extract(metadata_json, '$.source_type') = ? "
                           "AND json_extract(metadata_json, '$.created_at') >= ? "
                           "AND json_extract(metadata_json, '$.created_at') < ?",
                           (DAYFLOW_ITEM_SOURCE, DAYFLOW_ROOM_ID, SOURCE_TYPE, start_iso, end_iso)).fetchone()[0]


# ── a matter ────────────────────────────────────────────────────────────────

_DOOR = {"create": "opened as a new concern", "merge_open": "joined the open concern “{into}”",
         "suppress_closed": "matched “{into}”, which is already settled",
         "merge_candidate": "joined another new concern raised here"}
_SOURCES = {"chat": "chat message", "email": "email", "work": "report on work done", "camera": "camera sighting",
            "presence": "presence at the computer", "time": "a concern's day ending"}
_PLURALS = {"chat": "chat messages", "email": "emails", "work": "reports on work done", "camera": "camera sightings",
            "presence": "comings and goings at the computer", "time": "concerns whose day ended"}


def _event_who(e: Dict[str, Any]) -> str:
    return "" if e["source"] == "work" else str(e.get("speaker") or "")


def _route(e: Dict[str, Any], titles: Dict[str, str]) -> str:
    if e.get("gate_status") == "failed":
        return "could not be routed; read against everything"
    if e.get("route") == "new_matter":
        return "a new matter"
    if e.get("route") == "concern":
        return "bears on " + "; ".join(titles.get(cid, "a concern no longer kept") for cid in json.loads(e["concern_ids"] or "[]"))
    return str(e.get("route") or "")


def _conversation(matter: Dict[str, Any], now_utc: datetime) -> str:
    """The matter's events inside the conversations they happened in, each marked with its label, as the
    gate and the brain read them: a reply is read as a reply (2026-10-02: "yes, its they serve horses
    don't they", the owner's answer to a question about a TV episode, was told as the owner asking it)."""
    from app.assistant.subconscious import conversations
    marked = [{**e, "concern_ids": json.loads(e.get("concern_ids") or "[]"), "mark": f"E{i}"}
              for i, e in enumerate(matter["events"], 1)]
    return conversations.render(conversations.build(marked, now_utc=now_utc))


def matter_input(matter: Dict[str, Any], register: Dict[str, Any], now_utc: datetime) -> Dict[str, Any]:
    """`conversation` is the events in their conversations; the rest says what became of each. Only the
    rest is the story's basis: a conversation that goes on after the matter does not retell it."""
    every = _every_concern(register)
    titles = {cid: c.get("title") for cid, (_, c) in every.items()}
    decided = (matter["decisions"] or {}).get("events") or {}
    events = [{"label": f"E{i}", "when": _local(e["occurred_at"]).strftime("%a %H:%M"),
               "source": _SOURCES.get(e["source"], e["source"]), "who": _event_who(e),
               "route": _route(e, titles), "decision": (decided.get(e["source_ref"]) or {}).get("decision", ""),
               "reason": (decided.get(e["source_ref"]) or {}).get("reason", "")}
              for i, e in enumerate(matter["events"], 1)]
    new = []
    for n in (matter["decisions"] or {}).get("new_concerns") or []:
        landed = matter["admitted"].get(n["label"])
        door = n.get("door")
        outcome = (_DOOR[door].format(into=titles.get(landed) or "") if door in _DOOR
                   else f"kept as “{titles[landed]}”" if landed in titles else "not kept")
        new.append({"title": n["title"], "outcome": outcome})
    return {"conversation": _conversation(matter, now_utc), "events": events,
            "concern_updates": [{"concern": titles.get(u["concern_id"], "a concern no longer kept"),
                                 "action": "settled it" if u["action"] == "resolve" else "noted",
                                 "note": u["note"]} for u in (matter["decisions"] or {}).get("concern_updates") or []],
            "new_concerns": new}


def matter_problems(data: Any, labels: List[str]) -> List[str]:
    if not isinstance(data, dict):
        return ["no story"]
    out = [] if str(data.get("headline") or "").strip() else ["headline is empty"]
    given = [str((x or {}).get("event") or "").strip() for x in data.get("lines") or []]
    for x in data.get("lines") or []:
        if not str((x or {}).get("line") or "").strip():
            out.append(f"the line for {(x or {}).get('event')!r} is empty")
    unknown = [g for g in given if g not in labels]
    if unknown:
        out.append(f"{unknown} are not event labels given")
    repeated = sorted({g for g in given if given.count(g) > 1})
    if repeated:
        out.append(f"more than one line for {repeated}")
    missing = [lab for lab in labels if lab not in given]
    if missing:
        out.append(f"no line for {missing}")
    return out


# ── a concern ───────────────────────────────────────────────────────────────

def concern_input(concern: Dict[str, Any], bucket: str, now_utc: datetime) -> Dict[str, Any]:
    """The concern as its story is told from it."""
    from app.assistant.subconscious import concern_brief
    brief = concern["brief"]
    return {"concern": {
        "title": concern.get("title"), "status": bucket,
        "done_when": concern.get("done_when"), "owner_words": (concern.get("owner_request") or {}).get("words"),
        "brief": {k: brief.get(k) for k in ("what", "why_it_matters", "known", "tried", "owner_wishes",
                                            "open_questions")},
        "attempts": concern_brief.attempts(concern),
        "plan": {k: (concern.get("plan") or {}).get(k) for k in ("decision", "todo", "why", "hold_until")}
        if concern.get("plan") else None,
    }}


def _decision(inp: Dict[str, Any]) -> Optional[str]:
    c = inp["concern"]
    return (c["plan"] or {}).get("decision")


def concern_problems(data: Any, decision: Optional[str]) -> List[str]:
    if not isinstance(data, dict):
        return ["no story"]
    out = [f"{f} is empty" for f in ("state", "story", "fate") if not str(data.get(f) or "").strip()]
    proposed = [p for p in data.get("proposed") or [] if str(p).strip()]
    if decision != "act_now" and proposed:
        out.append(f"the decision is {decision or 'not made'}: nothing is proposed now, so proposed stays empty")
    return out


# ── a day ───────────────────────────────────────────────────────────────────

def day_input(day: str, now_utc: datetime, *, connect, register: Dict[str, Any], matter_stories: Dict[str, Dict],
              handed_on: int) -> Dict[str, Any]:
    start, end = _day_bounds(day)
    counts: Dict[str, int] = {}
    for e in _events(start, end, connect):
        label = _PLURALS.get(e["source"], e["source"])
        counts[label] = counts.get(label, 0) + 1
    matters = []
    for m in _matters(start, end, connect):
        body = (matter_stories.get(str(m["id"])) or {}).get("body")
        if body:
            matters.append({"when": _local(m["created_at"]).strftime("%H:%M"), "headline": body["headline"],
                            "lines": [x["line"] for x in body["lines"]]})
    every = _every_concern(register).values()
    within = lambda iso: bool(iso) and start <= str(iso) < end                      # noqa: E731
    return {"day": _local(start).strftime("%A %Y-%m-%d"), "today": day == _window_days(now_utc)[-1],
            "counts": counts, "matters": matters,
            "opened": [c.get("title") for _, c in every if within(c.get("created_at_utc"))],
            "settled": [c.get("title") for b, c in every if b == "resolved" and within(c.get("resolved_at_utc"))],
            "handed_on": handed_on}


def day_problems(data: Any) -> List[str]:
    if not isinstance(data, dict):
        return ["no story"]
    return [f"{f} is empty" for f in ("headline", "overview") if not str(data.get(f) or "").strip()]


# ── writing ─────────────────────────────────────────────────────────────────

def _agent_call(kind: str) -> Callable[[Dict[str, Any]], Any]:
    def call(payload: Dict[str, Any]) -> Any:
        from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
        return _run_subconscious_agent(handler_label=f"story_{kind}", agent_name=_AGENTS[kind], context=payload,
                                       scope_id=_AGENTS[kind], actor_id=_AGENTS[kind])
    return call


def _tell(kind: str, key: str, payload: Dict[str, Any], check: Callable[[Any], List[str]], call) -> Dict[str, Any]:
    """One story: the agent's answer, checked, with one correction; raises when still invalid."""
    from app.assistant.subconscious.brain_trace import step
    data = call(payload)
    problems = check(data)
    if problems:
        logger.warning("[stories] invalid %s story for %s, one correction: %s", kind, key, problems)
        step("stories", "answer refused, one correction", subject=key, kind=kind, problems=problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = check(data)
        if problems:
            raise ValueError(f"{kind} story still invalid after one correction: {problems}")
    return data


def _write(kind: str, key: str, inp: Dict[str, Any], check, call, stored, now_utc: datetime, connect) -> Optional[bool]:
    """Write one story when its input changed (a matter's conversation aside). True: written; False:
    failed; None: already current."""
    from app.assistant.subconscious.brain_trace import step, trace
    basis = _basis({k: v for k, v in inp.items() if k != "conversation"})
    prior = stored.get(key)
    if prior is not None and prior["basis"] == basis:
        return None
    from app.assistant.utils.time_utils import utc_to_local
    payload = {"now": utc_to_local(now_utc.isoformat()).strftime("%a %Y-%m-%d %H:%M"), **inp}
    try:
        with trace(stage="stories", story=f"{kind}:{key}"):
            data = _tell(kind, key, payload, check, call)
    except Exception as exc:
        logger.error("[stories] could not write the %s story for %s; not retried until it changes: %s",
                     kind, key, exc, exc_info=True)
        step("stories", "failed: not retried until its record changes", subject=key, kind=kind,
             error=f"{type(exc).__name__}: {exc}")
        _store(kind, key, basis, error=f"{type(exc).__name__}: {exc}", connect=connect)
        return False
    body = {k: data.get(k) for k in data}
    _store(kind, key, basis, body=body, connect=connect)
    step("stories", "written", subject=key, kind=kind)
    stored[key] = {"basis": basis, "body": body, "error": None}
    return True


def run_stories(*, call: Optional[Callable[[str], Callable]] = None, connect=None, register_connect=None,
                now_utc: Optional[datetime] = None, handed_on: Callable[[str, str], int] = None) -> Dict[str, Any]:
    """Tell what changed in the last STORY_DAYS days: matters first, then concerns, then the days. Free
    when nothing changed. `call(kind)` gives the agent call for a kind; the keyword arguments are test
    seams."""
    from app.assistant.subconscious.concern_store import load_register
    connect = connect or _connect
    ensure_schema(connect)
    now = now_utc or datetime.now(timezone.utc)
    call = call or _agent_call
    handed_on = handed_on or _handed_on
    register = load_register(connect=register_connect)
    written = failed = 0

    def count(result: Optional[bool]) -> None:
        nonlocal written, failed
        written += result is True
        failed += result is False

    days = _window_days(now)
    stored = load("matter", connect)
    start, end = _day_bounds(days[0])[0], _day_bounds(days[-1])[1]
    for m in _matters(start, end, connect):
        if m["status"] != "applied":
            continue
        inp = matter_input(m, register, now)
        labels = [e["label"] for e in inp["events"]]
        count(_write("matter", str(m["id"]), inp, lambda d, labels=labels: matter_problems(d, labels),
                     call("matter"), stored, now, connect))

    concerns = load("concern", connect)
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            if not c.get("brief"):
                continue
            inp = concern_input(c, bucket, now)
            decision = _decision(inp)
            count(_write("concern", c["concern_id"], inp, lambda d, decision=decision: concern_problems(d, decision),
                         call("concern"), concerns, now, connect))

    day_stories = load("day", connect)
    for day in days:
        d_start, d_end = _day_bounds(day)
        inp = day_input(day, now, connect=connect, register=register, matter_stories=stored,
                        handed_on=handed_on(d_start, d_end))
        if not inp["counts"] and not inp["matters"]:
            continue
        count(_write("day", day, inp, day_problems, call("day"), day_stories, now, connect))
    if written or failed:
        logger.info("[stories] written=%d failed=%d", written, failed)
    return {"stories_written": written, "stories_failed": failed}


# ── reading, for the page ───────────────────────────────────────────────────

_TAGS = {"used": ("noted", "noted"), "tracked_as_new": ("new concern", "new"),
         "needs_nothing": ("set aside", "aside")}


def _settled(e: Dict[str, Any], matter: Dict[str, Any]) -> bool:
    """The event settled a concern: a resolve cites it. Matters recorded before updates kept their events
    (2026-10-02) count when every update in them is a resolve."""
    updates = (matter["decisions"] or {}).get("concern_updates") or []
    if any("relies_on" in u for u in updates):       # since 2026-10-08: events named as objects (relies_on, attach)
        ref = ["brain_event", str(e["id"])]
        return any(u["action"] == "resolve" and (ref in u["relies_on"] or ref in u["attach"]) for u in updates)
    if any("events" in u for u in updates):
        return any(u["action"] == "resolve" and e["source_ref"] in u.get("events", []) for u in updates)
    return bool(updates) and all(u["action"] == "resolve" for u in updates)


def _tag(e: Dict[str, Any], matter: Dict[str, Any]) -> Tuple[str, str]:
    decided = ((matter["decisions"] or {}).get("events") or {}).get(e["source_ref"]) or {}
    if decided.get("decision") == "used" and _settled(e, matter):
        return "settled", "settled"
    if decided.get("decision") == "tracked_as_new":
        doors = {n.get("door") for n in (matter["decisions"] or {}).get("new_concerns") or []}
        if doors and doors <= {"merge_open", "merge_candidate"}:
            return "joined a concern", "noted"
        if doors == {"suppress_closed"}:
            return "already settled", "aside"
    return _TAGS.get(decided.get("decision"), ("", ""))


def day_view(day: str, *, now_utc: Optional[datetime] = None, connect=None, register_connect=None,
             handed_on: Callable[[str, str], int] = None) -> Dict[str, Any]:
    """Everything the owner's page shows for one local day: the day's story and ribbon, each matter
    with its story, what was set aside without one, every open concern's story, and the day's calendar
    (the beliefs placed at their times, subconscious/day_plan.py)."""
    from app.assistant.subconscious.concern_store import load_register
    connect = connect or _connect
    ensure_schema(connect)
    now = now_utc or datetime.now(timezone.utc)
    register = load_register(connect=register_connect)
    start, end = _day_bounds(day)
    every = _every_concern(register)
    matter_stories, concern_stories = load("matter", connect), load("concern", connect)
    events = _events(start, end, connect)
    by_source: Dict[str, int] = {}
    for e in events:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
    matters, touched = [], set()
    for m in _matters(start, end, connect):
        story = matter_stories.get(str(m["id"])) or {}
        lines = {x["event"]: x["line"] for x in ((story.get("body") or {}).get("lines") or [])}
        touched |= set(json.loads(m["concern_ids"] or "[]")) | {v for v in (m["admitted"] or {}).values() if v}
        items = []
        for i, e in enumerate(m["events"], 1):
            tag, tag_kind = _tag(e, m)
            items.append({"source": e["source"], "who": _event_who(e), "when": e["occurred_at"], "text": e["text"],
                          "line": lines.get(f"E{i}"), "tag": tag, "tag_kind": tag_kind})
        matters.append({"id": m["id"], "at": m["created_at"], "status": m["status"], "error": m["error"],
                        "headline": (story.get("body") or {}).get("headline"), "story_error": story.get("error"),
                        "items": items})
    set_aside = [{"source": e["source"], "who": _event_who(e), "when": e["occurred_at"], "text": e["text"],
                  "reason": e.get("gate_reasoning")} for e in events if e.get("route") == "none"]
    concerns = []
    for cid, (bucket, c) in every.items():
        if bucket not in ("active", "addressing"):
            continue
        story = concern_stories.get(cid) or {}
        plan = c.get("plan") or {}
        concerns.append({
            "concern_id": cid, "title": c.get("title"), "status": bucket, "story": story.get("body"),
            "story_error": story.get("error"), "written_at": story.get("written_at"),
            "decision": plan.get("decision"), "hold_until": plan.get("hold_until"),
            "work": len(c.get("attached_work") or {})})
    from app.assistant.subconscious import day_plan
    plan = day_plan.load_plan(day, connect=connect)
    calendar = [{"time": line["time"], "statement": line["statement"]}
                for line in ([*plan["flags"], *(line for s in plan["slots"] for line in s["lines"])] if plan else [])]
    settled = [{"concern_id": cid, "title": c.get("title"), "reason": c.get("resolution_reason")}
               for cid, (b, c) in every.items() if b == "resolved" and start <= str(c.get("resolved_at_utc") or "") < end]
    day_story = load("day", connect).get(day) or {}
    stored_days = sorted(set(load("day", connect)) | set(_window_days(now)), reverse=True)
    return {"day": day, "today": day == _window_days(now)[-1], "days": stored_days,
            "story": day_story.get("body"), "story_error": day_story.get("error"),
            "ribbon": {"came_in": len(events), "by_source": by_source, "matters": len(matters),
                       "concerns_touched": len(touched), "handed_on": (handed_on or _handed_on)(start, end),
                       "settled": len(settled)},
            "matters": matters, "set_aside": set_aside, "concerns": concerns, "calendar": calendar,
            "settled": settled}
