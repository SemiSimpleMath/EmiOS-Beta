"""The brain step: one matter at a time, from the events the gate passed on to the concerns (2026-09-30).

The gate routes every inbox event against the open concerns. What it passes on (bears on concerns,
a new matter, or could not be routed) comes here, and the brain decides what it changes. Before
this, the noticer read all of it once a day, in one call, beside twenty other context sections.

A matter is a group of events read together:
- events the gate routed to the same concerns;
- new-matter or unrouted events from the same conversation: a chat room, a Gmail thread, or a camera.
Dayflow's reports on attached work (finalizer judgments, endings) arrive routed to their concerns.
A split that leaves two matters about one thing is caught by the concern door, which folds the
second concern into the first.

For each matter `subconscious::brain` gets:
- the events inside their conversations or threads (subconscious/conversations.py), each with the
  gate's route;
- the chat summaries (chat_cluster pods) of those rooms from the 48 hours before the first event,
  and any since, so an event is read with the earlier conversations about it, not just this one;
- the record of every concern the events bear on: what it is, where it came from (its source, in its own words),
  what settles it, when it expires, its summary and the notes since, and the work made for it, read live;
- the other open concerns, briefly;
- past work (subconscious/work_links.py): work linked exactly (it cites one of the matter's
  concerns, or came from an email in one of its Gmail threads) and similar past work by meaning;
- what is already in motion: active work, live scheduled reminders, and the calendar for the next
  CALENDAR_DAYS, so the brain can name everything that depends on a fact that changed;
- the people, places and things the events name, from the knowledge graph, each with its ties to the
  household's people, and what joins them; and the knowledge graph's facts closest in meaning to the
  events, which say whose school or club an event is about when it does not (subconscious/kg_links.py);
- the household's beliefs closest in meaning to the events and their concerns (subconscious/belief_links.py,
  2026-10-09): what was learned about the household's habits, routines and wishes, as context.

It answers with a decision on every event (it changes one concern, a new concern comes from it, or it changes
no concern), notes and resolutions on the concerns the events change, and new concerns, each with what settles it
and when it expires (docs/design/concerns_as_state_2026-10-09.md, rules 3, 4 and 6). One event changes one concern:
the update of a concern names only events decided to change it. A new concern's source is the first event it comes
from, copied by code. Code validates the answer (one correction round), admits new concerns through the concern
door, applies the rest under the register lock, marks the events consumed and records the matter in `brain_matters`.
A matter that still fails is recorded failed and not retried automatically: its events stay in
the inbox, visible, instead of being dropped or re-sent every five minutes.

What the assistant did reaches the brain as `work` events, routed by the gate like chat and email
(docs/design/what_was_done_reaches_the_brain_2026-10-03.md). Work belongs to the concern its list entry was made for,
by code; until 2026-10-09 the brain also attached work to concerns by judgment (`attach_seen_work`), and a dog-walk
follow-up landed on a friend's birthday.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::brain"
ROOM_HISTORY = timedelta(hours=48)
CALENDAR_DAYS = 30

SCHEMA = """CREATE TABLE IF NOT EXISTS brain_matters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_ids TEXT NOT NULL,
    concern_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    decisions TEXT,
    admitted TEXT,
    error TEXT)"""
# status: applied | failed. decisions: the brain's answer with labels mapped to event refs and
# concern ids. admitted: new-concern label -> the concern it landed in.


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)


def _utc(value: Any) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


# ── which events, grouped how ───────────────────────────────────────────────

def pending_events(connect=None) -> List[Dict[str, Any]]:
    """Events the gate passed on (or could not route) that no matter has consumed or failed on."""
    from app.assistant.subconscious import brain_inbox
    connect = connect or _connect
    brain_inbox.ensure_schema(connect)
    ensure_schema(connect)
    with connect(False) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM brain_events WHERE consumed_at IS NULL AND "
            "(gate_status='failed' OR (gate_status='routed' AND route IN ('concern','new_matter'))) "
            "AND id NOT IN (SELECT j.value FROM brain_matters m, json_each(m.event_ids) j WHERE m.status='failed') "
            "ORDER BY occurred_at, id")]
    for r in rows:
        r["concern_ids"] = json.loads(r["concern_ids"]) if r.get("concern_ids") else []
    return rows


def group_matters(events: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Events routed to the same concerns form one matter; other events group by conversation."""
    from app.assistant.subconscious import conversations
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    for e in events:
        if e.get("gate_status") == "routed" and e.get("route") == "concern":
            key: Tuple = ("concerns", tuple(sorted(e["concern_ids"])))
        elif e["source"] == "email":
            record = conversations._load_email(e["source_ref"])
            key = ("thread", record.get("account_id"), record.get("thread_id") or e["source_ref"])
        elif e["source"] == "camera":
            key = ("camera", e["speaker"])
        elif e["source"] == "presence":
            key = ("presence",)
        elif e["source"] == "calendar":
            key = ("calendar",)
        elif e["source"] == "ticket":
            key = ("tickets",)
        elif e["source"] == "noticer":
            key = ("noticer", e["id"])          # each issue its own matter
        else:
            key = ("room", e.get("room_id"))
        groups.setdefault(key, []).append(e)
    return sorted(groups.values(), key=lambda g: (g[0]["occurred_at"], g[0]["id"]))


