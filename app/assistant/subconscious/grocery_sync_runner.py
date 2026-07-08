"""Grocery inventory sync — the single callable pass.

Shared by the daily routine (routine_handlers/subconscious.grocery_sync_run) and the CLI
(run_grocery_sync). Three steps: scan recent user chat for inventory intents
(grocery_intent_scanner agent) -> apply each (grocery_inventory.apply_*) -> apply daily
decay. State in resources/subconscious/resource_grocery_sync_state.json tracks scanned
chat-msg ids so intents aren't re-applied.

Before this was scheduled, inventory never updated from chat and decay never ran, so the
inventory_snapshot fed to the meal planners was permanently stale (see
scratch/MEAL-PLANNING-AUDIT.md).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Set

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.subconscious.grocery_inventory import (
    apply_decay,
    apply_meal_consumption,
    apply_shopping,
    manual_acquire,
    manual_consume,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_SYNC_STATE_REL = "resources/subconscious/resource_grocery_sync_state.json"


def run_grocery_sync_pass(
    *, hours: int = 48, dry_run: bool = False, scan_all: bool = False
) -> Dict[str, Any]:
    """Scan recent chat -> apply inventory intents -> apply daily decay.

    Returns a summary dict. Fails LOUD on scanner / apply errors (no silent swallow).
    """
    state = _load_sync_state()
    already_scanned: Set[str] = set() if scan_all else set(state.get("scanned_chat_msg_ids") or [])
    ctx = _build_scanner_context(hours=hours, exclude_msg_ids=already_scanned)

    intents: List[Dict[str, Any]] = []
    skipped_ids: List[str] = []
    recent = ctx.get("recent_user_messages") or ""
    if recent and not recent.startswith("(no"):
        agent = DI.agent_factory.create_agent("grocery_intent_scanner")
        if agent is None:
            raise RuntimeError("grocery_sync: grocery_intent_scanner unavailable")
        scope = load_scope_for_source(
            kind="subsystem", source_id="subconscious", actor_id="grocery_sync",
            identity_overrides={"owner_id": "system", "scope_id": "subconscious::grocery_intent_scanner", "surface": "internal"},
        )
        result = agent.action_handler(Message(agent_input=ctx, scope_context=scope))
        output = result.data if isinstance(getattr(result, "data", None), dict) else {}
        intents = output.get("intents") or []
        skipped_ids = output.get("skipped_chat_msg_ids") or []

    summary: Dict[str, Any] = {
        "scanned_window_hours": hours,
        "intents_detected": len(intents),
        "intents_applied": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    applied = 0
    skipped_low_confidence = 0
    for intent in intents:
        if intent.get("confidence") == "low":
            skipped_low_confidence += 1
            continue
        _apply_intent(intent)
        applied += 1
    summary["intents_applied"] = applied
    summary["intents_skipped_low_confidence"] = skipped_low_confidence
    summary["decay"] = apply_decay()

    # Mark these chat ids scanned so we don't re-apply on the next tick (idempotency).
    new_scanned: Set[str] = set(state.get("scanned_chat_msg_ids") or [])
    new_scanned.update(i.get("chat_msg_id", "") for i in intents if i.get("chat_msg_id"))
    new_scanned.update(skipped_ids)
    state["scanned_chat_msg_ids"] = sorted(new_scanned)[-500:]  # cap state size
    state["last_sync_at_utc"] = datetime.now(timezone.utc).isoformat()
    _save_sync_state(state)
    return summary


# ── scanner context ──────────────────────────────────────────────────────────

def _build_scanner_context(*, hours: int, exclude_msg_ids: Set[str]) -> Dict[str, str]:
    now_utc = datetime.now(timezone.utc)
    return {
        "date_time": now_utc.isoformat(),
        "recent_user_messages": _load_recent_user_messages(now_utc=now_utc, hours=hours, exclude=exclude_msg_ids),
        "active_shopping_intentions": _load_active_intentions(kind="intention.shopping", since=now_utc - timedelta(days=7)),
        "active_meal_intentions": _load_active_intentions(kind="intention.meal", since=now_utc - timedelta(days=4)),
    }


def _load_recent_user_messages(*, now_utc: datetime, hours: int, exclude: Set[str]) -> str:
    """Pull recent role=user rows from unified_log_2026, skip already-scanned ids."""
    try:
        from sqlalchemy import select, and_
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session

        since = now_utc - timedelta(hours=hours)
        session = get_session()
        try:
            stmt = (
                select(UnifiedLog2026)
                .where(and_(UnifiedLog2026.timestamp >= since, UnifiedLog2026.role == "user"))
                .order_by(UnifiedLog2026.timestamp.desc())
                .limit(200)
            )
            rows = session.execute(stmt).scalars().all()
        finally:
            session.close()
    except Exception as e:
        logger.warning("[grocery_sync] chat fetch failed: %s", e)
        return "(no recent user messages)"

    lines: List[str] = []
    for r in rows:
        msg_id = getattr(r, "id", "") or ""
        if msg_id in exclude:
            continue
        msg = (getattr(r, "message", "") or "").strip()
        if not msg or len(msg) > 400:
            continue
        ts = getattr(r, "timestamp", None)
        ts_str = ts.isoformat() if ts else ""
        lines.append(f"[{msg_id}] {ts_str}: {msg}")
        if len(lines) >= 50:
            break
    return "\n".join(lines) if lines else "(no new user messages in window)"


def _load_active_intentions(*, kind: str, since: datetime) -> str:
    """Pull intention.* pods that haven't been applied yet (no
    metadata.inventory_applied_at_utc)."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        results = store.query(kind=kind, since_utc=since, limit=40)
    except Exception as e:
        logger.warning("[grocery_sync] %s fetch failed: %s", kind, e)
        return f"(no {kind} pods available)"

    lines: List[str] = []
    for pod in results:
        meta = pod.metadata or {}
        if meta.get("inventory_applied_at_utc"):
            continue
        lines.append(f"- {pod.pod_id} — {pod.one_liner}")
        if kind == "intention.shopping":
            items = meta.get("items") or []
            if items:
                lines.append(f"    items: {', '.join(items[:20])}")
            if meta.get("suggested_date"):
                lines.append(f"    suggested_date: {meta['suggested_date']}")
        elif kind == "intention.meal":
            if meta.get("dish"):
                lines.append(f"    dish: {meta['dish']}")
            if meta.get("date"):
                lines.append(f"    date: {meta['date']}")
            ings = meta.get("primary_ingredients") or []
            if ings:
                lines.append(f"    primary_ingredients: {', '.join(ings)}")
    if not lines:
        return f"(no unapplied {kind} pods)"
    return "\n".join(lines)


