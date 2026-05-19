"""Persist a noticer tick's output to disk.

v0 storage:
- resource_concerns_register.json — updated in place (active/addressing/resolved/dormant)
- resource_subconscious_tick_log.jsonl — one line per tick: full AgentForm dump
  for audit + later replay

Belief updates and pending_questions are recorded in the tick log only.
In v0.5 these get persisted into the real belief_store and a question
queue the chat brain can read.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_REGISTER_REL = "resources/subconscious/resource_concerns_register.json"
_TICK_LOG_REL = "resources/subconscious/resource_subconscious_tick_log.jsonl"


def apply_noticer_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Apply one noticer tick to the concerns_register on disk.

    Accepts the raw dict shape (what JSON-roundtrip of AgentForm produces).
    Returns a small summary dict for the runner to print.
    """
    register_path = get_repo_root() / _REGISTER_REL
    tick_log_path = get_repo_root() / _TICK_LOG_REL

    register = _load_register(register_path)
    now_utc_iso = datetime.now(timezone.utc).isoformat()

    new_concerns = output.get("new_concerns") or []
    reinforced_concerns = output.get("reinforced_concerns") or []
    resolved_concerns = output.get("resolved_concerns") or []
    escalated_concerns = output.get("escalated_concerns") or []
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

    # 2. Reinforcements → update in-place
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
        notes = r.get("notes")
        if notes:
            existing["reinforcement_notes"] = (existing.get("reinforcement_notes") or "") + f"\n[{now_utc_iso}] {notes}"

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

    return {
        "new_concerns_count": len(new_concerns),
        "reinforced_count": len(reinforced_concerns),
        "resolved_count": len(resolved_concerns),
        "escalated_count": len(escalated_concerns),
        "belief_updates_count": len(belief_updates),
        "pending_questions_count": len(pending_questions),
        "active_total_after": len(register.get("active", [])),
        "register_path": str(register_path),
        "tick_log_path": str(tick_log_path),
    }


def _load_register(path: Path) -> Dict[str, Any]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("[noticer.persist] register parse failed; starting fresh: %s", e)
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
    path.write_text(json.dumps(register, indent=2, ensure_ascii=False), encoding="utf-8")


_SEVERITY_LADDER = ["low", "medium", "high"]


def _bump_severity(current: Optional[str], *, up: bool) -> str:
    if current not in _SEVERITY_LADDER:
        return current or "medium"
    idx = _SEVERITY_LADDER.index(current)
    if up:
        return _SEVERITY_LADDER[min(idx + 1, len(_SEVERITY_LADDER) - 1)]
    return _SEVERITY_LADDER[max(idx - 1, 0)]
