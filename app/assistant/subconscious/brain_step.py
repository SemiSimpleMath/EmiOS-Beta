"""The brain step: one matter at a time, from the events the gate passed on to the concerns (2026-09-30).

The gate routes every inbox event against the open concerns. What it passes on (bears on concerns,
a new matter, or could not be routed) comes here, and the brain decides what it changes. Before
this, the noticer read all of it once a day, in one call, beside twenty other context sections.

A matter is a group of events read together:
- events the gate routed to the same concerns;
- new-matter or unrouted events from the same conversation: a chat room, or a Gmail thread.
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
- the people, places and things the events name, from the knowledge graph, and what joins them
  (subconscious/kg_links.py).

It answers with notes and resolutions on concerns, new concerns (with done-when, and the owner's
words verbatim when the owner asked for it), and a decision on every event. Code validates the
answer (one correction round), admits new concerns through the concern door, applies the rest
under the register lock, marks the events consumed and records the matter in `brain_matters`.
A matter that still fails is recorded failed and not retried automatically: its events stay in
the inbox, visible, instead of being dropped or re-sent every five minutes.
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
        else:
            key = ("room", e.get("room_id"))
        groups.setdefault(key, []).append(e)
    return sorted(groups.values(), key=lambda g: (g[0]["occurred_at"], g[0]["id"]))


# ── what the brain reads ────────────────────────────────────────────────────

def _gate_note(e: Dict[str, Any], titles: Dict[str, str]) -> str:
    if e.get("gate_status") == "failed":
        return "gate: could not route this one; read it against every concern and as a new matter"
    if e.get("route") == "new_matter":
        return "gate: new matter, no open concern covers it"
    return "gate: bears on " + "; ".join(titles.get(cid, f"{cid} (no longer open)") for cid in e["concern_ids"])


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
        return f"{when} in {row.room_id}, {speaker}: {row.message or ''}"
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


def _calendar(now_utc: datetime) -> str:
    """The calendar from now through CALENDAR_DAYS, each event line with its calendar:<id> anchor."""
    from app.assistant.subconscious.context_builder import _fetch_calendar_text
    from app.assistant.utils.time_utils import utc_to_local
    start = utc_to_local(now_utc.isoformat())
    return _fetch_calendar_text(start, start + timedelta(days=CALENDAR_DAYS), label=f"next {CALENDAR_DAYS} days")


def _concern_view(label: str, c: Dict[str, Any]) -> Dict[str, Any]:
    from app.assistant.subconscious import concern_feedback
    return {
        "label": label, "title": c.get("title"), "status": c["_status"], "subject": c.get("subject") or "household",
        "kind": c.get("kind"), "severity": c.get("severity"), "horizon": c.get("horizon"),
        "owner_words": (c.get("owner_request") or {}).get("words"), "done_when": c.get("done_when"),
        "first_observed": c.get("first_observed"), "notes": c.get("notes") or "",
        "evidence": [{"kind": e.get("kind"), "ref": e.get("ref"), "snippet": _evidence_text(e)}
                     for e in c.get("evidence") or []],
        "attached_work": [{"work_id": w["work_id"], "title": w.get("title"), "objective": w.get("objective"),
                           "attached_at": w.get("attached_at"), "status": w.get("status"),
                           "judgments": [{**j, "replies": [concern_feedback._reply_line(r) for r in j.get("replies") or []]}
                                         for j in w.get("judgments") or []],
                           "ended": w.get("ended")}
                          for w in (c.get("attached_work") or {}).values()],
        "journal": c.get("reinforcement_notes") or "",
    }


def build_payload(events: List[Dict[str, Any]], register: Dict[str, Any],
                  now_utc: Optional[datetime] = None,
                  calendar: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Dict], Dict[str, str]]:
    """The brain's input for one matter. Returns (payload, event by label, concern_id by label).
    `calendar` is the run's calendar text, read once per run; omitted, it is read here."""
    from app.assistant.subconscious import conversations, kg_links, work_links
    from app.assistant.utils.time_utils import utc_to_local
    now = now_utc or datetime.now(timezone.utc)
    open_concerns = [{**c, "_status": b} for b in ("active", "addressing") for c in register.get(b) or []]
    by_id = {c["concern_id"]: c for c in open_concerns}
    named = list(dict.fromkeys(cid for e in events for cid in e["concern_ids"] if cid in by_id))
    ordered = [by_id[cid] for cid in named] + [c for c in open_concerns if c["concern_id"] not in named]
    labels = {f"C{i}": c["concern_id"] for i, c in enumerate(ordered, 1)}
    titles = {c["concern_id"]: c.get("title") for c in open_concerns}

    marked = [{**e, "mark": f"E{i}", "note": _gate_note(e, titles)} for i, e in enumerate(events, 1)]
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
    payload = {
        "now": utc_to_local(now.isoformat()).strftime("%a %Y-%m-%d %H:%M"),
        "events": conversations.render(conversations.build(marked, now_utc=now)),
        "room_history": [{"room": r, "summaries": _room_history(r, first)} for r in rooms],
        "concerns": [_concern_view(f"C{i}", c) for i, c in enumerate(ordered[:len(named)], 1)],
        "other_concerns": [{"label": f"C{i}", "title": c.get("title"), "status": c["_status"],
                            "subject": c.get("subject") or "household", "done_when": c.get("done_when")}
                           for i, c in enumerate(ordered, 1) if i > len(named)],
        "linked_work": linked,
        "similar_work": similar,
        "active_work": work_links.active_work(),
        "reminders": work_links.live_reminders(now),
        "calendar": calendar if calendar is not None else _calendar(now),
        "entities": entities,
        "shared": kg_links.shared([x["node_id"] for x in entities]),
    }
    return payload, {e["mark"]: e for e in marked}, labels


