"""Persist a noticer tick's output to disk.

v0 storage:
- resource_concerns_register.json — updated in place (active/addressing/resolved/dormant)
- resource_subconscious_tick_log.jsonl — one line per tick: full AgentForm dump
  for audit + later replay

Belief updates are still recorded in the tick log only.
Pending questions are persisted into the pending_question queue
(app/assistant/database/pending_question.py); the chat-reply injector
in EmiResultHandler picks them up and appends them to outbound replies.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_REGISTER_REL = "resources/subconscious/resource_concerns_register.json"
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
# stale — the handoff likely dropped; the noticer must re-escalate or
# resolve it.
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
        since_raw = str(c.get("addressing_since_utc") or "").strip()
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
    register_path: Optional[Path] = None,
    tick_log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Apply one noticer tick to the concerns_register on disk.

    Accepts the raw dict shape (what JSON-roundtrip of AgentForm produces).
    Returns a small summary dict for the runner to print. `register_path`/`tick_log_path`
    default to the real resource files; tests pass temp paths to avoid touching them.

    Holds the register lock for the whole read-modify-write so a concurrent
    answer-capture journal write can't be lost.
    """
    with _REGISTER_LOCK:
        return _apply_noticer_output_locked(
            output, register_path=register_path, tick_log_path=tick_log_path,
        )


def _apply_noticer_output_locked(
    output: Dict[str, Any],
    *,
    register_path: Optional[Path] = None,
    tick_log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    register_path = register_path or (get_repo_root() / _REGISTER_REL)
    tick_log_path = tick_log_path or (get_repo_root() / _TICK_LOG_REL)

    register = _load_register(register_path)
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

    # 1. New concerns → active
    for c in new_concerns:
        # Best-effort: skip if same concern_id already exists (idempotent reruns)
        cid = c.get("concern_id")
        if cid and cid in by_id:
            logger.info("[noticer.persist] new_concern %s already exists; skipping", cid)
            continue
        register.setdefault("active", []).append(c)

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
    # public outcomes; dayflow never writes the register.
    for a in addressing_concerns:
        cid = a.get("concern_id")
        if not cid or cid not in by_id:
            logger.warning("[noticer.persist] addressing targets unknown concern %s", cid)
            continue
        existing = by_id[cid]
        existing["addressing_since_utc"] = now_utc_iso
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
            existing["escalation"] = {
                "target": "dayflow_orchestrator",
                "urgency": "high",
                "reason": reason or "re-escalated after stalled handling",
                "escalated_at_utc": now_utc_iso,
            }
            existing["last_disposition_at_count"] = int(existing.get("reinforcement_count") or 0)
        elif action == "keep_active":
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
    _save_register(register_path, register)

    # 5. Tick log — append the full output for audit
    tick_log_path.parent.mkdir(parents=True, exist_ok=True)
    with tick_log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "tick_utc": now_utc_iso,
            "output": output,
        }, ensure_ascii=False) + "\n")

    # 6. Pending questions → pending_question queue. Each becomes a row the
    # chat-reply injector (EmiResultHandler) consults next time Emi sends
    # a reply. Topic tag, priority, and expiration are derived from the
    # related concern when one is named — otherwise reasonable defaults.
    # Include new_concerns in the lookup so a question linked to a
    # just-minted concern still inherits its tags + severity.
    concern_lookup: Dict[str, Dict[str, Any]] = dict(by_id)
    for c in new_concerns:
        cid = c.get("concern_id")
        if cid:
            concern_lookup[cid] = c
    questions_enqueued = _enqueue_pending_questions(pending_questions, concern_lookup)

    return {
        "new_concerns_count": len(new_concerns),
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
        "register_path": str(register_path),
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


def _load_register(path: Path) -> Dict[str, Any]:
    """Load the register; a MISSING file bootstraps empty, a CORRUPT file
    raises. The old behavior (parse failure → fresh register) meant the next
    save silently destroyed every concern — the spine deserves fail-loud, and
    the atomic save below makes corruption a should-never state."""
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "last_updated_utc": None,
        "last_noticer_tick_utc": None,
        "active": [],
        "addressing": [],
        "resolved": [],
        "dormant": [],
    }


def _save_register(path: Path, register: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, register)


def annotate_concern_answer(concern_id: str, *, question_text: str, answer_text: str) -> bool:
    """Journal a captured answer onto its concern immediately — the noticer
    formally processes it on its (triggered) next tick. Lives here with the
    other register writers: one lock, one atomic save. Returns False when the
    register or the concern doesn't exist; raises on a corrupt register."""
    path = get_repo_root() / _REGISTER_REL
    with _REGISTER_LOCK:
        if not path.is_file():
            return False
        register = _load_register(path)
        now_iso = datetime.now(timezone.utc).isoformat()
        for bucket in ("active", "addressing"):
            for c in register.get(bucket) or []:
                if c.get("concern_id") == concern_id:
                    c["reinforcement_notes"] = (
                        (c.get("reinforcement_notes") or "")
                        + f"\n[{now_iso}] USER ANSWERED ({question_text[:80]}): {answer_text[:200]}"
                    )
                    _save_register(path, register)
                    return True
    return False


_SEVERITY_LADDER = ["low", "medium", "high"]


def _bump_severity(current: Optional[str], *, up: bool) -> str:
    if current not in _SEVERITY_LADDER:
        return current or "medium"
    idx = _SEVERITY_LADDER.index(current)
    if up:
        return _SEVERITY_LADDER[min(idx + 1, len(_SEVERITY_LADDER) - 1)]
    return _SEVERITY_LADDER[max(idx - 1, 0)]
