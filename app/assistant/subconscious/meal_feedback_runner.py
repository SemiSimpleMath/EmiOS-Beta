"""Meal-feedback loop — proactively ASK how meals went, then INGEST the answers.

Two halves, one pass (shared by the routine + the CLI):

1. PRODUCE: for each recent past meal (intention.meal pod dated yesterday / the
   day before, not already asked about), enqueue a pending_question
   ("How was <dish>?"). The conversation_starter bridge surfaces it proactively.
   Idempotent: the meal pod is stamped metadata.feedback_asked_at_utc, and the
   question_id -> meal_pod mapping is kept in a small state file.

2. INGEST: for each asked meal question, find the user's reply (the first user
   message in master_room within ANSWER_WINDOW_HOURS of when the bridge asked it)
   and mint a feedback.comment pod targeting that meal pod — exactly as a /meals
   comment would. feedback_extractor then turns it into beliefs. This is the
   answer-ingestion "harder half" that closes the loop: ask -> answer -> belief.

State: resources/subconscious/resource_meal_feedback_state.json (gitignored).
Fails LOUD on a corrupt state file (silently resetting would re-ask every meal).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_STATE_REL = "resources/subconscious/resource_meal_feedback_state.json"
ANSWER_WINDOW_HOURS = 6      # how long after asking we'll attribute a reply as the answer
MAX_NEW_PER_PASS = 4        # don't flood the queue in one pass
PAST_DAYS = 2               # ask about meals from yesterday + the day before
DEDUP_DAYS = 5              # don't re-ask about the same dish (incl. its leftovers) within this window

# Cooking-method / serving / qualifier words to strip when comparing dishes, so
# "Baked salmon with broccoli and potatoes" and "Leftover oven-baked salmon w/
# broccoli and potatoes (reheated)" both reduce to {salmon, broccoli, potatoes}.
# Ingredient words are the signal and are kept (NOT listed here).
_DISH_STOP = {
    "with", "and", "the", "for", "over", "plus", "side", "served",
    "leftover", "leftovers", "friendly", "reheated", "packed", "packable", "flex",
    "nocook", "cook", "made", "stirred", "topped", "optional", "available", "fresh",
    "baked", "ovenbaked", "oven", "roasted", "warm", "simple", "quick", "easy",
    "low", "effort", "loweffort", "sliced", "stovetop", "light", "homemade",
}


def run_meal_feedback_pass(*, dry_run: bool = False) -> Dict[str, Any]:
    """One pass: ingest answers to already-asked meal questions, then enqueue new
    questions for recent past meals. Returns a summary. Ingest runs first so a
    reply is captured before the question can expire."""
    now_utc = datetime.now(timezone.utc)
    state = _load_state()
    summary: Dict[str, Any] = {"asked": 0, "ingested": 0, "dropped": 0, "dry_run": dry_run}

    _ingest(state, now_utc, summary, dry_run=dry_run)
    _produce(state, now_utc, summary, dry_run=dry_run)

    if not dry_run:
        state["last_run_utc"] = now_utc.isoformat()
        _save_state(state)
    return summary


# ── PRODUCE ────────────────────────────────────────────────────────────────

def _produce(state: Dict[str, Any], now_utc: datetime, summary: Dict[str, Any], *, dry_run: bool) -> None:
    from app.assistant.pod_store.pod_store import PodStore
    from app.assistant.pending_questions.store import enqueue_question

    store = PodStore()
    target_dates = {(now_utc.date() - timedelta(days=d)).isoformat() for d in range(1, PAST_DAYS + 1)}
    try:
        pods = store.query(kind="intention.meal", since_utc=now_utc - timedelta(days=PAST_DAYS + 2), limit=200)
    except Exception as e:
        logger.warning("[meal_feedback] intention.meal fetch failed: %s", e)
        return

    asked = 0
    deduped = 0
    for pod in pods:
        if asked >= MAX_NEW_PER_PASS:
            break
        meta = pod.metadata or {}
        if meta.get("feedback_asked_at_utc"):
            continue  # already asked about this meal
        if (meta.get("date") or "") not in target_dates:
            continue
        toks = _dish_tokens(meta.get("dish") or "")
        if _dish_covered_recently(state, toks, now_utc):
            # Same dish (e.g. last night's salmon now showing up as leftover
            # lunch) was already asked/answered recently — don't re-ask. Stamp
            # the pod so it isn't re-evaluated every pass; do NOT enqueue.
            if not dry_run:
                meta["feedback_asked_at_utc"] = now_utc.isoformat()
                meta["feedback_skip_reason"] = "duplicate_dish"
                pod.metadata = meta
                store.put(pod)
            deduped += 1
            continue
        if dry_run:
            _mark_dish_covered(state, toks, now_utc)  # within-pass dedup for the preview
            asked += 1
            continue
        qid = enqueue_question(
            question_text=_question_text(meta),
            topical_tag="meal_feedback",
            priority="medium",
            created_by="meal_feedback_producer",
            expires_after_hours=48.0,
        )
        if not qid:
            continue
        # Stamp the pod (durable idempotency) + record the q->meal mapping (for ingest).
        meta["feedback_asked_at_utc"] = now_utc.isoformat()
        meta["feedback_question_id"] = qid
        pod.metadata = meta
        store.put(pod)
        state.setdefault("active", {})[qid] = {
            "meal_pod_id": pod.pod_id,
            "dish": meta.get("dish") or "",
            "date": meta.get("date") or "",
            "meal_window": meta.get("meal_window") or "",
            "enqueued_at_utc": now_utc.isoformat(),
        }
        # Asking covers the dish: leftovers / re-cooks within DEDUP_DAYS won't re-ask.
        _mark_dish_covered(state, toks, now_utc)
        asked += 1
    summary["asked"] = asked
    summary["deduped"] = deduped


# ── dish matching (dedup salmon-dinner vs salmon-leftover-lunch, etc.) ─────────

def _dish_tokens(dish: str) -> frozenset:
    """Core ingredient tokens of a dish name — parentheticals, leftover/serving
    qualifiers and cooking-method words stripped. So 'Baked salmon with broccoli
    and potatoes' and 'Leftover oven-baked salmon w/ broccoli and potatoes
    (reheated)' both reduce to {salmon, broccoli, potatoes}."""
    import re
    s = re.sub(r"\([^)]*\)", " ", (dish or "").lower())
    return frozenset(t for t in re.split(r"[^a-z]+", s) if len(t) >= 3 and t not in _DISH_STOP)