# ── checking the answer ─────────────────────────────────────────────────────

def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


def _problems(data: Any, events: Dict[str, Dict[str, Any]], concern_labels: List[str]) -> List[str]:
    if not isinstance(data, dict) or not isinstance(data.get("event_decisions"), list):
        return ["no event_decisions list"]
    out: List[str] = []
    decided: Dict[str, str] = {}
    for d in data["event_decisions"]:
        ev = str((d or {}).get("event") or "").strip()
        if ev not in events:
            out.append(f"event {ev!r} is not one of the events given")
        elif ev in decided:
            out.append(f"{ev} decided more than once")
        else:
            decided[ev] = d.get("decision")
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
        refs = u.get("events") or []
        if not refs or any(r not in events for r in refs):
            out.append(f"{label}: events must be labels of events given, got {refs}")
        if not str(u.get("note") or "").strip():
            out.append(f"{label}: note is empty")
        cited_by_update.update(refs)

    seen_new = set()
    for n in data.get("new_concerns") or []:
        label = str(n.get("label") or "").strip()
        if not label or label in seen_new:
            out.append(f"new concern label {label!r} is missing or repeated")
        seen_new.add(label)
        if not str(n.get("title") or "").strip() or not str(n.get("done_when") or "").strip():
            out.append(f"{label}: a new concern needs a title and done_when")
        refs = n.get("events") or []
        if not refs or any(r not in events for r in refs):
            out.append(f"{label}: events must be labels of events given, got {refs}")
        words = _squash(n.get("owner_words") or "")
        if words and not any(words in _squash(events[r]["text"]) for r in refs if r in events):
            out.append(f"{label}: owner_words must be copied exactly from one of its events")
        cited_by_new.update(refs)

    for ev, decision in decided.items():
        if decision == "used" and ev not in cited_by_update:
            out.append(f"{ev} is decided used but no concern update cites it")
        if decision == "tracked_as_new" and ev not in cited_by_new:
            out.append(f"{ev} is decided tracked_as_new but no new concern cites it")
        if decision == "not_worth_tracking" and (ev in cited_by_update or ev in cited_by_new):
            out.append(f"{ev} is decided not_worth_tracking but an update or new concern cites it")
        if decision not in ("used", "tracked_as_new", "not_worth_tracking"):
            out.append(f"{ev}: decision {decision!r} is not used, tracked_as_new or not_worth_tracking")
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
    payload, by_label, labels = build_payload(events, register, now, calendar)
    call = call or _agent_call
    data = call(payload)
    problems = _problems(data, by_label, list(labels))
    if problems:
        logger.warning("[brain] invalid answer, one correction: %s", problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data, by_label, list(labels))
        if problems:
            raise ValueError(f"brain answer still invalid after one correction: {problems}")

    updates = [{"concern_id": labels[u["concern"]], "action": u["action"], "note": u["note"],
                "evidence": [_evidence(by_label[r]) for r in u["events"]]}
               for u in data.get("concern_updates") or []]
    candidates = []
    for n in data.get("new_concerns") or []:
        source_events = [by_label[r] for r in n["events"]]
        candidate = {"label": n["label"], "title": n["title"], "subject": n.get("subject"), "kind": n.get("kind"),
                     "severity": n.get("severity"), "horizon": n.get("horizon"), "done_when": n["done_when"],
                     "notes": n.get("notes") or "", "domain_tags": [], "addressable_by": [],
                     "first_observed": now.isoformat(), "evidence": [_evidence(e) for e in source_events]}
        words = _squash(n.get("owner_words") or "")
        if words:
            said = next(e for e in source_events if words in _squash(e["text"]))
            candidate["owner_request"] = {"words": n["owner_words"], "at": said["occurred_at"],
                                          "ref": said["source_ref"]}
        candidates.append(candidate)
    plan = concern_door.plan_admission(candidates, load_register(connect=register_connect), judge=judge)
    admitted = persist.apply_brain_matter(updates, plan, connect=register_connect)
    decisions = {
        "events": {by_label[d["event"]]["source_ref"]: {"decision": d["decision"], "reason": d["reason"]}
                   for d in data["event_decisions"]},
        "concern_updates": [{"concern_id": u["concern_id"], "action": u["action"], "note": u["note"]} for u in updates],
        "new_concerns": [{"label": c["label"], "title": c["title"], "owner_request": c.get("owner_request")}
                         for c in candidates],
    }
    return {"decisions": decisions, "admitted": admitted,
            "concern_ids": sorted({cid for e in events for cid in e["concern_ids"]})}