# ── what the brain reads ────────────────────────────────────────────────────

def _gate_note(e: Dict[str, Any], titles: Dict[str, str], closed: Dict[str, str]) -> str:
    """The gate's routing, in words: open concerns (`titles`) and closed ones (`closed`) by title, never by id."""
    if e.get("gate_status") == "failed":
        return "gate: could not route this one; read it against every concern and as a new matter"
    if e.get("route") == "new_matter":
        return "gate: new matter, no open concern covers it"
    return "gate: bears on " + "; ".join(
        f'"{titles[cid]}"' if cid in titles else f'"{closed.get(cid) or "a concern no longer kept"}" (no longer open)'
        for cid in e["concern_ids"])


def _message_text(message_id: str) -> str:
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.models.base import get_session
    from app.assistant.utils.time_utils import utc_to_local
    session = get_session()
    try:
        row = session.query(UnifiedLog2026).filter(UnifiedLog2026.id == message_id).first()
        if row is None:
            return "(this message is no longer stored)"
        when = utc_to_local(_utc(row.timestamp).isoformat()).strftime("%a %Y-%m-%d %H:%M")
        speaker = "assistant" if row.role == "assistant" else (row.speaker_name or "user")
        from app.assistant.pod_store.image_pod_enrichment import with_images
        return f"{when} in {row.room_id}, {speaker}: {with_images(row.message or '')}"
    finally:
        session.close()


def _pod_text(pod_id: str) -> str:
    from app.assistant.pod_store.pod_store import PodStore
    pod = PodStore().get(pod_id)
    if pod is None:
        return "(this pod is no longer stored)"
    return f"{pod.one_liner}\n{pod.body or ''}"


def _evidence_text(e: Dict[str, Any]) -> str:
    """Evidence as the words it points to: a chat message, a pod; other kinds by their snippet."""
    ref = str(e.get("ref") or "")
    if ref.startswith("message:"):
        return _message_text(ref[len("message:"):])
    if ref.startswith("datapod:"):
        return _pod_text(ref)
    return str(e.get("snippet") or "")


def _room_history(room_id: str, before: datetime) -> List[Dict[str, Any]]:
    """The room's chat summaries (chat_cluster pods) from the ROOM_HISTORY before `before`, oldest first."""
    from app.assistant.pod_store.pod_store import PodStore
    from app.assistant.utils.time_utils import utc_to_local
    pods = PodStore().query(kind="chat_cluster", scope=room_id, since_utc=before - ROOM_HISTORY, limit=None)
    pods = sorted(pods, key=lambda p: p.created_at)
    return [{"when": utc_to_local(_utc(p.created_at).isoformat()).strftime("%a %Y-%m-%d %H:%M"),
             "title": p.one_liner, "body": p.body or "", "pod_id": p.pod_id} for p in pods]


def _calendar_lines(now_utc: datetime) -> List[Dict[str, Any]]:
    """The calendar from now through CALENDAR_DAYS as lines of data (context_builder.fetch_calendar_lines), for a
    projection to label."""
    from app.assistant.subconscious.context_builder import fetch_calendar_lines
    from app.assistant.utils.time_utils import utc_to_local
    start = utc_to_local(now_utc.isoformat())
    return fetch_calendar_lines(start, start + timedelta(days=CALENDAR_DAYS), label=f"next {CALENDAR_DAYS} days")


