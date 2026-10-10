"""Work from the brain's list (docs/design/brain_delegates_work_2026-10-04.md, section 3, "The list-item intake node").

The brain decides what work exists and keeps it as entries on its list (subconscious/brain_list.py), decided by one
decider, the day's tasks: today's calendar, every concern's entries (each concern's planner proposes them), and the
day's own. This turns the list's changes into work, by code, in each
planning tick (control_nodes/brain_list_intake_node.py, between the tick's prep node and the architect). It
judges nothing; the entry already says what is needed:

- an entry decided needed with no work yet: a work object shell is built from it (objective = the outcome and when,
  success = the outcome, the brief = the entry with the concern or belief behind it in full and the sources word for
  word; the entry id, and the concern or belief, in its constraints) and the architect lays its graph;
- an entry changed while its work is active (any change the list records: what, the decision, the assistant's part,
  when, what it covers): the objective and brief are revised and a re-plan instruction is left on the work object in
  the same commit, licensed as the brain's directive (until 2026-10-05 only a changed what or when counted, and the
  re-plan request lived on the tick's blackboard);
- an entry cancelled, or decided not needed, while its work is active: the work ends, abandoned with the brain's
  reason, and the concerns and beliefs it served are told. The brain decides what work exists, so its end is binding
  (owner, 2026-10-10: "make it binding"). Until then it was an end instruction the architect could refuse: on
  2026-10-10 it kept five pieces of work whose concerns the day pass had withdrawn ("the cancellation reason does
  not override this pending work"), the day's tasks then took that work as covering the concerns raised in their
  place, and the work's outcomes would have reached the closed concerns;
- an entry still decided needed after its work ended: new work is made from it. Until 2026-10-08 such a change was
  recorded only ("a done entry runs again only by a new decision, never by a rewording"); on 10-08 the architect
  ended the evening's phone-charge reminder as a duplicate of the day before's, the brain re-decided it needed with
  the reason, and nothing could make the work again (owner: "you have a gate keeper not letting a decision
  through"). The decider reads each entry with its work's ending, so a needed entry over ended work is its decision.

Every entry is the brain's to delegate, except those made before each kind's work came from the list: a day's
entries (calendar and the day's own) before CALENDAR_FROM_DAY, a concern's before CONCERN_SINCE. Their work was the
steward's, which was retired on 2026-10-10 (owner: "retire the old steward"); until then lanes were switched in
configs/brain_lanes.json (off, dry_run, live) and a lane not live reached the steward instead. Every change acted on
is recorded in `brain_list_deliveries` (emi.db), so none is acted on twice (its `mode` is `live`; rows marked
`dry_run` are the lanes' trial runs of 2026-10-05).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA = """CREATE TABLE IF NOT EXISTS brain_list_deliveries (
    change_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    work_id TEXT,
    PRIMARY KEY (change_id, mode))"""
# action: create | revise | end | none (work_ended_not_reopened in rows before 2026-10-08). mode: live (dry_run in the
# lanes' trial rows of 2026-10-05).

CALENDAR_FROM_DAY = "2026-10-06"               # the first day whose calendar and own entries became work from the list
CONCERN_SINCE = "2026-10-05T16:31:58+00:00"    # concern entries made from then on are the brain's to delegate

_ACTIVE = ("active", "blocked")


def _connect(write: bool):
    from app.assistant.subconscious.db import connect
    return connect(write)


def ensure_schema(connect=None) -> None:
    with (connect or _connect)(True) as c:
        c.execute(SCHEMA)


def _the_brains(entry: Dict[str, Any]) -> bool:
    """True when the entry's work is the brain's to make: made after its kind's work came from the list."""
    from app.assistant.utils.time_utils import parse_iso_utc
    if entry["kind"] in ("calendar", "day"):
        return entry["day"] >= CALENDAR_FROM_DAY
    return parse_iso_utc(entry["created_at"]) >= parse_iso_utc(CONCERN_SINCE)


# ── what a shell carries ─────────────────────────────────────────────────────

def objective(entry: Dict[str, Any]) -> str:
    """The outcome with its moment, dated: a calendar entry names its day, and every entry when it was decided, which
    anchors a bare or relative when ("9:00 PM", "now", "Thursday, 7:00 PM"). Until 2026-10-08 it was `what (when:
    9:00 PM)`, and the architect ended that evening's phone-charge reminder as already achieved by the day before's."""
    from app.assistant.utils.time_utils import local_time_text
    parts = []
    if entry["kind"] == "calendar":
        parts.append(f"for {datetime.strptime(entry['day'], '%Y-%m-%d').strftime('%a %m-%d')}")
    if entry["when_local"]:
        parts.append(f"when: {entry['when_local']}")
    parts.append(f"decided {local_time_text(entry['updated_at'])}")
    return f"{entry['what']} ({'; '.join(parts)})"


