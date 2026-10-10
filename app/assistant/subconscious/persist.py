"""The concern register's writers.

Storage: the concerns table in emi.db (subconscious/concern_store.py) — the register (active/addressing/resolved/
dormant), read and written whole under one lock; replaced resource_concerns_register.json on 2026-09-29.

Until 2026-10-09 the daily noticer wrote here too (new concerns, reinforcements, addressing, resolutions,
escalations, forced dispositions, questions to the owner and their answers); it now raises issues for the brain's
inbox and writes nothing here (docs/design/concerns_as_state_2026-10-09.md). The same day work feedback stopped
copying each judgment and ending onto the concern and moving it between active and addressing, a decline button
stopped parking it dormant, and the brain stopped attaching work by judgment: a concern holds references to the work
made for it, read live (rule 3b), and only the brain, or its expiry, closes it.
"""
from __future__ import annotations

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


def _entry_stamp(line: str):
    """The UTC time a journal entry was written ("[<UTC ISO>] text"), or None for a line without one."""
    from app.assistant.utils.time_utils import parse_iso_utc
    end = line.find("]")
    return parse_iso_utc(line[1:end]) if line.startswith("[") and end > 0 else None


def _entry_for_prompt(line: str) -> str:
    """One journal entry as agents read it: its stored UTC stamp shown in local time, as every time in a prompt is;
    a line without a stamp (the archive marker, older entries) as stored. On 2026-10-04 the concern planner read UTC
    stamps beside a local "Current Time" and called a 9 AM notice "now-past" at 3:31 AM."""
    from app.assistant.utils.time_utils import local_time_text
    stamp = _entry_stamp(line)
    return f"[{local_time_text(stamp)}]{line[line.find(']') + 1:]}" if stamp else line


def journal_for_prompt(notes: str) -> str:
    """A whole journal as agents read it, each entry in local time (_entry_for_prompt)."""
    return "\n".join(_entry_for_prompt(line) for line in _journal_entries(notes))


def unsummarized(concern: Dict[str, Any]) -> List[str]:
    """The notes no summary covers yet, oldest first, as agents read them: the note the concern was raised with
    (until the first summary folds it in), stamped with when it was first observed, then each journal entry after the
    summary's last."""
    from app.assistant.utils.time_utils import local_time_text, parse_iso_utc
    summary = concern.get("summary")
    entries = _journal_entries(concern.get("reinforcement_notes") or "")
    lines = []
    notes = str(concern.get("notes") or "").strip()
    if notes and not summary:
        lines.append(f"[{local_time_text(parse_iso_utc(concern['first_observed']))}] {notes}")
    return lines + [_entry_for_prompt(line) for line in entries[(summary or {}).get("lines", 0):]]


def history_for_prompt(concern: Dict[str, Any]) -> str:
    """A concern's record as every reader gets it: the summary of its older notes, when it has one, then every note
    written since, whole, in the order written, each with when, in local time (docs/design/concerns_as_state_2026-10-09.md,
    rule 1). Owner, 2026-10-08: "all messages that are carried need to carry timestamps"; 2026-10-09: "we cant have
    something with 100 notes on it. we also don't want to show truncated record." A concern raised on 2026-10-07 said
    "She has a doctor appointment at 10:30 today." with no date; read on the 8th, the daily-context tracker put the
    appointment on the 8th's calendar."""
    from app.assistant.utils.time_utils import local_time_text
    summary = concern.get("summary")
    head = ([f"Summary of the notes up to {local_time_text(summary['through'])}: {summary['text']}"]
            if summary else [])
    return "\n".join(head + unsummarized(concern))


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
            if u.get("expires_at"):
                concern["expires_at"] = u["expires_at"]
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


