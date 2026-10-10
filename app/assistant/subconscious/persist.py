"""The concern register's writers.

Storage: the concerns table in emi.db (subconscious/concern_store.py) — the register (active/addressing/resolved/
dormant), read and written whole under one lock; replaced resource_concerns_register.json on 2026-09-29.

Until 2026-10-09 the daily noticer wrote here too (new concerns, reinforcements, addressing, resolutions,
escalations, forced dispositions, questions to the owner and their answers); it now raises issues for the brain's
inbox and writes nothing here (docs/design/concerns_as_state_2026-10-09.md).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.subconscious import concern_store
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Every read-modify-write of the register holds this lock, so no writer can lose another's update.
_REGISTER_LOCK = threading.RLock()

# A concern keeps its whole journal and all its evidence (owner, 2026-10-08: "Do we lose context if we don't keep
# all notes? i think we do. so we have to keep em."). Until then the journal kept its newest 10 entries and the
# evidence its first 3 and newest 9; one deleted note was the record that an appointment had happened.


def _journal_entries(notes: str) -> List[str]:
    return [ln.strip() for ln in (notes or "").splitlines() if ln.strip()]


def journal_for_prompt(notes: str) -> str:
    """A concern's journal as agents read it: each entry's stored UTC stamp shown in local time, as every
    time in a prompt is. The journal is stored "[<UTC ISO>] text" per line; a line without a stamp (the
    archive marker, older entries) is shown as stored. On 2026-10-04 the concern planner read UTC stamps
    beside a local "Current Time" and called a 9 AM notice "now-past" at 3:31 AM."""
    from app.assistant.utils.time_utils import local_time_text, parse_iso_utc
    lines = []
    for line in _journal_entries(notes):
        end = line.find("]")
        stamp = parse_iso_utc(line[1:end]) if line.startswith("[") and end > 0 else None
        lines.append(f"[{local_time_text(stamp)}]{line[end + 1:]}" if stamp else line)
    return "\n".join(lines)


def history_for_prompt(concern: Dict[str, Any]) -> str:
    """A concern's notes in the order they were written, each with when, in local time: the note it was raised with,
    stamped with when it was first observed, then its journal. Owner, 2026-10-08: "all messages that are carried need
    to carry timestamps"; "we could just have the notes recorded in order of timestamp ... then it has clear history".
    A concern raised on 2026-10-07 said "She has a doctor appointment at 10:30 today." with no date; read on the 8th,
    the daily-context tracker put the appointment on the 8th's calendar."""
    from app.assistant.utils.time_utils import local_time_text, parse_iso_utc
    lines = []
    notes = str(concern.get("notes") or "").strip()
    if notes:
        lines.append(f"[{local_time_text(parse_iso_utc(concern['first_observed']))}] {notes}")
    journal = journal_for_prompt(concern.get("reinforcement_notes") or "").strip("\n")
    if journal:
        lines.append(journal)
    return "\n".join(lines)


def _journal_on(concern: Dict[str, Any], now_iso: str, line: str) -> None:
    """Append one dated line to a concern's journal."""
    concern["reinforcement_notes"] = (
        (concern.get("reinforcement_notes") or "") + f"\n[{now_iso}] {line}")


