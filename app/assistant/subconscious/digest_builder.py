"""Render the subconscious's concerns_register into a chat-ready digest.

Pure Python — no LLM call. The concerns_register is already structured;
this just templates it into a terse markdown message the chat brain (or a
log file) can surface.

What goes in:
- NEW concerns       — active concerns the user hasn't seen yet (not in
                       prior digests). Sorted by severity desc.
- ONGOING concerns   — active concerns previously surfaced; mentioned
                       briefly so the user knows they're still tracked.
- RESOLVED recently  — concerns resolved since last digest. One-line each.
- PENDING questions  — up to 2 user-facing questions from the last tick.

What deliberately stays out:
- Belief updates (internal calibration data, not user-facing)
- Dormant concerns (already-faded patterns)
- Full evidence lists (too noisy)
- Reasoning/notes blow-by-blow (the title + 1-2 sentences is enough)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time


_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def render_digest(
    *,
    register: Dict[str, Any],
    previously_surfaced_ids: Set[str],
    pending_questions: Optional[List[Dict[str, Any]]] = None,
    digest_date_local: Optional[str] = None,
) -> str:
    """Build the markdown digest text.

    `register` is the concerns_register dict (active/addressing/resolved/dormant).
    `previously_surfaced_ids` are concern_ids that appeared in past digests —
    used to distinguish NEW from ONGOING.
    `pending_questions` come from the most recent noticer tick (not stored
    long-term in the register).
    """
    if digest_date_local is None:
        # %a %b %d is cross-platform; strip leading zero on day for readability
        digest_date_local = get_local_time().strftime("%a %b %d").replace(" 0", " ")

    active = list(register.get("active") or [])
    addressing = list(register.get("addressing") or [])
    resolved = list(register.get("resolved") or [])

    # Split active into new vs ongoing
    new_concerns: List[Dict[str, Any]] = []
    ongoing_concerns: List[Dict[str, Any]] = []
    for c in active + addressing:
        cid = c.get("concern_id")
        if not cid:
            continue
        if cid in previously_surfaced_ids:
            ongoing_concerns.append(c)
        else:
            new_concerns.append(c)

    new_concerns.sort(key=lambda c: (_SEVERITY_ORDER.get(c.get("severity"), 9), c.get("first_observed") or ""))
    ongoing_concerns.sort(key=lambda c: (_SEVERITY_ORDER.get(c.get("severity"), 9), c.get("first_observed") or ""))

    # Resolved-recently: anything in resolved bucket — caller controls window
    # by passing a pre-filtered register if needed. For v0, show up to 5
    # most-recent resolutions.
    resolved_recent = sorted(
        resolved,
        key=lambda c: (c.get("resolved_at_utc") or ""),
        reverse=True,
    )[:5]

    # ─── render ─────────────────────────────────────────────────────────

    lines: List[str] = []
    lines.append(f"🧠 **What I've been noticing — {digest_date_local}**")
    lines.append("")

    if not new_concerns and not ongoing_concerns and not resolved_recent:
        lines.append("Quiet day. Nothing new to flag.")
        # A quiet day is still a natural moment to ask — and the runner marks
        # rendered questions asked (anchored to the digest row), so they must
        # actually appear whenever they're loaded.
        _append_pending_questions(lines, pending_questions)
        return "\n".join(lines)

    if new_concerns:
        lines.append(f"**New this round** ({len(new_concerns)}):")
        for c in new_concerns:
            lines.extend(_render_concern_block(c, mark_new=True))
        lines.append("")

    if ongoing_concerns:
        lines.append(f"**Still tracking** ({len(ongoing_concerns)}):")
        for c in ongoing_concerns:
            lines.append(_render_concern_oneliner(c))
        lines.append("")

    if resolved_recent:
        lines.append(f"**Resolved** ({len(resolved_recent)}):")
        for c in resolved_recent:
            reason = (c.get("resolution_reason") or "").strip()
            title = c.get("title", "(untitled)")
            lines.append(f"- {title}" + (f" — {reason}" if reason else ""))
        lines.append("")

    _append_pending_questions(lines, pending_questions)

    # Footer with stats
    total_active = len(active) + len(addressing)
    lines.append(f"_{total_active} concern{'s' if total_active != 1 else ''} active in total._")

    return "\n".join(lines)


def _append_pending_questions(lines: List[str], pending_questions: Optional[List[Dict[str, Any]]]) -> None:
    """Render up to 2 open questions (the noticer's own cap). Shared by the
    normal and quiet-day paths so the runner's ask-anchoring (mark-asked with
    the digest row id) always matches what the user actually saw."""
    if not pending_questions:
        return
    for q in pending_questions[:2]:
        lines.append(f"**Question for when you have a moment:** {q.get('text', '')}")
        why = (q.get("why_asking") or "").strip()
        if why:
            lines.append(f"  _(why: {why})_")
    lines.append("")


def _render_concern_block(c: Dict[str, Any], *, mark_new: bool) -> List[str]:
    """Multi-line block for a freshly surfaced concern."""
    sev = c.get("severity", "?")
    emoji = _SEVERITY_EMOJI.get(sev, "•")
    title = c.get("title", "(untitled)")
    subject = c.get("subject") or "household"
    kind = c.get("kind", "")
    horizon = c.get("horizon", "")

    head = f"- {emoji} **{title}** _(subject: {subject} · {kind} · {horizon})_"

    # First sentence of notes — keep it tight
    notes = (c.get("notes") or "").strip()
    note_preview = notes.split(". ")[0].strip()
    if note_preview and not note_preview.endswith("."):
        note_preview += "."

    routed = ", ".join(c.get("addressable_by") or []) or "(no routing)"
    block = [head]
    if note_preview:
        block.append(f"  {note_preview}")
    block.append(f"  _Flagged for: {routed}_")
    return block


def _render_concern_oneliner(c: Dict[str, Any]) -> str:
    """One-line summary for an ongoing concern (already seen)."""
    sev = c.get("severity", "?")
    emoji = _SEVERITY_EMOJI.get(sev, "•")
    title = c.get("title", "(untitled)")
    return f"- {emoji} {title} _(still active)_"


# ─── state tracking helpers ────────────────────────────────────────────

_DIGEST_STATE_REL = "resources/subconscious/resource_digest_state.json"


def load_digest_state() -> Dict[str, Any]:
    path = get_repo_root() / _DIGEST_STATE_REL
    if not path.is_file():
        return {
            "last_digest_at_utc": None,
            "previously_surfaced_concern_ids": [],
            "history_count": 0,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "last_digest_at_utc": None,
            "previously_surfaced_concern_ids": [],
            "history_count": 0,
        }


def save_digest_state(state: Dict[str, Any]) -> None:
    path = get_repo_root() / _DIGEST_STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