def brief(entry: Dict[str, Any]) -> str:
    """The entry with what it serves in full: the concern as the brain's agents read it and the emails and messages
    it came from word for word, or the calendar belief with the beliefs read beside it, as the run that decided the
    entry saw them (day_tasks.decided_placement)."""
    from app.assistant.dayflow_orchestrator.work_context import render_view
    concern_text, belief, sources = "", None, []
    if entry.get("concern_id"):            # a concern's entry, or a day's entry made for a concern (2026-10-08)
        from app.assistant.subconscious.brain_step import _evidence_text
        from app.assistant.subconscious.concern_planner import concern_text as render_concern
        from app.assistant.subconscious.concern_store import load_register
        register = load_register()
        found = [(c, b) for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []
                 if c["concern_id"] == entry["concern_id"]]
        if not found:
            raise KeyError(f"brain list entry {entry['id']}: concern {entry['concern_id']} is not in the register")
        concern, bucket = found[0]
        concern_text = render_concern(concern, bucket)
        sources = [_evidence_text(e) for e in concern.get("evidence") or []]
    elif entry["kind"] == "calendar":
        from app.assistant.subconscious.day_tasks import decided_placement
        belief = decided_placement(entry["day"], entry["belief_key"])
    return render_view("brain_entry_brief", entry={**entry, "times": entry.get("placement_time") or ""},
                       concern_text=concern_text, belief=belief, sources=sources).strip()


def _belief_refs(entry: Dict[str, Any]) -> List[str]:
    """The beliefs the work carries back its outcome to: a calendar entry's belief (before 2026-10-10), or the beliefs
    of the concern an entry serves (a calendar concern carries its belief, subconscious/calendar_concerns.py)."""
    if entry["kind"] == "calendar":
        return [entry["belief_key"]]
    if not entry.get("concern_id"):
        return []
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    found = [c for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []
             if c["concern_id"] == entry["concern_id"]]
    if not found:
        raise KeyError(f"brain list entry {entry['id']}: concern {entry['concern_id']} is not in the register")
    return list(found[0].get("belief_refs") or [])


def _shell(entry: Dict[str, Any]) -> Dict[str, Any]:
    from app.assistant.dayflow_orchestrator.work_context import render_view
    goal = objective(entry)
    constraints = {"objective": goal, "success_criteria": entry["what"], "rationale": brief(entry),
                   "source_intake": [], "list_entry_id": entry["id"],
                   "concern_refs": [f"concern:{entry['concern_id']}"] if entry.get("concern_id") else [],
                   "belief_refs": _belief_refs(entry)}
    return {"title": goal, "satisfied_when_kind": "all_owned_children_done", "constraints": constraints,
            "goal_content": render_view("goal_content", objective=goal, sources=[], success_criteria=entry["what"])}


# ── acting on the list ───────────────────────────────────────────────────────

def _work_by_entry() -> Dict[str, Dict[str, Any]]:
    """entry id -> the newest work object made from it (id, status, constraints)."""
    from app.assistant.subconscious import work_links
    out: Dict[str, Dict[str, Any]] = {}
    for r in sorted(work_links._rows(), key=lambda r: r["created_at"] or ""):
        entry_id = r["constraints"].get("list_entry_id")
        if entry_id:
            out[entry_id] = r
    return out


def _decide(entry: Dict[str, Any], work: Optional[Dict[str, Any]]) -> str:
    """What the entry's current state asks of its work. Called only for an entry with undelivered changes, and the
    list records a change only when the entry did change (brain_list.upsert), so active work is revised."""
    active = work is not None and work["status"] in _ACTIVE
    if entry["status"] == "cancelled" or entry["decision"] != "needed":
        return "end" if active else "none"
    if not active:
        return "create"
    return "revise"