def _same_dish(a, b) -> bool:
    """True when two dish token-sets share their core ingredients."""
    a, b = frozenset(a), frozenset(b)
    if len(a) < 2 or len(b) < 2:
        return bool(a) and a == b   # too few tokens to fuzzy-match — require exact
    inter = len(a & b)
    return inter >= 2 and inter / len(a | b) >= 0.5


def _dish_covered_recently(state: Dict[str, Any], toks, now_utc: datetime) -> bool:
    """Has this dish been asked-about OR answered within DEDUP_DAYS? Prunes the
    covered-dish history in place as a side effect."""
    cutoff = (now_utc - timedelta(days=DEDUP_DAYS)).isoformat()
    covered = [c for c in state.get("covered_dishes", []) if (c.get("at") or "") >= cutoff]
    state["covered_dishes"] = covered
    if not toks:
        return False
    return any(_same_dish(toks, c.get("tokens") or []) for c in covered)


def _mark_dish_covered(state: Dict[str, Any], toks, now_utc: datetime) -> None:
    if not toks:
        return
    state.setdefault("covered_dishes", []).append(
        {"tokens": sorted(toks), "at": now_utc.isoformat()}
    )


def _cancel_sibling_dishes(active: Dict[str, Any], answered_toks, *, skip_qid, mark_dismissed) -> int:
    """Dismiss every still-queued meal question about the same dish as the one
    just answered. Returns the count cancelled. Mutates ``active`` in place."""
    if not answered_toks:
        return 0
    cancelled = 0
    for other_qid in list(active.keys()):
        if other_qid == skip_qid:
            continue
        if _same_dish(answered_toks, _dish_tokens(active[other_qid].get("dish") or "")):
            mark_dismissed(other_qid, reason="superseded_by_sibling_answer")
            del active[other_qid]
            cancelled += 1
    return cancelled


def _question_text(meta: Dict[str, Any]) -> str:
    dish = meta.get("dish") or "the meal"
    window = meta.get("meal_window") or "meal"
    date = meta.get("date") or ""
    when = f"{window} on {date}" if date else window
    return f"How was {dish} ({when})? Did everyone like it, and anything to change for next time?"