def apply_brain_matter(updates: List[Dict[str, Any]], plan: List[Dict[str, Any]], *,
                       source: str = "brain", connect=None) -> Dict[str, Optional[str]]:
    """Apply one brain matter (subconscious/brain_step.py) under the register lock: notes and
    resolutions on open concerns, then new concerns through the concern door (`plan` was made
    before the lock). Returns new-concern label -> the concern it landed in.

    A note records new facts with the events as evidence. An update naming a concern that is no longer open
    raises: the matter is recorded failed, nothing is half-written."""
    from app.assistant.subconscious import concern_door
    with _REGISTER_LOCK:
        register = _load_register(connect)
        now_iso = datetime.now(timezone.utc).isoformat()
        open_by_id = {c.get("concern_id"): (b, c) for b in ("active", "addressing") for c in register.get(b) or []}
        for u in updates:
            if u["concern_id"] not in open_by_id:
                raise KeyError(f"concern {u['concern_id']} is no longer open")
            bucket, concern = open_by_id[u["concern_id"]]
            concern.setdefault("evidence", []).extend(u["evidence"])
            if u["action"] == "note":
                concern["last_reinforced_utc"] = now_iso
                _journal_on(concern, now_iso, f"[{source}] {u['note']}")
            elif u["action"] == "resolve":
                concern["resolved_at_utc"] = now_iso
                concern["resolution_reason"] = u["note"]
                concern["resolution_evidence"] = u["evidence"]
                _journal_on(concern, now_iso, f"RESOLVED by the {source}: {u['note']}")
                register[bucket] = [c for c in register[bucket] if c.get("concern_id") != u["concern_id"]]
                register.setdefault("resolved", []).append(concern)
                del open_by_id[u["concern_id"]]
            else:
                raise ValueError(f"unknown brain update action {u['action']!r}")
        admitted = concern_door.apply_admission(plan, register, source=source, now_iso=now_iso)
        register["last_updated_utc"] = now_iso
        _save_register(connect, register)
    return admitted


def retire_standing_concerns(reason: str, *, connect=None) -> List[str]:
    """Move every open standing concern (one made from the owner's beliefs by the day pass, until
    2026-10-03) to dormant, journalled with `reason`. Beliefs are no longer made into concerns
    (docs/design/what_was_done_reaches_the_brain_2026-10-03.md). Returns the concern ids moved."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        now_iso = datetime.now(timezone.utc).isoformat()
        moved = []
        for bucket in ("active", "addressing"):
            for c in [c for c in register.get(bucket) or [] if c.get("kind") == "standing"]:
                register[bucket].remove(c)
                c.pop("occurrences", None)
                c["dormant_at_utc"] = now_iso
                c["dormant_reason"] = reason
                _journal_on(c, now_iso, f"RETIRED to dormant: {reason}")
                register.setdefault("dormant", []).append(c)
                moved.append(c["concern_id"])
        if moved:
            register["last_updated_utc"] = now_iso
            _save_register(connect, register)
    return moved


def journal_concern(concern_id: str, line: str, *, connect=None) -> None:
    """Append one dated line to a concern's journal, whatever bucket it is in. Raises when the
    concern is not in the register."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        for bucket in ("active", "addressing", "resolved", "dormant"):
            for concern in register.get(bucket) or []:
                if concern.get("concern_id") == concern_id:
                    _journal_on(concern, datetime.now(timezone.utc).isoformat(), line)
                    _save_register(connect, register)
                    return
    raise KeyError(f"concern {concern_id} is not in the register")


def set_concern_brief(concern_id: str, written_from: str, *, brief: Optional[Dict[str, Any]] = None,
                      error: Optional[str] = None, connect=None) -> bool:
    """Store a concern's brief (or the error that stopped it) against the record it was written from.
    Returns False, writing nothing, when the record changed meanwhile: the next run writes it again."""
    from app.assistant.subconscious import concern_brief
    with _REGISTER_LOCK:
        register = _load_register(connect)
        for bucket in ("active", "addressing", "resolved", "dormant"):
            for concern in register.get(bucket) or []:
                if concern.get("concern_id") != concern_id:
                    continue
                if concern_brief.basis(concern, bucket) != written_from:
                    return False
                now_iso = datetime.now(timezone.utc).isoformat()
                if error is None:
                    concern["brief"] = {**brief, "basis": written_from, "written_at": now_iso}
                    concern.pop("brief_error", None)
                else:
                    concern["brief_error"] = {"basis": written_from, "error": error, "at": now_iso,
                                              "writer": concern_brief.WRITER_REVISION}
                _save_register(connect, register)
                return True
    raise KeyError(f"concern {concern_id} is not in the register")


