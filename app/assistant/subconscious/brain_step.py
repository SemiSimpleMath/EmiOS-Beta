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
- the full record of every concern the events bear on, evidence shown as its text, not its id,
  with the work attached to it and every judgment of that work;
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

It answers with notes and resolutions on concerns, new concerns (with done-when, and the owner's
words verbatim when the owner asked for it), and a decision on every event. Code validates the
answer (one correction round), admits new concerns through the concern door, applies the rest
under the register lock, marks the events consumed and records the matter in `brain_matters`.
A matter that still fails is recorded failed and not retried automatically: its events stay in
the inbox, visible, instead of being dropped or re-sent every five minutes.

What the assistant did reaches the brain as `work` events, routed by the gate like chat and email
(docs/design/what_was_done_reaches_the_brain_2026-10-03.md). When the brain cites a work report on a
concern, or raises a new concern from one, the work is attached to that concern if it was not already
(`persist.attach_seen_work`), so the work's later judgments and its ending reach the concern by code.
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
    from app.assistant.subconscious import concern_brief, persist
    return {
        "label": label, "title": c.get("title"), "status": c["_status"], "subject": c.get("subject") or "household",
        "kind": c.get("kind"), "severity": c.get("severity"), "horizon": c.get("horizon"),
        "owner_words": (c.get("owner_request") or {}).get("words"), "done_when": c.get("done_when"),
        "first_observed": c.get("first_observed"), "notes": c.get("notes") or "",
        "evidence": [{"kind": e.get("kind"), "ref": e.get("ref"), "snippet": _evidence_text(e)}
                     for e in c.get("evidence") or []],
        "attached_work": concern_brief.attempts(c),
        "history": persist.history_for_prompt(c),
    }


# A concern's evidence as the object it points to (kind, id), and the label prefix of each kind.
_EVIDENCE_PREFIX = {"work": "W", "message": "M", "pod": "P", "calendar_event": "K", "source": "S"}
# What `attach` may name: an event (its message, email, frame or work report) or a work object.
_ATTACHABLE = ("brain_event", "work")


def _evidence_object(e: Dict[str, Any]) -> Tuple[str, str]:
    ref, kind = str(e.get("ref") or ""), str(e.get("kind") or "")
    if ref.startswith("work:"):
        return "work", ref.split(":")[1]
    if ref.startswith("message:"):
        return "message", ref[len("message:"):]
    if ref.startswith("datapod:"):
        return "pod", ref
    if kind == "calendar_event":
        return "calendar_event", ref[len("calendar:"):] if ref.startswith("calendar:") else ref
    return "source", f"{kind}:{ref}"


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
        for ev in c["evidence"]:
            if ev.get("ref"):
                kind, obj_id = _evidence_object(ev)
                source_labels[ev["ref"]] = p.label(_EVIDENCE_PREFIX[kind], kind, obj_id)
        for w in c["attached_work"]:
            source_labels[w["work_id"]] = p.label("W", "work", w["work_id"])
    for w in [*linked, *similar, *active]:
        source_labels[w["work_id"]] = p.label("W", "work", w["work_id"])
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

def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


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
            out.append(f"{where}: attach names events (E) and work (W) only, got {wrong}")
    return out


def _in_prose(call_labels: Dict[str, Any], new_labels: List[str], *texts: Any) -> List[str]:
    from app.assistant.subconscious.projection import labels_in
    return labels_in(" ".join(str(t or "") for t in texts), [*call_labels, *new_labels])