def _calendar(now_utc: datetime) -> str:
    """The calendar from now through CALENDAR_DAYS, each event line with its calendar:<id> anchor."""
    from app.assistant.subconscious.context_builder import _fetch_calendar_text
    from app.assistant.utils.time_utils import utc_to_local
    start = utc_to_local(now_utc.isoformat())
    return _fetch_calendar_text(start, start + timedelta(days=CALENDAR_DAYS), label=f"next {CALENDAR_DAYS} days")


def _concern_view(label: str, c: Dict[str, Any]) -> Dict[str, Any]:
    """One concern as the brain reads it (rule 1): what it is, where it came from, what settles it, when it expires,
    the work made for it, and its record (the summary and the notes since). Its evidence list is not shown: the notes
    say what each event established (until 2026-10-09 every piece of evidence was rendered, fifteen presence events
    on one concern)."""
    from app.assistant.subconscious import concern_view, persist
    return {
        "label": label, "title": c.get("title"), "status": c["_status"], "subject": c.get("subject") or "household",
        "kind": c.get("kind"), "severity": c.get("severity"), "horizon": c.get("horizon"),
        "source": concern_view.source_text(c), "done_when": c.get("done_when"),
        "expires": concern_view.expires_text(c), "first_observed": c.get("first_observed"),
        "attached_work": concern_view.attempts(c),
        "history": persist.history_for_prompt(c),
    }


# What `attach` may name: an event (its message, email, frame or work report), kept as the concern's evidence.
_ATTACHABLE = ("brain_event",)
# Where a concern raised from an event came from (rule 6): the owner, in chat or a ticket reply, or the event's sense.
_SOURCE_KIND = {"chat": "user", "ticket": "user", "email": "email", "calendar": "calendar", "camera": "camera",
                "presence": "presence", "work": "work", "noticer": "noticer"}


def _source(event: Dict[str, Any]) -> Dict[str, Any]:
    """A new concern's source: the event it comes from, its own words copied by code."""
    return {"kind": _SOURCE_KIND[event["source"]], "at": event["occurred_at"], "ref": event["source_ref"],
            "content": event["text"]}


