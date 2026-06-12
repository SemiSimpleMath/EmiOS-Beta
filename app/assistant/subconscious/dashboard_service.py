"""Read-only data layer for the /subconscious dashboard's "mind" sections.

The page used to show only the proposers' OUTPUT (intention.* pods). These
helpers surface the subconscious itself: the concerns register with its
lifecycle, recent noticer tick activity, and the latest digest.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.assistant.subconscious.digest_builder import load_digest_state
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_REGISTER_REL = "resources/subconscious/resource_concerns_register.json"
_TICK_LOG_REL = "resources/subconscious/resource_subconscious_tick_log.jsonl"
_DIGEST_DIR_REL = "app/subconscious_digests"

_EVIDENCE_CAP = 8
_REINFORCEMENT_NOTE_TAIL = 4
_RESOLVED_CAP = 8
_TICKS_CAP = 7


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
    register_path = get_repo_root() / _REGISTER_REL
    register: Dict[str, Any] = {}
    if register_path.is_file():
        try:
            register = json.loads(register_path.read_text(encoding="utf-8"))
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

    ticks = _load_recent_ticks()
    digest = _load_latest_digest()

    return {
        "last_tick_utc": str(register.get("last_noticer_tick_utc") or "")[:16].replace("T", " "),
        "counts": {
            "active": len(register.get("active") or []),
            "addressing": len(register.get("addressing") or []),
            "resolved": len(register.get("resolved") or []),
            "dormant": len(register.get("dormant") or []),
        },
        "concerns": buckets,
        "ticks": ticks,
        "digest": digest,
    }


def _load_recent_ticks() -> List[Dict[str, Any]]:
    path = get_repo_root() / _TICK_LOG_REL
    if not path.is_file():
        return []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception as e:
        logger.error("[dashboard] failed reading tick log: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-_TICKS_CAP:][::-1]:
        try:
            tick = json.loads(line)
        except Exception:
            continue
        output = tick.get("output") or {}
        new_c = output.get("new_concerns") or []
        out.append({
            "tick_utc": str(tick.get("tick_utc") or "")[:16].replace("T", " "),
            "new": len(new_c),
            "new_titles": [str(c.get("title") or "")[:90] for c in new_c[:3] if isinstance(c, dict)],
            "reinforced": len(output.get("reinforced_concerns") or []),
            "resolved": len(output.get("resolved_concerns") or []),
            "escalated": len(output.get("escalated_concerns") or []),
            "questions": len(output.get("pending_questions") or []),
        })
    return out


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