def _apply_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch one detected intent to the right grocery_inventory function."""
    kind = intent.get("kind")
    pod_id = intent.get("intention_pod_id")
    items = intent.get("items") or []
    if kind == "shopping_done" and pod_id:
        return apply_shopping(pod_id)
    if kind == "meal_consumed" and pod_id:
        return apply_meal_consumption(pod_id)
    if kind == "manual_acquire" and items:
        return manual_acquire(items, source_note=intent.get("chat_msg_text", "")[:120])
    if kind == "manual_consume" and items:
        return manual_consume(items, reason=intent.get("chat_msg_text", "")[:120])
    return {"status": "skipped", "reason": "missing required fields"}


# ── sync state ───────────────────────────────────────────────────────────────

def _load_sync_state() -> Dict[str, Any]:
    path = get_repo_root() / _SYNC_STATE_REL
    if not path.is_file():
        return {"last_sync_at_utc": None, "scanned_chat_msg_ids": []}
    # Fail LOUD on a corrupt state file — silently resetting scanned_chat_msg_ids would
    # re-scan + re-apply every prior chat message (double-counting inventory).
    return json.loads(path.read_text(encoding="utf-8"))


def _save_sync_state(state: Dict[str, Any]) -> None:
    path = get_repo_root() / _SYNC_STATE_REL
    from app.assistant.utils.atomic_write import write_json_atomic
    write_json_atomic(path, state)
