"""Read-only data layer for the /subconscious dashboard's "mind" sections.

The page used to show only the proposers' OUTPUT (intention.* pods). These
helpers surface the subconscious itself: the concerns register with its
lifecycle, the issues the noticer raised lately and what became of them, and the latest digest.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.subconscious.digest_builder import load_digest_state
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_DIGEST_DIR_REL = "app/subconscious_digests"

_EVIDENCE_CAP = 8
_REINFORCEMENT_NOTE_TAIL = 4
_RESOLVED_CAP = 8
_NOTICER_ISSUES_CAP = 12


def _slim_concern(c: Dict[str, Any]) -> Dict[str, Any]:
    evidence = c.get("evidence") or []
    notes_raw = str(c.get("reinforcement_notes") or "").strip()
    # reinforcement_notes is an append-only "\n[ts] text" journal — show the tail.
    note_lines = [ln.strip() for ln in notes_raw.splitlines() if ln.strip()]
    return {
        "concern_id": c.get("concern_id"),
        "title": c.get("title") or "",
        "severity": str(c.get("severity") or "low"),
        "kind": c.get("kind") or "",
        "subject": c.get("subject") or "",
        "horizon": c.get("horizon") or "",
        "domain_tags": list(c.get("domain_tags") or []),
        "addressable_by": list(c.get("addressable_by") or []),
        "first_observed": str(c.get("first_observed") or "")[:10],
        "last_reinforced": str(c.get("last_reinforced_utc") or "")[:10],
        "evidence_count": len(evidence),
        "evidence": [
            {"kind": e.get("kind") or "", "snippet": str(e.get("snippet") or "")[:240]}
            for e in evidence[:_EVIDENCE_CAP]
            if isinstance(e, dict)
        ],
        "reinforcement_tail": note_lines[-_REINFORCEMENT_NOTE_TAIL:],
        "notes": str(c.get("notes") or "")[:400],
    }


def load_mind_overview() -> Dict[str, Any]:
    """Everything the dashboard's mind sections need, in one dict."""
    from app.assistant.subconscious.concern_store import load_register
    register: Dict[str, Any] = {}
    try:
        register = load_register()
    except Exception as e:
        logger.error("[dashboard] failed reading concerns register: %s", e)

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for bucket in ("active", "addressing"):
        items = [c for c in (register.get(bucket) or []) if isinstance(c, dict)]
        slim = [_slim_concern(c) for c in items]
        sev_rank = {"high": 0, "medium": 1, "low": 2}
        slim.sort(key=lambda c: (sev_rank.get(c["severity"], 3), c["title"]))
        buckets[bucket] = slim
    resolved = [c for c in (register.get("resolved") or []) if isinstance(c, dict)]
    buckets["resolved"] = [_slim_concern(c) for c in resolved[-_RESOLVED_CAP:]][::-1]

    issues = _load_noticer_issues()
    digest = _load_latest_digest()

    return {
        "last_noticer_utc": issues[0]["at"] if issues else "",
        "counts": {
            "active": len(register.get("active") or []),
            "addressing": len(register.get("addressing") or []),
            "resolved": len(register.get("resolved") or []),
            "dormant": len(register.get("dormant") or []),
        },
        "concerns": buckets,
        "noticer_issues": issues,
        "digest": digest,
    }


def _load_noticer_issues() -> List[Dict[str, Any]]:
    """The issues the noticer raised lately (brain events of source `noticer`), newest first, each with the gate's
    route and the brain's decision. Until 2026-10-09 this read the noticer's tick log, which it no longer writes."""
    from app.assistant.subconscious.db import connect
    with connect(False) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT occurred_at, text, gate_status, route FROM brain_events WHERE source = 'noticer' "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?", (_NOTICER_ISSUES_CAP,))]
    return [{"at": str(r["occurred_at"])[:16].replace("T", " "), "title": r["text"].split("\n", 1)[0],
             "route": r["route"] or r["gate_status"]} for r in rows]


def _load_latest_digest() -> Dict[str, Any]:
    state = load_digest_state()
    digest_dir = get_repo_root() / _DIGEST_DIR_REL
    latest_text = ""
    latest_name = ""
    if digest_dir.is_dir():
        files = sorted(digest_dir.glob("digest_*.md"))
        if files:
            latest = files[-1]
            latest_name = latest.name
            try:
                latest_text = latest.read_text(encoding="utf-8")
            except Exception as e:
                logger.error("[dashboard] failed reading digest %s: %s", latest, e)
    return {
        "last_digest_at": str(state.get("last_digest_at_utc") or "")[:16].replace("T", " "),
        "history_count": state.get("history_count") or 0,
        "latest_name": latest_name,
        "latest_text": latest_text,
    }