def build_payload(events: List[Dict[str, Any]], register: Dict[str, Any],
                  now_utc: Optional[datetime] = None,
                  calendar: Optional[List[Dict[str, Any]]] = None
                  ) -> Tuple[Dict[str, Any], Dict[str, Dict], Dict[str, str], Dict[str, List[str]]]:
    """The brain's input for one matter, a projection of objects (docs/design/objects_and_projections_2026-10-08.md):
    each object it shows carries a label of the call and never its id: events E, concerns C, work W, a concern's
    evidence by what it points to (messages M, emails and other pods P, calendar events K, other sources S),
    reminders R, calendar lines K. Returns (payload, event by label, concern_id by label, the call's labels: label ->
    [kind, id]). `calendar` is the run's calendar lines (_calendar_lines), read once per run; omitted, read here."""
    from app.assistant.subconscious.projection import Projection
    from app.assistant.subconscious import belief_links, conversations, kg_links, work_links
    from app.assistant.utils.time_utils import utc_to_local
    now = now_utc or datetime.now(timezone.utc)
    open_concerns = [{**c, "_status": b} for b in ("active", "addressing") for c in register.get(b) or []]
    by_id = {c["concern_id"]: c for c in open_concerns}
    named = list(dict.fromkeys(cid for e in events for cid in e["concern_ids"] if cid in by_id))
    ordered = [by_id[cid] for cid in named] + [c for c in open_concerns if c["concern_id"] not in named]
    labels = {f"C{i}": c["concern_id"] for i, c in enumerate(ordered, 1)}
    titles = {c["concern_id"]: c.get("title") for c in open_concerns}
    closed = {c["concern_id"]: c.get("title") for b in ("resolved", "dormant") for c in register.get(b) or []}

    marked = [{**e, "mark": f"E{i}", "note": _gate_note(e, titles, closed)} for i, e in enumerate(events, 1)]
    p = Projection()
    for e in marked:
        p.fixed(e["mark"], "brain_event", str(e["id"]))
    for label, cid in labels.items():
        p.fixed(label, "concern", cid)
    work_labels = {e["source_ref"].split(":")[1]: p.label("W", "work", e["source_ref"].split(":")[1])
                   for e in marked if e["source"] == "work"}
    first = min(_utc(e["occurred_at"]) for e in events)
    rooms = list(dict.fromkeys(e["room_id"] for e in events if e["source"] == "chat"))
    threads = []
    for e in events:
        if e["source"] == "email":
            record = conversations._load_email(e["source_ref"])
            threads.append({"account_id": record.get("account_id"), "thread_id": record.get("thread_id")})
    entities = kg_links.find_entities([e["text"] for e in events])
    linked = work_links.linked_work(named, threads)
    similar = work_links.similar_work([e["text"] for e in events] + [by_id[cid].get("title") for cid in named],
                                      exclude=[w["work_id"] for w in linked])
    concern_views = [_concern_view(f"C{i}", c) for i, c in enumerate(ordered[:len(named)], 1)]
    active = work_links.active_work()
    reminders = work_links.live_reminders(now)
    lines = [dict(line) for line in (calendar if calendar is not None else _calendar_lines(now))]
    # ref -> label, read by shared/brain/concern.j2 and work_and_motion.j2 in place of an id.
    source_labels: Dict[str, str] = dict(work_labels)
    for c in concern_views:
        for w in c["attached_work"]:
            source_labels[w["work_id"]] = p.label("W", "work", w["work_id"])
    # Each work is shown in full once: under the concern it is attached to, where the brain reads that concern's work;
    # elsewhere it is one line naming that concern (2026-10-12; a work event's work was shown four times).
    under = {w["work_id"]: c["label"] for c in concern_views for w in c["attached_work"]}
    for w in [*linked, *similar, *active]:
        source_labels[w["work_id"]] = p.label("W", "work", w["work_id"])
        w["shown_under"] = under.get(w["work_id"])
    for r in reminders:
        source_labels[r["ref"]] = p.label("R", "reminder", r["ref"].split(":", 1)[1])
    for line in lines:
        if line["anchor"]:
            line["ref"] = p.label("K", "calendar_event", line["anchor"][len("calendar:"):])
    payload = {
        "now": utc_to_local(now.isoformat()).strftime("%a %Y-%m-%d %H:%M"),
        "events": conversations.render(conversations.build(marked, now_utc=now, work_labels=work_labels)),
        "room_history": [{"description": conversations._describe(r), "summaries": _room_history(r, first)}
                         for r in rooms],
        "concerns": concern_views,
        "other_concerns": [{"label": f"C{i}", "title": c.get("title"), "status": c["_status"],
                            "subject": c.get("subject") or "household", "done_when": c.get("done_when")}
                           for i, c in enumerate(ordered, 1) if i > len(named)],
        "linked_work": linked,
        "similar_work": similar,
        "active_work": active,
        "reminders": reminders,
        "calendar_lines": lines,
        "source_labels": source_labels,
        "entities": entities,
        "shared": kg_links.shared([x["node_id"] for x in entities]),
        "facts": kg_links.facts_by_meaning([e["text"] for e in events]),
        # What the household's beliefs say, closest in meaning to the events and the concerns they bear on
        # (subconscious/belief_links.py, 2026-10-09): context, never an instruction.
        "beliefs": belief_links.beliefs_by_meaning([e["text"] for e in events] +
                                                   [by_id[cid].get("title") for cid in named]),
    }
    return payload, {e["mark"]: e for e in marked}, labels, p.labels


# ── checking the answer ─────────────────────────────────────────────────────

def _refs(things: Any, call_labels: Dict[str, List[str]], where: str, *, attach: bool) -> List[str]:
    """Problems with one reference field: every label given in the call; `attach` names events and work only."""
    out = []
    names = [str(x).strip() for x in things or []]
    unknown = [x for x in names if x not in call_labels]
    if unknown:
        out.append(f"{where}: {unknown} are not labels given")
    if attach:
        wrong = [x for x in names if x in call_labels and call_labels[x][0] not in _ATTACHABLE]
        if wrong:
            out.append(f"{where}: attach names events (E) only, got {wrong}")
    return out


def _when(value: Any, now: datetime, where: str, out: List[str]) -> Optional[datetime]:
    """A local 'YYYY-MM-DD HH:MM' in the future as UTC, or a problem."""
    from app.assistant.subconscious.concern_view import future_local_utc
    try:
        return future_local_utc(value, now)
    except ValueError as exc:
        out.append(f"{where}: expires_at must be a future local YYYY-MM-DD HH:MM ({exc})")
        return None


_DECISIONS = ("changes_concern", "new_concern", "no_concern")