def _problems(data: Any, events: Dict[str, Dict[str, Any]], concern_labels: List[str],
              call_labels: Dict[str, List[str]]) -> List[str]:
    if not isinstance(data, dict) or not isinstance(data.get("event_decisions"), list):
        return ["no event_decisions list"]
    out: List[str] = []
    decided: Dict[str, str] = {}
    new_labels = [str(n.get("label") or "").strip() for n in data.get("new_concerns") or []]
    prose = "write it in words and put the reference in relies_on or attach"
    for d in data["event_decisions"]:
        ev = str((d or {}).get("event") or "").strip()
        if ev not in events:
            out.append(f"event {ev!r} is not one of the events given")
        elif ev in decided:
            out.append(f"{ev} decided more than once")
        else:
            decided[ev] = d.get("decision")
        written = _in_prose(call_labels, new_labels, d.get("reason"))
        if written:
            out.append(f"{ev}: {written} written into its reason; write it in words")
    missing = [e for e in events if e not in decided]
    if missing:
        out.append(f"not decided: {missing}")

    cited_by_update, cited_by_new, updated = set(), set(), set()
    for u in data.get("concern_updates") or []:
        label = str(u.get("concern") or "").strip()
        if label not in concern_labels:
            out.append(f"concern {label!r} is not one of the concerns given")
        elif label in updated:
            out.append(f"{label} updated more than once")
        updated.add(label)
        if u.get("action") not in ("note", "resolve"):
            out.append(f"{label}: action {u.get('action')!r} is not note or resolve")
        relied, attached = u.get("relies_on") or [], u.get("attach") or []
        if not relied and not attached:
            out.append(f"{label}: relies_on and attach name nothing; name the events it rests on")
        out += _refs(relied, call_labels, f"{label} relies_on", attach=False)
        out += _refs(attached, call_labels, f"{label} attach", attach=True)
        if not str(u.get("note") or "").strip():
            out.append(f"{label}: note is empty")
        written = _in_prose(call_labels, new_labels, u.get("note"))
        if written:
            out.append(f"{label}: {written} written into its note; {prose}")
        cited_by_update.update(x for x in [*relied, *attached] if x in events)

    seen_new = set()
    for n in data.get("new_concerns") or []:
        label = str(n.get("label") or "").strip()
        if not label or label in seen_new:
            out.append(f"new concern label {label!r} is missing or repeated")
        seen_new.add(label)
        if not str(n.get("title") or "").strip() or not str(n.get("done_when") or "").strip():
            out.append(f"{label}: a new concern needs a title and done_when")
        relied, attached = n.get("relies_on") or [], n.get("attach") or []
        if not any(x in events for x in attached):
            out.append(f"{label}: attach names no event; attach the events it comes from")
        out += _refs(relied, call_labels, f"{label} relies_on", attach=False)
        out += _refs(attached, call_labels, f"{label} attach", attach=True)
        refs = [x for x in [*relied, *attached] if x in events]
        words = _squash(n.get("owner_words") or "")
        if words and not any(words in _squash(events[r]["text"]) for r in refs):
            out.append(f"{label}: owner_words must be copied exactly from one of its events")
        written = _in_prose(call_labels, new_labels, n.get("title"), n.get("done_when"), n.get("notes"))
        if written:
            out.append(f"{label}: {written} written into its title, done_when or notes; {prose}")
        cited_by_new.update(refs)

    for ev, decision in decided.items():
        if decision == "used" and ev not in cited_by_update:
            out.append(f"{ev} is decided used but no concern update names it in relies_on or attach: name it on the "
                       f"concern it changes, or decide needs_nothing if it changes none")
        if decision == "tracked_as_new" and ev not in cited_by_new:
            out.append(f"{ev} is decided tracked_as_new but no new concern names it")
        if decision == "needs_nothing" and (ev in cited_by_update or ev in cited_by_new):
            out.append(f"{ev} is decided needs_nothing but an update or new concern names it")
        if decision not in ("used", "tracked_as_new", "needs_nothing"):
            out.append(f"{ev}: decision {decision!r} is not used, tracked_as_new or needs_nothing")
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
    return {"kind": "chat_msg", "ref": e["source_ref"], "snippet": e["text"]}


# ── one matter ──────────────────────────────────────────────────────────────

def _work_record(work_id: str) -> Dict[str, Any]:
    """A work object as a concern records it: title, objective, status now, and its ending when it ended."""
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    wo = get_dayflow_work_store().load(work_id)
    constraints = wo.constraints or {}
    record = {"title": wo.title, "objective": constraints.get("objective") or wo.title,
              "success_criteria": constraints.get("success_criteria") or "",
              "status": wo.status if wo.status in ("done", "abandoned") else "active"}
    if wo.status in ("done", "abandoned"):
        goal = wo.nodes.get(wo.goal_node_id)
        record["ended"] = {"outcome": wo.status, "at": str(wo.updated_at),
                           "reason": ((goal.payload.get("terminal") or {}).get("reason") if goal else "") or ""}
    return record


def _attach_work(data: Dict[str, Any], by_label: Dict[str, Dict], labels: Dict[str, str],
                 call_labels: Dict[str, List[str]], admitted: Dict[str, Optional[str]], work_record,
                 register_connect) -> List[tuple]:
    """Attach the work the brain named in `attach` (a work report's work, or a work object directly) to that concern,
    unless the concern holds it already. Only `attach` attaches: until 2026-10-08 every work report cited on a
    concern was attached, and a report cited on the pickup concern to say it did not apply attached the Nest work
    to it. Returns (concern_id, work_id)."""
    from app.assistant.subconscious import persist
    named = [(labels[u["concern"]], x) for u in data.get("concern_updates") or [] for x in u.get("attach") or []]
    named += [(admitted.get(n["label"]), x) for n in data.get("new_concerns") or [] for x in n.get("attach") or []]
    attached = []
    for concern_id, label in named:
        kind, obj_id = call_labels[label]
        if kind == "brain_event":
            event = by_label[label]
            if event["source"] != "work":
                continue
            work_id, receipt = event["source_ref"].split(":")[1], f"brain:{event['id']}:{concern_id}"
        else:
            work_id = obj_id
            receipt = f"brain:{'-'.join(str(e['id']) for e in by_label.values())}:{concern_id}:{work_id}"
        if not concern_id or concern_id in persist.concerns_holding_work(work_id, connect=register_connect):
            continue
        result = persist.attach_seen_work(concern_id, work_id=work_id, work=work_record(work_id),
                                          receipt_id=receipt, connect=register_connect)
        if result == "attached":
            attached.append((concern_id, work_id))
    return attached