def set_concern_plan(concern_id: str, from_brief: str, *, plan: Optional[Dict[str, Any]] = None,
                     error: Optional[str] = None, connect=None) -> bool:
    """Store a matter concern's plan (subconscious/concern_planner.py), or the error that stopped it, against
    the brief it was made from. Returns False, writing nothing, when the brief changed meanwhile: the next
    run plans again."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        for bucket in ("active", "addressing", "resolved", "dormant"):
            for concern in register.get(bucket) or []:
                if concern.get("concern_id") != concern_id:
                    continue
                if (concern.get("brief") or {}).get("basis") != from_brief:
                    return False
                now_iso = datetime.now(timezone.utc).isoformat()
                if error is None:
                    concern["plan"] = {**plan, "basis": from_brief, "written_at": now_iso}
                    concern.pop("plan_error", None)
                else:
                    concern["plan_error"] = {"basis": from_brief, "error": error, "at": now_iso}
                _save_register(connect, register)
                return True
    raise KeyError(f"concern {concern_id} is not in the register")


def _load_register(connect=None) -> Dict[str, Any]:
    """The whole register from the concerns table. An unreadable row raises: the spine
    deserves fail-loud, never a fresh register the next save would write over every concern."""
    return concern_store.load_register(connect=connect)


def _save_register(connect, register: Dict[str, Any]) -> None:
    """The whole register back, in one transaction."""
    concern_store.save_register(register, connect=connect)


def _find_concern(register: Dict[str, Any], concern_ref: str):
    """(bucket, index, concern) for a full concern_id or `concern:<8+ chars>`; None unless exactly one."""
    ref = str(concern_ref or "").strip()
    if ref.startswith("concern:"):
        ref = ref[len("concern:"):].strip()
    if not ref:
        return None
    matches = [(bucket, i, c) for bucket in ("active", "addressing", "dormant", "resolved")
               for i, c in enumerate(register.get(bucket) or [])
               if str(c.get("concern_id") or "") == ref
               or (len(ref) >= 8 and str(c.get("concern_id") or "").startswith(ref))]
    return matches[0] if len(matches) == 1 else None


def concern_id_for(concern_ref: str, *, connect=None) -> Optional[str]:
    """The full concern_id a ref names, or None when it names no single concern."""
    found = _find_concern(_load_register(connect), concern_ref)
    return found[2]["concern_id"] if found else None


def _move(register: Dict[str, Any], bucket: str, index: int, concern: Dict[str, Any], to: str) -> None:
    register[bucket].pop(index)
    register.setdefault(to, []).append(concern)


def _work_in_progress(concern: Dict[str, Any]) -> List[str]:
    return [w for w, rec in (concern.get("attached_work") or {}).items() if rec.get("status") == "active"]


def attach_work(concern_ref: str, *, work_id: str, work: Dict[str, Any], receipt_id: str = "",
                connect=None) -> str:
    """A work object now cites this concern (owner, 2026-09-30: the concern does not become the work,
    the work is attached to it, so the concern shows how it is progressing). The work gets its record
    on the concern; an active concern becomes `addressing`: something is being done about it.
    Returns 'attached' | 'already_applied' | 'unresolved'."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        found = _find_concern(register, concern_ref)
        if found is None:
            logger.warning("[persist.attach_work] concern ref %r names no single concern", concern_ref)
            return "unresolved"
        bucket, index, concern = found
        if receipt_id and receipt_id in concern.get("work_receipts", []):
            return "already_applied"
        now_iso = datetime.now(timezone.utc).isoformat()
        concern.setdefault("attached_work", {}).setdefault(work_id, {
            "work_id": work_id, "title": work.get("title"), "objective": work.get("objective"),
            "success_criteria": work.get("success_criteria") or "", "attached_at": work.get("attached_at"),
            "status": "active", "judgments": [], "ended": None})
        _journal_on(concern, now_iso, f"WORK ATTACHED {work_id}: {work.get('objective')}")
        if receipt_id:
            concern.setdefault("work_receipts", []).append(receipt_id)
        if bucket == "active":
            _move(register, bucket, index, concern, "addressing")
            concern["addressing_since_utc"] = now_iso
        register["last_updated_utc"] = now_iso
        _save_register(connect, register)
        return "attached"


def concern_title(concern_ref: str, *, connect=None) -> Optional[str]:
    """The title of the concern a ref names, or None when it names no single concern."""
    found = _find_concern(_load_register(connect), concern_ref)
    return found[2].get("title") if found else None