def _problems(data: Any, events: Dict[str, Dict[str, Any]], concern_labels: List[str],
              call_labels: Dict[str, List[str]], now: Optional[datetime] = None) -> List[str]:
    """The answer's contract (docs/design/concerns_as_state_2026-10-09.md, rule 3): every event decided once; an event
    that changes a concern names exactly one, which an update records, and an update names only events decided to
    change it; a new concern comes from the events decided as its own; references name labels given, and attach names
    events only; expiry times are future local times."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(data, dict) or not isinstance(data.get("event_decisions"), list):
        return ["no event_decisions list"]
    out: List[str] = []
    decided: Dict[str, Tuple[str, str]] = {}
    for d in data["event_decisions"]:
        ev = str((d or {}).get("event") or "").strip()
        if ev not in events:
            out.append(f"event {ev!r} is not one of the events given")
        elif ev in decided:
            out.append(f"{ev} decided more than once")
        else:
            decided[ev] = (d.get("decision"), str(d.get("concern") or "").strip())
    missing = [e for e in events if e not in decided]
    if missing:
        out.append(f"not decided: {missing}")

    new_labels = [str(n.get("label") or "").strip() for n in data.get("new_concerns") or []]
    for ev, (decision, concern) in decided.items():
        if decision not in _DECISIONS:
            out.append(f"{ev}: decision {decision!r} is not changes_concern, new_concern or no_concern")
        elif decision == "changes_concern" and concern not in concern_labels:
            out.append(f"{ev}: changes_concern names one open concern given, got {concern!r}")
        elif decision == "new_concern" and concern not in new_labels:
            out.append(f"{ev}: new_concern names one of the new concerns you raise, got {concern!r}")
        elif decision == "no_concern" and concern:
            out.append(f"{ev}: no_concern names no concern, got {concern!r}")
    changing = lambda label: {ev for ev, (d, c) in decided.items() if d == "changes_concern" and c == label}
    raising = lambda label: {ev for ev, (d, c) in decided.items() if d == "new_concern" and c == label}

    updated = set()
    for u in data.get("concern_updates") or []:
        label = str(u.get("concern") or "").strip()
        if label not in concern_labels:
            out.append(f"concern {label!r} is not one of the concerns given")
            continue
        if label in updated:
            out.append(f"{label} updated more than once")
        updated.add(label)
        if u.get("action") not in ("note", "resolve"):
            out.append(f"{label}: action {u.get('action')!r} is not note or resolve")
        if not str(u.get("note") or "").strip():
            out.append(f"{label}: note is empty")
        relied, attached = u.get("relies_on") or [], u.get("attach") or []
        out += _refs(relied, call_labels, f"{label} relies_on", attach=False)
        out += _refs(attached, call_labels, f"{label} attach", attach=True)
        mine = changing(label)
        if not mine:
            out.append(f"{label} is updated but no event is decided changes_concern for it: decide the events that "
                       f"change it, or leave it alone")
        stray = [x for x in [*relied, *attached] if x in events and x not in mine]
        if stray:
            out.append(f"{label}: {stray} are not decided to change it; an event changes one concern")
        if u.get("expires_at"):
            _when(u["expires_at"], now, label, out)
    for ev, (decision, concern) in decided.items():
        if decision == "changes_concern" and concern in concern_labels and concern not in updated:
            out.append(f"{ev} changes {concern}, but no update of {concern} records it")

    seen_new = set()
    for n in data.get("new_concerns") or []:
        label = str(n.get("label") or "").strip()
        if not label or label in seen_new:
            out.append(f"new concern label {label!r} is missing or repeated")
        seen_new.add(label)
        if not str(n.get("title") or "").strip() or not str(n.get("done_when") or "").strip():
            out.append(f"{label}: a new concern needs a title and done_when")
        _when(n.get("expires_at"), now, label, out)
        relied, attached = n.get("relies_on") or [], n.get("attach") or []
        out += _refs(relied, call_labels, f"{label} relies_on", attach=False)
        out += _refs(attached, call_labels, f"{label} attach", attach=True)
        mine = raising(label)
        if not mine:
            out.append(f"{label}: no event is decided new_concern for it")
        if not any(x in mine for x in attached):
            out.append(f"{label}: attach names none of the events it comes from")
        stray = [x for x in attached if x in events and x not in mine]
        if stray:
            out.append(f"{label}: attaches {stray}, which are not decided as its own")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="brain", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::brain", actor_id="subconscious::brain")


def _evidence(e: Dict[str, Any]) -> Dict[str, Any]:
    if e["source"] == "email":
        return {"kind": "pod", "ref": e["source_ref"], "snippet": e["text"].split("\n", 1)[0]}
    if e["source"] == "work":
        return {"kind": "work", "ref": e["source_ref"], "snippet": e["text"]}
    if e["source"] == "camera":
        return {"kind": "pod", "ref": e["source_ref"], "snippet": e["text"]}
    if e["source"] == "presence":
        return {"kind": "presence", "ref": e["source_ref"], "snippet": e["text"]}
    if e["source"] == "time":
        return {"kind": "time", "ref": e["source_ref"], "snippet": e["text"]}
    if e["source"] == "calendar":
        # The calendar event itself (calendar:<id>), as a concern's calendar evidence is kept.
        return {"kind": "calendar_event", "ref": e["source_ref"].split("|")[0], "snippet": e["text"]}
    if e["source"] == "ticket":
        return {"kind": "ticket_reply", "ref": e["source_ref"], "snippet": e["text"]}
    if e["source"] == "noticer":
        return {"kind": "noticer_issue", "ref": e["source_ref"], "snippet": e["text"]}
    return {"kind": "chat_msg", "ref": e["source_ref"], "snippet": e["text"]}


# ── one matter ──────────────────────────────────────────────────────────────

def process_matter(events: List[Dict[str, Any]], *, call: Optional[Callable[[Dict[str, Any]], Any]] = None,
                   judge=None, register_connect=None, calendar: Optional[str] = None) -> Dict[str, Any]:
    """Decide and apply one matter. Returns {decisions, admitted, concern_ids}; raises when the
    brain's answer is still invalid after one correction or applying it fails."""
    from app.assistant.subconscious import concern_door, persist
    from app.assistant.subconscious.concern_store import load_register
    now = datetime.now(timezone.utc)
    register = load_register(connect=register_connect)
    payload, by_label, labels, call_labels = build_payload(events, register, now, calendar)
    call = call or _agent_call
    data = call(payload)
    problems = _problems(data, by_label, list(labels), call_labels, now)
    if problems:
        from app.assistant.subconscious.brain_trace import step
        logger.warning("[brain] invalid answer, one correction: %s", problems)
        step("brain", "answer refused, one correction", subject=[e["id"] for e in events], problems=problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data, by_label, list(labels), call_labels, now)
        if problems:
            raise ValueError(f"brain answer still invalid after one correction: {problems}")

    # The answer's labels back to objects (docs/design/objects_and_projections_2026-10-08.md): what an update or a new
    # concern rests on and what it attaches are kept as relations (`links`, written with the matter by _record);
    # what it attaches also becomes the concern's evidence (an event) or attached work.
    from app.assistant.subconscious.concern_view import future_local_utc
    ids = lambda names: [tuple(call_labels[x]) for x in names or []]
    attached_events = lambda names: [by_label[x] for x in names or [] if x in by_label]
    updates = [{"concern_id": labels[u["concern"]], "action": u["action"], "note": u["note"],
                "evidence": [_evidence(e) for e in attached_events(u.get("attach"))],
                "expires_at": future_local_utc(u["expires_at"], now).isoformat() if u.get("expires_at") else None}
               for u in data.get("concern_updates") or []]
    candidates = []
    for n in data.get("new_concerns") or []:
        source_events = sorted(attached_events(n.get("attach")), key=lambda e: (e["occurred_at"], e["id"]))
        candidates.append({
            "label": n["label"], "title": n["title"], "subject": n.get("subject"), "kind": n.get("kind"),
            "severity": n.get("severity"), "horizon": n.get("horizon"), "done_when": n["done_when"],
            "expires_at": future_local_utc(n["expires_at"], now).isoformat(), "source": _source(source_events[0]),
            "notes": n.get("notes") or "", "domain_tags": [], "addressable_by": [],
            "first_observed": now.isoformat(), "evidence": [_evidence(e) for e in source_events]})
    plan = concern_door.plan_admission(candidates, load_register(connect=register_connect), judge=judge)
    admitted = persist.apply_brain_matter(updates, plan, connect=register_connect)
    # The concern each event changed or raised, as its id (a new concern's: where the door landed it).
    concern_of = lambda d: (labels.get(d.get("concern") or "") if d["decision"] == "changes_concern"
                            else (admitted or {}).get(d.get("concern") or "") if d["decision"] == "new_concern" else None)
    decisions = {
        "events": {by_label[d["event"]]["source_ref"]: {"decision": d["decision"], "concern_id": concern_of(d),
                                                         "reason": d["reason"]}
                   for d in data["event_decisions"]},
        "concern_updates": [{"concern_id": u["concern_id"], "action": u["action"], "note": u["note"],
                             "expires_at": u["expires_at"]} for u in updates],
        # door: what the concern door made of it (create | merge_open | suppress_closed | merge_candidate)
        "new_concerns": [{"label": c["label"], "title": c["title"], "source": c["source"],
                          "expires_at": c["expires_at"], "door": s["action"], "door_reason": s["reason"]}
                         for c, s in zip(candidates, plan)],
    }
    # Each update is the object ("brain_update", "<matter>:<concern id>"), each new concern ("brain_new_concern",
    # "<matter>:<n>"), n its place in the answer; the matter's id is known once _record writes it.
    links = [(("brain_update", u["concern_id"]), rel, ids(d.get(rel)))
             for u, d in zip(updates, data.get("concern_updates") or []) for rel in ("relies_on", "attach")]
    links += [(("brain_new_concern", str(i)), rel, ids(n.get(rel)))
              for i, n in enumerate(data.get("new_concerns") or [], 1) for rel in ("relies_on", "attach")]
    return {"decisions": decisions, "admitted": admitted, "links": links,
            "concern_ids": sorted({cid for e in events for cid in e["concern_ids"]})}