def _record(connect, events, status, *, concern_ids=(), decisions=None, admitted=None, error=None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect(True) as c:
        c.execute("INSERT INTO brain_matters (created_at, event_ids, concern_ids, status, decisions, admitted, error) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (now, json.dumps([e["id"] for e in events]), json.dumps(list(concern_ids)), status,
                   json.dumps(decisions, ensure_ascii=False) if decisions is not None else None,
                   json.dumps(admitted) if admitted is not None else None, error))
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
    calendar = _calendar(datetime.now(timezone.utc))
    for matter in matters:
        from app.assistant.subconscious.brain_trace import trace
        try:
            with trace(stage="brain", event_ids=[e["id"] for e in matter]):
                result = process_matter(matter, call=call, judge=judge, register_connect=register_connect,
                                        calendar=calendar)
        except Exception as exc:
            logger.error("[brain] matter of %d event(s) failed; its events stay in the inbox, not retried: %s",
                         len(matter), exc, exc_info=True)
            _record(connect, matter, "failed", error=f"{type(exc).__name__}: {exc}")
            failed += 1
            continue
        _record(connect, matter, "applied", concern_ids=result["concern_ids"], decisions=result["decisions"],
                admitted=result["admitted"])
        applied += 1
    logger.info("[brain] matters=%d applied=%d failed=%d", len(matters), applied, failed)
    return {"matters": len(matters), "applied": applied, "failed": failed}