def deliver(store, *, now_utc: Optional[datetime] = None, connect=None, list_connect=None) -> Dict[str, Any]:
    """Act on every change on the brain's list not yet delivered, one entry at a time (its newest state stands for all
    its changes). Returns {created, revised, ended} work ids, and
    `failed`: entries whose action raised. Reading the list raises; one entry's failure is logged ERROR and left
    undelivered for the next tick, and the other entries go on (2026-10-05: until then one entry that could not be
    briefed stopped every planning tick)."""
    from app.assistant.subconscious import brain_list
    from app.assistant.subconscious.brain_trace import step
    now = now_utc or datetime.now(timezone.utc)
    connect = connect or _connect
    ensure_schema(connect)
    brain_list.ensure_schema(list_connect)
    out: Dict[str, List[Any]] = {"created": [], "revised": [], "ended": [], "failed": []}
    with (list_connect or _connect)(False) as c:
        changes = [dict(r) for r in c.execute("SELECT id, entry_id FROM brain_list_changes ORDER BY id")]
        entries = {r["id"]: dict(r) for r in c.execute("SELECT * FROM brain_list_entries")}
    with connect(False) as c:
        delivered = {r["change_id"] for r in c.execute(
            "SELECT change_id FROM brain_list_deliveries WHERE mode = 'live'")}
    pending: Dict[str, List[int]] = {}
    for ch in changes:
        if _the_brains(entries[ch["entry_id"]]) and ch["id"] not in delivered:
            pending.setdefault(ch["entry_id"], []).append(ch["id"])
    if not pending:
        return out
    by_entry = _work_by_entry()
    for entry_id, change_ids in pending.items():
        try:
            _deliver_entry(store, entries[entry_id], change_ids, by_entry.get(entry_id), now, connect, out)
        except Exception as exc:
            logger.error("[brain_list_intake] entry %s could not be delivered; left for the next tick: %s",
                         entry_id, exc, exc_info=True)
            step("list_intake", "failed: left for the next tick", subject=entry_id, error=f"{type(exc).__name__}: {exc}")
            out["failed"].append(entry_id)
    logger.info("[brain_list_intake] created=%s revised=%s ended=%s failed=%s", out["created"],
                out["revised"], out["ended"], out["failed"])
    return out


def _deliver_ending(store, work_id: str) -> None:
    """After the brain ends work: the concerns and beliefs it served get the ending, as after the architect ends a goal
    (work_architect_node._deliver_ending). Both are receipt-backed; a failed delivery is retried by the tick's prep."""
    from app.assistant.subconscious.concern_feedback import propagate_work_outcome
    from belief_engine.work_feedback import propagate_work_outcome_to_beliefs
    propagate_work_outcome(store, work_id, "abandoned")
    try:
        propagate_work_outcome_to_beliefs(store, work_id, "abandoned")
    except Exception:
        logger.error("[brain_list_intake] work %s ended by the brain; belief feedback failed", work_id, exc_info=True)


def _deliver_entry(store, entry: Dict[str, Any], change_ids: List[int], work: Optional[Dict[str, Any]],
                   now: datetime, connect, out: Dict[str, List[Any]]) -> None:
    """Act on one entry's undelivered changes and record them delivered."""
    from app.assistant.subconscious.brain_trace import step
    entry_id = entry["id"]
    action = _decide(entry, work)
    work_id = work["id"] if work else None
    if action == "create":
        wo = store.apply("create_work_object", _shell(entry), actor="brain")
        store.apply("set_status", {"work_id": wo.id, "node_id": wo.goal_node_id, "status": "dispatched"},
                    actor="brain")
        from app.assistant.subconscious.concern_feedback import propagate_work_outcome
        propagate_work_outcome(store, wo.id, "attached")
        work_id = wo.id
        out["created"].append(wo.id)
    elif action == "revise":
        from app.assistant.dayflow_orchestrator.work_intake import goal_update
        wo = store.load(work_id)
        update = goal_update(wo, objective=objective(entry), success_criteria=entry["what"])
        update["constraints"]["rationale"] = brief(entry)
        reason = f"The brain revised the entry this goal was made from: {entry['why']}"
        store.apply("batch", {"work_id": work_id, "operations": [
            {"op": "revise_goal", "data": update},
            {"op": "instruct_goal", "data": {"kind": "replan", "reason": reason}}]}, actor="brain")
        out["revised"].append(work_id)
    elif action == "end":
        reason = entry["why"] if entry["status"] != "cancelled" else f"cancelled on the brain's list: {entry['why']}"
        store.apply("set_work_status", {"work_id": work_id, "status": "abandoned", "reason": f"The brain: {reason}"},
                    actor="brain")
        _deliver_ending(store, work_id)
        out["ended"].append(work_id)
    step("list_intake", action, subject=entry_id, work_id=work_id, outcome=entry["what"])
    with connect(True) as c:
        c.executemany("INSERT OR IGNORE INTO brain_list_deliveries (change_id, mode, entry_id, at, action, work_id) "
                      "VALUES (?,?,?,?,?,?)",
                      [(cid, "live", entry_id, now.isoformat(), action, work_id) for cid in change_ids])


def work_of(entry_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """entry id -> its work as the deciders read it: the work id, its status, and each task's outcome in the
    finalizer's own words (work_links.work_view: never the verdict). Read live from the work store."""
    from app.assistant.subconscious import work_links
    by_entry = _work_by_entry()
    return {eid: work_links.work_view(by_entry[eid]["id"], link="made from this entry")
            for eid in entry_ids if eid in by_entry}