def _record(connect, events, status, *, concern_ids=(), decisions=None, admitted=None, error=None,
            links=()) -> None:
    """The matter, and its links (process_matter `links`) as relations from its updates and new concerns, in one
    transaction."""
    from app.assistant.subconscious import relations
    now = datetime.now(timezone.utc).isoformat()
    relations.ensure_schema(connect)
    with connect(True) as c:
        cur = c.execute("INSERT INTO brain_matters (created_at, event_ids, concern_ids, status, decisions, admitted, "
                        "error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (now, json.dumps([e["id"] for e in events]), json.dumps(list(concern_ids)), status,
                         json.dumps(decisions, ensure_ascii=False) if decisions is not None else None,
                         json.dumps(admitted) if admitted is not None else None, error))
        for (kind, key), rel, to in links:
            relations.replace(c, (kind, f"{cur.lastrowid}:{key}"), rel, to, at=now, by=f"brain matter {cur.lastrowid}")
        if status == "applied":
            c.executemany("UPDATE brain_events SET consumed_at=? WHERE id=?", [(now, e["id"]) for e in events])


def run_brain_step(*, connect=None, register_connect=None, call=None, judge=None) -> Dict[str, Any]:
    """Every pending matter, one at a time. Free when nothing is pending. The keyword arguments are
    test seams (inbox store, register store, the brain agent, the concern_door judge)."""
    connect = connect or _connect
    events = pending_events(connect)
    if not events:
        return {"matters": 0, "applied": 0, "failed": 0}
    applied = failed = 0
    matters = group_matters(events)
    calendar = _calendar_lines(datetime.now(timezone.utc))
    for matter in matters:
        from app.assistant.subconscious.brain_trace import step, trace
        try:
            with trace(stage="brain", event_ids=[e["id"] for e in matter]):
                result = process_matter(matter, call=call, judge=judge, register_connect=register_connect,
                                        calendar=calendar)
        except Exception as exc:
            logger.error("[brain] matter of %d event(s) failed; its events stay in the inbox, not retried: %s",
                         len(matter), exc, exc_info=True)
            step("brain", "matter failed: its events stay in the inbox", subject=[e["id"] for e in matter],
                 error=f"{type(exc).__name__}: {exc}")
            _record(connect, matter, "failed", error=f"{type(exc).__name__}: {exc}")
            failed += 1
            continue
        step("brain", "matter applied", subject=[e["id"] for e in matter], decisions=result["decisions"],
             admitted=result["admitted"])
        _record(connect, matter, "applied", concern_ids=result["concern_ids"], decisions=result["decisions"],
                admitted=result["admitted"], links=result["links"])
        applied += 1
    logger.info("[brain] matters=%d applied=%d failed=%d", len(matters), applied, failed)
    return {"matters": len(matters), "applied": applied, "failed": failed}