def concerns_holding_work(work_id: str, *, connect=None) -> List[str]:
    """Every concern, in any bucket, that holds this work as attached work: the ones it cites, and the
    ones the brain attached it to (`attach_seen_work`). Concern ids."""
    register = _load_register(connect)
    return [c["concern_id"] for b in ("active", "addressing", "dormant", "resolved") for c in register.get(b) or []
            if work_id in (c.get("attached_work") or {})]


def attach_seen_work(concern_id: str, *, work_id: str, work: Dict[str, Any], receipt_id: str,
                     connect=None) -> str:
    """The brain found that work bears on a concern it was not made for (it used the work's report on the
    concern; docs/design/what_was_done_reaches_the_brain_2026-10-03.md): the work is attached here too, so
    its later judgments and its ending reach this concern by code. `work` carries the work's status now
    ('active' | 'done' | 'abandoned') and, when it ended, `ended`; the concern is in progress only while
    the work is. Returns 'attached' | 'already_attached' | 'unresolved'."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        found = _find_concern(register, concern_id)
        if found is None:
            logger.warning("[persist.attach_seen_work] %r names no single concern", concern_id)
            return "unresolved"
        bucket, index, concern = found
        if work_id in (concern.get("attached_work") or {}) or receipt_id in concern.get("work_receipts", []):
            return "already_attached"
        now_iso = datetime.now(timezone.utc).isoformat()
        status = work.get("status") or "active"
        concern.setdefault("attached_work", {})[work_id] = {
            "work_id": work_id, "title": work.get("title"), "objective": work.get("objective"),
            "success_criteria": work.get("success_criteria") or "", "attached_at": now_iso,
            "status": status, "judgments": [], "ended": work.get("ended")}
        _journal_on(concern, now_iso, f"WORK ATTACHED {work_id} by the brain (its outcome bears on this concern): "
                                      f"{work.get('objective')}")
        concern.setdefault("work_receipts", []).append(receipt_id)
        if bucket == "active" and status == "active":
            _move(register, bucket, index, concern, "addressing")
            concern["addressing_since_utc"] = now_iso
        register["last_updated_utc"] = now_iso
        _save_register(connect, register)
        return "attached"


def record_judgment(concern_ref: str, *, work_id: str, task: Dict[str, Any], receipt_id: str = "",
                    connect=None) -> str:
    """The finalizer judged one task of attached work: its verdict, account, and the owner's replies,
    kept on the work's record. Returns 'recorded' | 'already_applied' | 'unresolved'; raises when the
    work was never attached to the concern."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        found = _find_concern(register, concern_ref)
        if found is None:
            logger.warning("[persist.record_judgment] concern ref %r names no single concern", concern_ref)
            return "unresolved"
        _, _, concern = found
        if receipt_id and receipt_id in concern.get("work_receipts", []):
            return "already_applied"
        record = (concern.get("attached_work") or {}).get(work_id)
        if record is None:
            raise ValueError(f"work {work_id} was never attached to concern {concern['concern_id']}")
        fin = task.get("finalizer") or {}
        judgment = {"node_id": task.get("node_id"), "title": task.get("title"), "verdict": fin.get("verdict"),
                    "next_step": fin.get("next_step"), "outcome": fin.get("outcome"),
                    "recommendation": fin.get("recommendation"), "at": fin.get("at"),
                    "replies": list(task.get("replies") or [])}
        if not any((j.get("node_id"), j.get("at")) == (judgment["node_id"], judgment["at"])
                   for j in record["judgments"]):
            record["judgments"].append(judgment)
        if receipt_id:
            concern.setdefault("work_receipts", []).append(receipt_id)
        register["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
        _save_register(connect, register)
        return "recorded"


def apply_work_outcome(
    concern_ref: str,
    *,
    work_id: str,
    outcome: str,
    user_words: str = "",
    user_response: Optional[dict] = None,
    connect=None,
    receipt_id: str = "",
    work_context: Optional[dict] = None,
) -> str:
    """Attached work ended (2026-08-01 audit: outcomes never reached the register — 19 AC-service
    re-mints, 4 after an explicit user decline).

    ``concern_ref`` is the full concern_id or the rendered short form ``concern:<prefix>``.
    The work's record on the concern gets its ending (outcome, the reason, the owner's last reply).
      abandoned + unqualified explicit decline button -> park DORMANT
      otherwise, once no attached work is in progress   -> `addressing` back to `active`: whether the
                                                          concern is settled is the brain's decision,
                                                          made from the ending as a brain event
    The owner's reply is also journalled verbatim. Returns 'user_declined' | 'ended' |
    'already_applied' | 'unresolved'. Lives with the other register writers: one lock, one atomic save.
    """
    with _REGISTER_LOCK:
        register = _load_register(connect)
        found = _find_concern(register, concern_ref)
        if found is None:
            logger.warning("[persist.work_outcome] concern ref %r names no single concern — unresolved",
                           concern_ref)
            return "unresolved"
        bucket, index, concern = found
        if receipt_id and receipt_id in concern.get("work_receipts", []):
            return "already_applied"
        now_iso = datetime.now(timezone.utc).isoformat()

        response = dict(user_response or {})
        context = dict(work_context or {})
        record = concern.setdefault("attached_work", {}).setdefault(work_id, {
            "work_id": work_id, "title": context.get("title"), "objective": context.get("title"),
            "success_criteria": "", "attached_at": None, "status": "active", "judgments": [], "ended": None})
        record["status"] = outcome
        record["ended"] = {"outcome": outcome, "at": now_iso,
                           "reason": (context.get("terminal") or {}).get("reason") or "",
                           "owner_reply": response, "legacy_user_words": user_words}
        if receipt_id:
            concern.setdefault("work_receipts", []).append(receipt_id)

        details = response.get("response_details") or {}
        history = details.get("response_history") or []
        latest = history[-1] if history else details
        # Typed text can qualify or override any button. Leave that interpretation
        # to the brain; do not infer intent from words or lifecycle state.
        explicit_decline = (latest.get("meaning") == "decline"
                            and not str(latest.get("typed_text") or "").strip())
        if response:
            _journal_on(concern, now_iso, f"USER RESPONSE via {work_id}: " + json.dumps(response, ensure_ascii=False))
        elif user_words.strip():
            _journal_on(concern, now_iso, f"USER WORDS via {work_id}: " + json.dumps(user_words, ensure_ascii=False))
        _journal_on(concern, now_iso, f"WORK ENDED {work_id} ({outcome})")
        if outcome == "abandoned" and explicit_decline:
            _journal_on(concern, now_iso, f"USER DECLINED via {work_id} (explicit scoped choice; see response above)")
            concern["user_declined_at_utc"] = now_iso
            concern["last_disposition_at_count"] = int(concern.get("reinforcement_count") or 0)
            if bucket in ("active", "addressing"):
                _move(register, bucket, index, concern, "dormant")
            result = "user_declined"
        else:
            if bucket == "addressing" and not _work_in_progress(concern):
                _move(register, bucket, index, concern, "active")
                concern.pop("addressing_since_utc", None)
                concern.pop("addressing_reviewed_at_utc", None)
            result = "ended"

        register["last_updated_utc"] = now_iso
        _save_register(connect, register)
        logger.info("[persist.work_outcome] %s -> %s (%s, outcome=%s)",
                    work_id, concern.get("concern_id"), result, outcome)
        return result


def rederive_attached_work(load_work_objects, *, connect=None) -> List[Dict[str, Any]]:
    """One-time move to attached work (owner-approved, 2026-09-30), for every concern without an
    `attached_work` record: its record is built from the work objects citing it (objective, status,
    each task's latest finalizer judgment and the owner's replies to it) and from its old
    `work_outcomes` endings; `work_outcomes` is dropped and `work_outcome_receipts` becomes
    `work_receipts`. Its bucket then follows the work: `addressing` when attached work is in progress,
    otherwise an `addressing` concern goes back to `active`.

    Returns the concerns that went back to `active`, each with its latest ended work
    ({concern_id, work}): their work ended before its ending could reach the brain, so the caller
    reports each ending to the brain as a live ending would be. `load_work_objects()` returns the
    store's work objects; it is called only when a concern still has no record (new concerns are
    created with one, concern_door.apply_admission)."""
    from work_objects.concern_outbox import _replies
    citing: Dict[str, List[Any]] = {}
    with _REGISTER_LOCK:
        register = _load_register(connect)
        if all("attached_work" in c for b in ("active", "addressing", "resolved", "dormant")
               for c in register.get(b) or []):
            return []
        for wo in load_work_objects():
            for ref in (wo.constraints or {}).get("concern_refs") or []:
                found = _find_concern(register, ref)
                if found:
                    citing.setdefault(found[2]["concern_id"], []).append(wo)
        changed, reopened = 0, []
        for bucket in ("active", "addressing", "resolved", "dormant"):
            for concern in list(register.get(bucket) or []):
                if "attached_work" in concern:
                    continue
                attached = {}
                for wo in citing.get(concern["concern_id"], []):
                    k = wo.constraints or {}
                    goal = wo.nodes.get(wo.goal_node_id)
                    terminal = (goal.payload.get("terminal") or {}) if goal else {}
                    attached[wo.id] = {
                        "work_id": wo.id, "title": wo.title, "objective": k.get("objective") or wo.title,
                        "success_criteria": k.get("success_criteria") or "", "attached_at": str(wo.created_at),
                        "status": wo.status if wo.status in ("done", "abandoned") else "active",
                        "judgments": [{"node_id": n.id, "title": n.title,
                                       "verdict": n.payload["finalizer"].get("verdict"),
                                       "next_step": n.payload["finalizer"].get("next_step"),
                                       "outcome": n.payload["finalizer"].get("outcome"),
                                       "recommendation": n.payload["finalizer"].get("recommendation"),
                                       "at": n.payload["finalizer"].get("at"), "replies": _replies(wo, n.id)}
                                      for n in sorted(wo.nodes.values(), key=lambda n: str(n.created_at))
                                      if wo.is_work_unit(n) and n.payload.get("finalizer")],
                        "ended": {"outcome": wo.status, "at": terminal.get("at") or str(wo.updated_at),
                                  "reason": terminal.get("reason") or "", "owner_reply": {},
                                  "legacy_user_words": ""} if wo.status in ("done", "abandoned") else None}
                for w, old in (concern.pop("work_outcomes", None) or {}).items():
                    rec = attached.setdefault(w, {"work_id": w, "title": (old.get("context") or {}).get("title"),
                                                  "objective": (old.get("context") or {}).get("title"),
                                                  "success_criteria": "", "attached_at": None,
                                                  "status": old.get("outcome"), "judgments": [], "ended": None})
                    rec["ended"] = {"outcome": old.get("outcome"), "at": old.get("recorded_at"),
                                    "reason": ((old.get("context") or {}).get("terminal") or {}).get("reason") or "",
                                    "owner_reply": old.get("user_response") or {},
                                    "legacy_user_words": old.get("legacy_user_words") or ""}
                concern["attached_work"] = attached
                concern["work_receipts"] = concern.pop("work_outcome_receipts", None) or []
                in_progress = _work_in_progress(concern)
                if bucket == "active" and in_progress:
                    register["active"].remove(concern)
                    register.setdefault("addressing", []).append(concern)
                elif bucket == "addressing" and not in_progress:
                    register["addressing"].remove(concern)
                    register.setdefault("active", []).append(concern)
                    concern.pop("addressing_since_utc", None)
                    concern.pop("addressing_reviewed_at_utc", None)
                    ended = [w for w in attached.values() if w["ended"]]
                    if ended:
                        reopened.append({"concern_id": concern["concern_id"], "title": concern.get("title"),
                                         "work": max(ended, key=lambda w: str(w["ended"]["at"]))})
                changed += 1
        if changed:
            register["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
            _save_register(connect, register)
            logger.info("[persist] attached work derived for %d concern(s); %d back to active",
                        changed, len(reopened))
        return reopened