def process_matter(events: List[Dict[str, Any]], *, call: Optional[Callable[[Dict[str, Any]], Any]] = None,
                   judge=None, register_connect=None, calendar: Optional[str] = None,
                   work_record=None) -> Dict[str, Any]:
    """Decide and apply one matter. Returns {decisions, admitted, concern_ids}; raises when the
    brain's answer is still invalid after one correction or applying it fails."""
    from app.assistant.subconscious import concern_door, persist
    from app.assistant.subconscious.concern_store import load_register
    now = datetime.now(timezone.utc)
    register = load_register(connect=register_connect)
    payload, by_label, labels, call_labels = build_payload(events, register, now, calendar)
    call = call or _agent_call
    data = call(payload)
    problems = _problems(data, by_label, list(labels), call_labels)
    if problems:
        from app.assistant.subconscious.brain_trace import step
        logger.warning("[brain] invalid answer, one correction: %s", problems)
        step("brain", "answer refused, one correction", subject=[e["id"] for e in events], problems=problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data, by_label, list(labels), call_labels)
        if problems:
            raise ValueError(f"brain answer still invalid after one correction: {problems}")

    # The answer's labels back to objects (docs/design/objects_and_projections_2026-10-08.md): what an update or a new
    # concern rests on and what it attaches are kept as relations (`links`, written with the matter by _record);
    # what it attaches also becomes the concern's evidence (an event) or attached work.
    ids = lambda names: [tuple(call_labels[x]) for x in names or []]
    attached_events = lambda names: [by_label[x] for x in names or [] if x in by_label]
    updates = [{"concern_id": labels[u["concern"]], "action": u["action"], "note": u["note"],
                "evidence": [_evidence(e) for e in attached_events(u.get("attach"))]}
               for u in data.get("concern_updates") or []]
    candidates = []
    for n in data.get("new_concerns") or []:
        source_events = attached_events(n.get("attach"))
        relied_events = [*source_events, *attached_events(n.get("relies_on"))]
        candidate = {"label": n["label"], "title": n["title"], "subject": n.get("subject"), "kind": n.get("kind"),
                     "severity": n.get("severity"), "horizon": n.get("horizon"), "done_when": n["done_when"],
                     "notes": n.get("notes") or "", "domain_tags": [], "addressable_by": [],
                     "first_observed": now.isoformat(), "evidence": [_evidence(e) for e in source_events]}
        words = _squash(n.get("owner_words") or "")
        if words:
            said = next(e for e in relied_events if words in _squash(e["text"]))
            candidate["owner_request"] = {"words": n["owner_words"], "at": said["occurred_at"],
                                          "ref": said["source_ref"]}
        candidates.append(candidate)
    plan = concern_door.plan_admission(candidates, load_register(connect=register_connect), judge=judge)
    admitted = persist.apply_brain_matter(updates, plan, connect=register_connect)
    attached = _attach_work(data, by_label, labels, call_labels, admitted or {}, work_record or _work_record,
                            register_connect)
    decisions = {
        "events": {by_label[d["event"]]["source_ref"]: {"decision": d["decision"], "reason": d["reason"]}
                   for d in data["event_decisions"]},
        "concern_updates": [{"concern_id": u["concern_id"], "action": u["action"], "note": u["note"]}
                            for u in updates],
        # door: what the concern door made of it (create | merge_open | suppress_closed | merge_candidate)
        "new_concerns": [{"label": c["label"], "title": c["title"], "owner_request": c.get("owner_request"),
                          "door": s["action"], "door_reason": s["reason"]}
                         for c, s in zip(candidates, plan)],
    }
    if attached:
        decisions["attached_work"] = [{"concern_id": c, "work_id": w} for c, w in attached]
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


def run_brain_step(*, connect=None, register_connect=None, call=None, judge=None, work_record=None) -> Dict[str, Any]:
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
                                        calendar=calendar, work_record=work_record)
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
