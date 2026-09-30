"""Persist a noticer tick's output.

Storage:
- the concerns table in emi.db (subconscious/concern_store.py) — the register
  (active/addressing/resolved/dormant), read and written whole under one lock;
  replaced resource_concerns_register.json on 2026-09-29
- resource_subconscious_tick_log.jsonl — one line per tick: full AgentForm dump
  for audit + later replay

Belief updates are still recorded in the tick log only.
Pending questions are persisted into the pending_question queue; the
chat-reply injector (pending_questions/injector.py, consulted by the
context injector at prompt time) surfaces them as nudges the chat agent
can weave into its replies.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.subconscious import concern_store
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_TICK_LOG_REL = "resources/subconscious/resource_subconscious_tick_log.jsonl"

# The register is the subconscious's spine and has TWO writers (the noticer
# tick and answer capture's concern journaling, which deliberately runs
# seconds before a triggered tick). Every read-modify-write of the register
# holds this lock so neither writer can lose the other's update.
_REGISTER_LOCK = threading.RLock()

# ── Lifecycle pressure knobs (2026-06-11) ────────────────────────────────
# A concern reinforced this many times since its last disposition MUST get
# a disposition from the noticer (accept_chronic / re_escalate /
# keep_active). Stops eternal reinforcement sinks like the sleep concern
# that accumulated 64 evidence items over 3 weeks with no decision.
DISPOSITION_REINFORCEMENT_THRESHOLD = 8
# A concern sitting in `addressing` this many days without resolution is
# due for review. Age alone is not evidence that handling failed or permission
# to contact the user again.
ADDRESSING_STALE_DAYS = 4
# Evidence list cap per concern: keep the founding items + the freshest.
_EVIDENCE_KEEP_HEAD = 3
_EVIDENCE_KEEP_TAIL = 9
# Reinforcement journal cap (entries, newest kept).
_JOURNAL_KEEP = 10


def _trim_evidence(existing: Dict[str, Any]) -> None:
    """Cap the evidence list, counting what was dropped."""
    evidence = existing.get("evidence") or []
    cap = _EVIDENCE_KEEP_HEAD + _EVIDENCE_KEEP_TAIL
    if len(evidence) <= cap:
        return
    dropped = len(evidence) - cap
    existing["evidence"] = evidence[:_EVIDENCE_KEEP_HEAD] + evidence[-_EVIDENCE_KEEP_TAIL:]
    existing["evidence_archived_count"] = int(existing.get("evidence_archived_count") or 0) + dropped


def _journal_entries(notes: str) -> List[str]:
    return [ln.strip() for ln in (notes or "").splitlines() if ln.strip()]


def _trim_journal(existing: Dict[str, Any]) -> None:
    """Cap the reinforcement_notes journal to the newest entries."""
    entries = _journal_entries(existing.get("reinforcement_notes") or "")
    if len(entries) <= _JOURNAL_KEEP:
        return
    dropped = len(entries) - _JOURNAL_KEEP
    kept = entries[-_JOURNAL_KEEP:]
    existing["reinforcement_notes"] = "\n" + "\n".join(
        [f"({dropped} earlier notes archived)"] + kept
    )


def _bump_reinforcement_count(existing: Dict[str, Any]) -> int:
    """Increment the explicit counter, backfilling from the journal for
    concerns that predate the counter."""
    count = existing.get("reinforcement_count")
    if count is None:
        count = len(_journal_entries(existing.get("reinforcement_notes") or ""))
    count = int(count) + 1
    existing["reinforcement_count"] = count
    return count


def compute_pressure(register: Dict[str, Any], *, now_utc: Optional[datetime] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Deterministically rank which concerns demand a disposition.

    Returns {"needs_disposition": [...], "addressing_stale": [...]} with the
    raw concern dicts. Pure read — the LLM decides what to DO (the
    deterministic side only proposes). Used by the noticer context builder.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    needs: List[Dict[str, Any]] = []
    stale: List[Dict[str, Any]] = []

    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            count = c.get("reinforcement_count")
            if count is None:
                count = len(_journal_entries(c.get("reinforcement_notes") or ""))
            since_disposition = int(count) - int(c.get("last_disposition_at_count") or 0)
            if since_disposition >= DISPOSITION_REINFORCEMENT_THRESHOLD:
                needs.append(c)

    for c in register.get("addressing") or []:
        since_raw = str(c.get("addressing_reviewed_at_utc") or c.get("addressing_since_utc") or "").strip()
        if not since_raw:
            continue
        try:
            since = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now_utc - since).days >= ADDRESSING_STALE_DAYS:
            stale.append(c)

    return {"needs_disposition": needs, "addressing_stale": stale}


def apply_noticer_output(
    output: Dict[str, Any],
    *,
    connect=None,
    tick_log_path: Optional[Path] = None,
    judge=None,
) -> Dict[str, Any]:
    """Apply one noticer tick to the concerns register.

    Accepts the raw dict shape (what JSON-roundtrip of AgentForm produces).
    Returns a small summary dict for the runner to print. `connect`/`tick_log_path`
    default to emi.db and the real tick log; tests pass a scratch store and path, and
    `judge` in place of the concern_door agent.

    New concerns go through the concern door (subconscious/concern_door.py): planned first,
    outside the lock (it may call a model), then applied with everything else under the
    register lock for the whole read-modify-write, so a concurrent answer-capture journal
    write can't be lost.
    """
    from app.assistant.subconscious import concern_door
    plan = concern_door.plan_admission(output.get("new_concerns") or [], _load_register(connect), judge=judge)
    with _REGISTER_LOCK:
        return _apply_noticer_output_locked(output, plan, connect=connect, tick_log_path=tick_log_path)


def _journal_on(concern: Dict[str, Any], now_iso: str, line: str) -> None:
    """Append one dated line to a concern's journal, respecting the journal cap."""
    concern["reinforcement_notes"] = (
        (concern.get("reinforcement_notes") or "") + f"\n[{now_iso}] {line}")
    _trim_journal(concern)