def set_concern_summary(concern_id: str, lines: int, *, text: Optional[str] = None, error: Optional[str] = None,
                        connect=None) -> None:
    """Store a concern's summary of the note it was raised with and its first `lines` journal entries (subconscious/
    concern_summary.py), or the error that stopped it. The journal only grows, so entries written meanwhile stay
    after the summary, unread by it and shown whole. Raises when the concern is gone or has fewer entries."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        for bucket in ("active", "addressing", "resolved", "dormant"):
            for concern in register.get(bucket) or []:
                if concern.get("concern_id") != concern_id:
                    continue
                entries = _journal_entries(concern.get("reinforcement_notes") or "")
                if len(entries) < lines:
                    raise ValueError(f"concern {concern_id} has {len(entries)} journal entries, not {lines}")
                now_iso = datetime.now(timezone.utc).isoformat()
                if error is None:
                    stamps = [t for t in (_entry_stamp(line) for line in entries[:lines]) if t]
                    through = stamps[-1].isoformat() if stamps else concern["first_observed"]
                    concern["summary"] = {"text": text, "lines": lines, "through": through, "written_at": now_iso}
                    concern.pop("summary_error", None)
                else:
                    concern["summary_error"] = {"lines": lines, "error": error, "at": now_iso}
                _save_register(connect, register)
                return
    raise KeyError(f"concern {concern_id} is not in the register")


def set_concern_plan(concern_id: str, from_record: str, *, plan: Optional[Dict[str, Any]] = None,
                     error: Optional[str] = None, connect=None) -> bool:
    """Store a concern's plan (subconscious/concern_planner.py), or the error that stopped it, against the record it
    was made from (concern_view.basis). Returns False, writing nothing, when the record changed meanwhile: the next
    run plans again."""
    from app.assistant.subconscious.concern_view import basis
    with _REGISTER_LOCK:
        register = _load_register(connect)
        for bucket in ("active", "addressing", "resolved", "dormant"):
            for concern in register.get(bucket) or []:
                if concern.get("concern_id") != concern_id:
                    continue
                if basis(concern, bucket) != from_record:
                    return False
                now_iso = datetime.now(timezone.utc).isoformat()
                if error is None:
                    concern["plan"] = {**plan, "basis": from_record, "written_at": now_iso}
                    concern.pop("plan_error", None)
                else:
                    concern["plan_error"] = {"basis": from_record, "error": error, "at": now_iso}
                _save_register(connect, register)
                return True
    raise KeyError(f"concern {concern_id} is not in the register")


def expire_concerns(now_utc: Optional[datetime] = None, *, connect=None) -> List[str]:
    """Close every open concern whose expiry has passed, whatever its state (docs/design/concerns_as_state_2026-10-09.md,
    rule 4; owner, 2026-10-09: "Concerns have an expiration date. time today that it becomes irrelevant no matter
    what"). Its resolution says so; its open list entries follow it (brain_list.cancel_for_closed_concerns). Until
    then a concern about one evening was asked again every day whether it still mattered. Returns the concern ids
    closed."""
    from app.assistant.utils.time_utils import local_time_text, parse_iso_utc
    now = now_utc or datetime.now(timezone.utc)
    with _REGISTER_LOCK:
        register = _load_register(connect)
        closed = []
        for bucket in ("active", "addressing"):
            for c in list(register.get(bucket) or []):
                expires = parse_iso_utc(c.get("expires_at")) if c.get("expires_at") else None
                if expires is None or expires > now:
                    continue
                register[bucket].remove(c)
                c["resolved_at_utc"] = now.isoformat()
                c["resolution_reason"] = f"its time passed: it mattered until {local_time_text(expires)}"
                register.setdefault("resolved", []).append(c)
                closed.append(c["concern_id"])
        if closed:
            register["last_updated_utc"] = now.isoformat()
            _save_register(connect, register)
            logger.info("[persist] %d concern(s) expired: %s", len(closed), closed)
    return closed


# What the day pass sets on a calendar concern when it places the day again; the brain's notes, its resolution and an
# expiry it moved are the brain's.
_PLACEMENT_FIELDS = ("title", "done_when", "notes", "calendar", "source", "belief_refs")


def apply_calendar_concerns(day: str, desired: List[Dict[str, Any]], now_utc: datetime, *,
                            connect=None) -> Dict[str, int]:
    """The day pass's concerns for `day` (subconscious/calendar_concerns.py, rule 7): each desired one raised when it
    is new, its placement updated when it is open, left as it is when the brain closed it; an open one of the day the
    plan no longer places is closed. Returns the counts."""
    now_iso = now_utc.isoformat()
    counts = {"raised": 0, "updated": 0, "withdrawn": 0}
    with _REGISTER_LOCK:
        register = _load_register(connect)
        known = {c["concern_id"]: (b, c) for b in ("active", "addressing", "resolved", "dormant")
                 for c in register.get(b) or []}
        wanted = {c["concern_id"] for c in desired}
        for d in desired:
            if d["concern_id"] not in known:
                register.setdefault("active", []).append({
                    **d, "origin": "day_pass", "first_observed": now_iso, "created_at_utc": now_iso, "evidence": [],
                    "domain_tags": [], "addressable_by": [], "attached_work": {}, "work_receipts": []})
                counts["raised"] += 1
                continue
            bucket, c = known[d["concern_id"]]
            if bucket not in ("active", "addressing"):
                continue                                   # closed by the brain: it stays closed
            if any(c.get(k) != d[k] for k in _PLACEMENT_FIELDS):
                c.update({k: d[k] for k in _PLACEMENT_FIELDS})
                counts["updated"] += 1
        for bucket in ("active", "addressing"):
            for c in list(register.get(bucket) or []):
                if (c.get("calendar") or {}).get("day") == day and c["concern_id"] not in wanted:
                    register[bucket].remove(c)
                    c["resolved_at_utc"] = now_iso
                    c["resolution_reason"] = "the day pass no longer places it today"
                    register.setdefault("resolved", []).append(c)
                    counts["withdrawn"] += 1
        if any(counts.values()):
            register["last_updated_utc"] = now_iso
            _save_register(connect, register)
    return counts


def next_expiry(register: Dict[str, Any], now_utc: datetime) -> Optional[datetime]:
    """The earliest future expiry of an open concern, or None: the brain wakes then to close it."""
    from app.assistant.utils.time_utils import parse_iso_utc
    times = [parse_iso_utc(c["expires_at"]) for b in ("active", "addressing") for c in register.get(b) or []
             if c.get("expires_at")]
    future = [t for t in times if t > now_utc]
    return min(future) if future else None


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


def attach_work(concern_ref: str, *, work_id: str, receipt_id: str = "", connect=None) -> str:
    """A work object cites this concern (the concern does not become the work, the work is attached to it; owner,
    2026-09-30). The concern keeps a reference only, the work's id and when it was attached; every reader renders the
    work from the work store when it reads it (concern_view.attempts; owner, 2026-10-09: "attached work should just be
    a reference to the work object. then its rendered"). Attaching changes nothing else: whether something is being
    done is read from the work. Returns 'attached' | 'already_applied' | 'unresolved'."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        found = _find_concern(register, concern_ref)
        if found is None:
            logger.warning("[persist.attach_work] concern ref %r names no single concern", concern_ref)
            return "unresolved"
        _, _, concern = found
        if receipt_id and receipt_id in concern.get("work_receipts", []):
            return "already_applied"
        now_iso = datetime.now(timezone.utc).isoformat()
        concern.setdefault("attached_work", {}).setdefault(work_id, {"work_id": work_id, "attached_at": now_iso})
        if receipt_id:
            concern.setdefault("work_receipts", []).append(receipt_id)
        register["last_updated_utc"] = now_iso
        _save_register(connect, register)
        return "attached"


def concern_title(concern_ref: str, *, connect=None) -> Optional[str]:
    """The title of the concern a ref names, or None when it names no single concern."""
    found = _find_concern(_load_register(connect), concern_ref)
    return found[2].get("title") if found else None