# ── INGEST ─────────────────────────────────────────────────────────────────

def _ingest(state: Dict[str, Any], now_utc: datetime, summary: Dict[str, Any], *, dry_run: bool) -> None:
    from app.assistant.subconscious.feedback_service import mint_feedback_comment
    from app.assistant.pending_questions.store import mark_dismissed

    active: Dict[str, Any] = state.get("active") or {}
    ingested = 0
    dropped = 0
    for qid in list(active.keys()):
        if qid not in active:
            continue                  # already cancelled as a sibling below
        info = active[qid]
        status, asked_at = _question_status(qid)
        if status is None or status in ("dismissed", "expired"):
            del active[qid]          # gone from the queue; nothing to capture
            dropped += 1
            continue
        if status == "pending" or asked_at is None:
            continue                  # not surfaced by the bridge yet — keep waiting

        until = asked_at + timedelta(hours=ANSWER_WINDOW_HOURS)
        reply = _first_user_reply_after(asked_at, min(until, now_utc))
        if reply:
            if not dry_run:
                mint_feedback_comment(
                    target_pod_id=info["meal_pod_id"],
                    text=reply,
                    actor="user",
                )
                mark_dismissed(qid, reason="answered_via_chat")
                del active[qid]
                # Answering one meal question makes its same-dish siblings moot
                # (e.g. you commented on the salmon dinner; the queued
                # salmon-leftover-lunch questions would just look dumb). Cancel
                # them and cover the dish so nothing new gets minted for it.
                answered_toks = _dish_tokens(info.get("dish") or "")
                _mark_dish_covered(state, answered_toks, now_utc)
                dropped += _cancel_sibling_dishes(
                    active, answered_toks, skip_qid=qid, mark_dismissed=mark_dismissed
                )
            ingested += 1
        elif now_utc > until:
            # Asked, window passed, no reply — give up so it doesn't linger.
            if not dry_run:
                mark_dismissed(qid, reason="unanswered_window_passed")
                del active[qid]
            dropped += 1
        # else: still within the window, no reply yet — keep for the next pass.

    state["active"] = active
    summary["ingested"] = ingested
    summary["dropped"] = dropped


def _question_status(qid: str):
    """Return (status, asked_at) for a pending question id, or (None, None)."""
    from app.assistant.database.pending_question import PendingQuestion
    from app.models.base import get_session
    session = get_session()
    try:
        row = session.query(PendingQuestion).filter(PendingQuestion.id == qid).first()
        if row is None:
            return None, None
        return row.status, row.asked_at
    finally:
        session.close()


def _first_user_reply_after(asked_at: datetime, until: datetime) -> Optional[str]:
    """First non-empty user message in master_room within (asked_at, until].
    A reply right after the assistant's proactive 'how was dinner?' is almost
    certainly the answer; feedback_extractor filters quality downstream."""
    if until <= asked_at:
        return None
    try:
        from sqlalchemy import select, and_
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session
        session = get_session()
        try:
            stmt = (
                select(UnifiedLog2026)
                .where(and_(
                    UnifiedLog2026.role == "user",
                    UnifiedLog2026.timestamp > asked_at,
                    UnifiedLog2026.timestamp <= until,
                ))
                .order_by(UnifiedLog2026.timestamp.asc())
                .limit(20)
            )
            rows = session.execute(stmt).scalars().all()
        finally:
            session.close()
    except Exception as e:
        logger.warning("[meal_feedback] reply fetch failed: %s", e)
        return None

    for r in rows:
        room = getattr(r, "room_id", None)
        if room not in (None, "", "master_room"):
            continue
        msg = (getattr(r, "message", "") or "").strip()
        if msg:
            return msg[:1000]
    return None


# ── state ────────────────────────────────────────────────────────────────────

def _load_state() -> Dict[str, Any]:
    path = get_repo_root() / _STATE_REL
    if not path.is_file():
        return {"schema_version": 1, "last_run_utc": None, "active": {}}
    # Fail LOUD on corruption — silently resetting would re-ask about every meal.
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(state: Dict[str, Any]) -> None:
    path = get_repo_root() / _STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