def _settled_anchors(register: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Anchor -> the concern carrying the owner's standing ruling about it.

    A ruling is one of exactly two things, and the narrowness is the point:

      * ``user_declined_at_utc`` — the owner said no to this thing.
      * ``chronic`` (accept_chronic) — a standing decision that the pattern is real but not
        worth tick-by-tick attention.

    Deliberately NOT included:

      * ``resolved`` — a resolved concern means the need was MET or the moment passed, which
        is no reason to refuse the next one. Monthly timesheets and the dogs' medication
        legitimately mint again each cycle, and suppressing those would break the feature to
        fix the bug.
      * dormant for any other reason — only an explicit ruling suppresses.

    Scanned across every bucket because a ruling outlives the bucket a concern sits in: a
    declined concern is parked dormant, and dormant was exactly where the old dedup did not
    look. Later buckets win on a duplicate anchor so the freshest ruling is the one enforced.
    """
    settled: Dict[str, Dict[str, Any]] = {}
    for bucket in ("resolved", "dormant", "addressing", "active"):
        for c in register.get(bucket) or []:
            anchor = str(c.get("anchor") or "").strip()
            if not anchor:
                continue
            if c.get("user_declined_at_utc") or c.get("chronic"):
                settled[anchor] = c
    return settled


def _refuse_owner_close(concern: Dict[str, Any], now_iso: str, what: str) -> bool:
    """A concern the owner asked for closes only when its done_when is met (work achieved it) or
    the owner says it is over (the brain step reads the owner's words). The noticer infers; it does
    not get to close one. Refused attempts are journalled on the concern."""
    if not concern.get("owner_request"):
        return False
    _journal_on(concern, now_iso, f"REFUSED a noticer {what}: the owner asked for this; it closes when "
                                  "its done-when is met or the owner says it is over")
    logger.warning("[noticer.persist] refused to close owner-requested concern %s: %s",
                   concern.get("concern_id"), what)
    return True


def apply_brain_matter(updates: List[Dict[str, Any]], plan: List[Dict[str, Any]], *,
                       source: str = "brain", connect=None) -> Dict[str, Optional[str]]:
    """Apply one brain matter (subconscious/brain_step.py) under the register lock: notes and
    resolutions on open concerns, then new concerns through the concern door (`plan` was made
    before the lock). Returns new-concern label -> the concern it landed in.

    A note records new facts with the events as evidence; it is not a reinforcement of the same
    signal, so it does not count toward the noticer's disposition pressure. An update naming a
    concern that is no longer open raises: the matter is recorded failed, nothing is half-written."""
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
                _trim_evidence(concern)
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
                    concern["brief_error"] = {"basis": written_from, "error": error, "at": now_iso}
                _save_register(connect, register)
                return True
    raise KeyError(f"concern {concern_id} is not in the register")


def _apply_noticer_output_locked(
    output: Dict[str, Any],
    plan: List[Dict[str, Any]],
    *,
    connect=None,
    tick_log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    tick_log_path = tick_log_path or (get_repo_root() / _TICK_LOG_REL)

    register = _load_register(connect)
    now_utc_iso = datetime.now(timezone.utc).isoformat()

    new_concerns = output.get("new_concerns") or []
    reinforced_concerns = output.get("reinforced_concerns") or []
    addressing_concerns = output.get("addressing_concerns") or []
    resolved_concerns = output.get("resolved_concerns") or []
    escalated_concerns = output.get("escalated_concerns") or []
    concern_dispositions = output.get("concern_dispositions") or []
    question_outcomes = output.get("question_outcomes") or []
    belief_updates = output.get("belief_updates") or []
    pending_questions = output.get("pending_questions") or []

    # Index active + addressing for fast lookup
    by_id: Dict[str, Dict[str, Any]] = {}
    for status_key in ("active", "addressing"):
        for c in register.get(status_key, []) or []:
            cid = c.get("concern_id")
            if cid:
                by_id[cid] = c

    # 1. New concerns, through the concern door: created with a code-assigned id, folded into
    # the concern they duplicate, or journalled on the settled one they repeat.
    from app.assistant.subconscious import concern_door
    admitted = concern_door.apply_admission(plan, register, source="noticer", now_iso=now_utc_iso)
    # References to a just-raised concern use its label; point them at where it landed.
    for item in list(pending_questions) + list(belief_updates):
        ref = item.get("related_concern_id")
        if ref in admitted:
            item["related_concern_id"] = admitted[ref]

    # 2. Reinforcements → update in-place (with growth caps: evidence and
    # the notes journal are bounded, and an explicit reinforcement_count
    # feeds the disposition-pressure rule).
    for r in reinforced_concerns:
        cid = r.get("concern_id")
        if not cid or cid not in by_id:
            logger.warning("[noticer.persist] reinforcement targets unknown concern %s", cid)
            continue
        existing = by_id[cid]
        existing.setdefault("evidence", []).extend(r.get("new_evidence") or [])
        existing["last_reinforced_utc"] = now_utc_iso
        sev_change = r.get("severity_change")
        if sev_change == "raised":
            existing["severity"] = _bump_severity(existing.get("severity"), up=True)
        elif sev_change == "lowered":
            existing["severity"] = _bump_severity(existing.get("severity"), up=False)
        # Counter bump BEFORE the journal append — the backfill path counts
        # journal entries, which must not include this tick's own note.
        _bump_reinforcement_count(existing)
        notes = r.get("notes")
        if notes:
            existing["reinforcement_notes"] = (existing.get("reinforcement_notes") or "") + f"\n[{now_utc_iso}] {notes}"
        _trim_evidence(existing)
        _trim_journal(existing)

    # 2b. Addressing → move active → addressing. Work is in flight (e.g. the dayflow orchestrator
    # already researched it) but the concern is NOT resolved yet (no booking/appointment). Moving
    # it out of `active` stops it nagging the planner (project_concerns reads only `active`) while
    # keeping it tracked. This is owner-driven: the noticer decides this from reading dayflow's
    # public outcomes; Dayflow also delivers durable closure receipts through this writer.
    for a in addressing_concerns:
        cid = a.get("concern_id")
        if not cid or cid not in by_id:
            logger.warning("[noticer.persist] addressing targets unknown concern %s", cid)
            continue
        existing = by_id[cid]
        existing["addressing_since_utc"] = now_utc_iso
        existing["addressing_reviewed_at_utc"] = now_utc_iso
        note = a.get("notes")
        if note:
            existing["reinforcement_notes"] = (
                (existing.get("reinforcement_notes") or "") + f"\n[{now_utc_iso}] addressing: {note}"
            )
        register["active"] = [c for c in register.get("active", []) if c.get("concern_id") != cid]
        if not any(c.get("concern_id") == cid for c in register.get("addressing", [])):
            register.setdefault("addressing", []).append(existing)

    # 3. Resolutions → move active/addressing → resolved
    for r in resolved_concerns:
        cid = r.get("concern_id")
        if not cid or cid not in by_id:
            logger.warning("[noticer.persist] resolution targets unknown concern %s", cid)
            continue
        existing = by_id[cid]
        if _refuse_owner_close(existing, now_utc_iso, f"resolution ({r.get('reason', '')})"):
            continue
        existing["resolved_at_utc"] = now_utc_iso
        existing["resolution_reason"] = r.get("reason", "")
        existing["resolution_evidence"] = r.get("evidence", [])
        # Remove from active/addressing, append to resolved
        for status_key in ("active", "addressing"):
            register[status_key] = [c for c in register.get(status_key, []) if c.get("concern_id") != cid]
        register.setdefault("resolved", []).append(existing)
        by_id.pop(cid, None)

    # 4. Escalations → mark on the concern (kept in active) + tick log entry
    for e in escalated_concerns:
        cid = e.get("concern_id")
        if not cid or cid not in by_id:
            logger.warning("[noticer.persist] escalation targets unknown concern %s", cid)
            continue
        existing = by_id[cid]
        existing["escalation"] = {
            "target": e.get("target"),
            "urgency": e.get("urgency"),
            "reason": e.get("reason"),
            "escalated_at_utc": now_utc_iso,
        }

    # 4b. Dispositions — the forced decision on long-running concerns
    # (the pressure rule in compute_pressure demands these).
    for d in concern_dispositions:
        cid = d.get("concern_id")
        action = str(d.get("action") or "").strip()
        if not cid or cid not in by_id:
            logger.warning("[noticer.persist] disposition targets unknown concern %s", cid)
            continue
        existing = by_id[cid]
        reason = str(d.get("reason") or "").strip()
        existing["reinforcement_notes"] = (
            (existing.get("reinforcement_notes") or "")
            + f"\n[{now_utc_iso}] disposition={action}: {reason}"
        )

        if action == "accept_chronic" and _refuse_owner_close(existing, now_utc_iso, f"accept_chronic ({reason})"):
            continue
        if action == "accept_chronic":
            # Known long-term pattern; stop tracking it tick-by-tick. Keeps a
            # compact record in `dormant` (founding + freshest evidence only).
            existing["chronic"] = True
            existing["dormant_at_utc"] = now_utc_iso
            existing["dormant_reason"] = reason
            evidence = existing.get("evidence") or []
            if len(evidence) > 4:
                existing["evidence_archived_count"] = (
                    int(existing.get("evidence_archived_count") or 0) + len(evidence) - 4
                )
                existing["evidence"] = evidence[:2] + evidence[-2:]
            for status_key in ("active", "addressing"):
                register[status_key] = [
                    c for c in register.get(status_key, []) if c.get("concern_id") != cid
                ]
            register.setdefault("dormant", []).append(existing)
            by_id.pop(cid, None)
        elif action == "re_escalate":
            # The handoff stalled (stale `addressing`) or the pattern needs
            # another push: back to active + a fresh escalation marker.
            register["addressing"] = [
                c for c in register.get("addressing", []) if c.get("concern_id") != cid
            ]
            if not any(c.get("concern_id") == cid for c in register.get("active", [])):
                register.setdefault("active", []).append(existing)
            existing.pop("addressing_since_utc", None)
            existing.pop("addressing_reviewed_at_utc", None)
            existing["escalation"] = {
                "target": "dayflow_orchestrator",
                "urgency": "high",
                "reason": reason or "re-escalated after stalled handling",
                "escalated_at_utc": now_utc_iso,
            }
            existing["last_disposition_at_count"] = int(existing.get("reinforcement_count") or 0)
        elif action == "keep_active":
            if existing in register.get("addressing", []):
                existing["addressing_reviewed_at_utc"] = now_utc_iso
            # Justified continuation — resets the pressure window so the
            # rule doesn't re-fire next tick.
            existing["last_disposition_at_count"] = int(existing.get("reinforcement_count") or 0)
        else:
            logger.warning("[noticer.persist] unknown disposition action %r for %s", action, cid)

    # 4c. Question outcomes — retire processed/expired mailbox items so
    # they don't re-demand attention next tick. The concern-side effects
    # were emitted as regular ops in this same output.
    outcomes_applied = 0
    for qo in question_outcomes:
        qid = str(qo.get("question_id") or "").strip()
        outcome = str(qo.get("outcome") or "").strip()
        if not qid:
            continue
        status = "closed" if outcome == "processed" else "expired"
        try:
            from app.assistant.pending_questions import close_question
            if close_question(qid, outcome=status, notes=str(qo.get("notes") or "")):
                outcomes_applied += 1
        except Exception:
            logger.exception("[noticer.persist] question outcome failed for %s", qid)

    register["last_updated_utc"] = now_utc_iso
    register["last_noticer_tick_utc"] = now_utc_iso
    _save_register(connect, register)

    # 5. Tick log — append the full output for audit
    tick_log_path.parent.mkdir(parents=True, exist_ok=True)
    with tick_log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "tick_utc": now_utc_iso,
            "output": output,
            "admitted": admitted,
        }, ensure_ascii=False) + "\n")

    # 6. Pending questions → pending_question queue. Each becomes a row the
    # chat-reply injector (pending_questions/injector.py) consults at
    # prompt time. Topic tag, priority, and expiration are derived from the
    # related concern when one is named — otherwise reasonable defaults.
    # Every open concern, including those just admitted, so a question linked to a just-raised
    # concern (its label already mapped to the real id) inherits its tags + severity.
    concern_lookup: Dict[str, Dict[str, Any]] = {
        c["concern_id"]: c for b in ("active", "addressing") for c in register.get(b) or []}
    questions_enqueued = _enqueue_pending_questions(pending_questions, concern_lookup)

    return {
        "new_concerns_count": len(new_concerns),
        "concerns_created": sum(1 for st in plan if st["action"] == "create"),
        "concerns_merged": sum(1 for st in plan if st["action"] in ("merge_open", "merge_candidate")),
        "concerns_suppressed": sum(1 for st in plan if st["action"] == "suppress_closed"),
        "reinforced_count": len(reinforced_concerns),
        "addressing_count": len(addressing_concerns),
        "resolved_count": len(resolved_concerns),
        "escalated_count": len(escalated_concerns),
        "dispositions_count": len(concern_dispositions),
        "question_outcomes_count": outcomes_applied,
        "belief_updates_count": len(belief_updates),
        "pending_questions_count": len(pending_questions),
        "questions_enqueued_count": questions_enqueued,
        "active_total_after": len(register.get("active", [])),
        "tick_log_path": str(tick_log_path),
    }


_HORIZON_TO_EXPIRY_HOURS = {
    "today": 24.0,
    "this_week": 24.0 * 7,
    "this_month": 24.0 * 30,
    "long_horizon": None,
}

_SEVERITY_TO_PRIORITY = {"low": "low", "medium": "medium", "high": "high"}


def _enqueue_pending_questions(
    pending_questions: List[Dict[str, Any]],
    by_id: Dict[str, Dict[str, Any]],
) -> int:
    """Push each noticer-emitted question onto the pending_question queue.

    Mapping:
      - text             → question_text
      - related_concern  → topical_tag = concern's first domain_tag
                           (or 'general'),
                           priority = concern.severity,
                           expires_after_hours = horizon-derived
      - why_asking + if_unanswered → folded into question_text as
        "(if unanswered: <default>)" tail so the user sees the noticer's
        assumed default; why_asking goes in the queue's created_by-side
        for audit only.

    Failures are logged but do not propagate — persist must complete
    even if the queue is unavailable.
    """
    if not pending_questions:
        return 0
    try:
        from app.assistant.pending_questions import enqueue_question
    except Exception as exc:
        logger.warning(
            "[noticer.persist] pending_questions import failed; questions "
            "logged to tick log only: %s", exc,
        )
        return 0

    enqueued = 0
    for q in pending_questions:
        text = (q.get("text") or "").strip()
        if not text:
            continue
        related_id = q.get("related_concern_id")
        concern = by_id.get(related_id) if related_id else None
        topical_tag = "general"
        priority = "medium"
        expires_after_hours: Optional[float] = 72.0
        if concern is not None:
            tags = concern.get("domain_tags") or []
            if tags:
                topical_tag = str(tags[0]).strip().lower() or "general"
            sev = (concern.get("severity") or "").lower()
            priority = _SEVERITY_TO_PRIORITY.get(sev, "medium")
            horizon = (concern.get("horizon") or "").lower()
            if horizon in _HORIZON_TO_EXPIRY_HOURS:
                expires_after_hours = _HORIZON_TO_EXPIRY_HOURS[horizon]

        if_unanswered = (q.get("if_unanswered") or "").strip()
        question_text = text
        if if_unanswered:
            question_text = f"{text} (if no reply, I'll go with: {if_unanswered})"

        # High-stakes questions earn a blocking ticket; everything else is
        # woven naturally into chat (the magical default).
        ask_mode = "chat"
        if concern is not None:
            sev = (concern.get("severity") or "").lower()
            horizon = (concern.get("horizon") or "").lower()
            if sev == "high" and horizon in ("today", "this_week"):
                ask_mode = "ticket"

        qid = enqueue_question(
            question_text=question_text,
            topical_tag=topical_tag,
            priority=priority,
            created_by="subconscious::noticer",
            expires_after_hours=expires_after_hours,
            related_concern_id=related_id,
            ask_mode=ask_mode,
        )
        if qid:
            enqueued += 1
            if ask_mode == "ticket":
                # High-stakes: deliver as the modal immediately (perfect
                # answer attribution). On failure it stays pending and the
                # chat injector delivers it on the normal path.
                from app.assistant.pending_questions.ticket_delivery import (
                    deliver_question_as_ticket,
                )
                deliver_question_as_ticket(
                    question_id=qid,
                    question_text=question_text,
                    priority=priority,
                )
    if enqueued:
        logger.info(
            "[noticer.persist] enqueued %d of %d pending questions",
            enqueued, len(pending_questions),
        )
    return enqueued


def _load_register(connect=None) -> Dict[str, Any]:
    """The whole register from the concerns table. An unreadable row raises: the spine
    deserves fail-loud, never a fresh register the next save would write over every concern."""
    return concern_store.load_register(connect=connect)


def _save_register(connect, register: Dict[str, Any]) -> None:
    """The whole register back, in one transaction."""
    concern_store.save_register(register, connect=connect)


def annotate_concern_answer(concern_id: str, *, question_text: str, answer_text: str, connect=None) -> bool:
    """Journal a captured answer onto its concern immediately — the noticer
    formally processes it on its (triggered) next tick. Lives here with the
    other register writers: one lock, one transaction. Returns False when the
    concern isn't active or addressing; raises on an unreadable register."""
    with _REGISTER_LOCK:
        register = _load_register(connect)
        now_iso = datetime.now(timezone.utc).isoformat()
        for bucket in ("active", "addressing"):
            for c in register.get(bucket) or []:
                if c.get("concern_id") == concern_id:
                    c["reinforcement_notes"] = (
                        (c.get("reinforcement_notes") or "")
                        + f"\n[{now_iso}] USER ANSWERED ({question_text[:80]}): {answer_text[:200]}"
                    )
                    _save_register(connect, register)
                    return True
    return False


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
                        reopened.append({"concern_id": concern["concern_id"],
                                         "work": max(ended, key=lambda w: str(w["ended"]["at"]))})
                changed += 1
        if changed:
            register["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
            _save_register(connect, register)
            logger.info("[persist] attached work derived for %d concern(s); %d back to active",
                        changed, len(reopened))
        return reopened


_SEVERITY_LADDER = ["low", "medium", "high"]


def _bump_severity(current: Optional[str], *, up: bool) -> str:
    if current not in _SEVERITY_LADDER:
        return current or "medium"
    idx = _SEVERITY_LADDER.index(current)
    if up:
        return _SEVERITY_LADDER[min(idx + 1, len(_SEVERITY_LADDER) - 1)]
    return _SEVERITY_LADDER[max(idx - 1, 0)]
