"""CLI to sync grocery inventory state from recent chat + apply decay.

Three things in order:

1. Scan recent user chat for intents (calls grocery_intent_scanner agent)
2. Apply each intent (grocery_inventory.apply_*)
3. Apply daily decay (move past-decay items to history)

State tracking: resources/subconscious/resource_grocery_sync_state.json
records the last scan timestamp + chat_msg_ids already processed so we
don't re-apply the same intent on every tick.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_grocery_sync
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_grocery_sync --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_grocery_sync --hours 24
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.subconscious.grocery_inventory import (
    apply_decay,
    apply_meal_consumption,
    apply_shopping,
    manual_acquire,
    manual_consume,
    render_inventory_summary,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import (
    Message,
)

from app.assistant.scope.loader import load_scope_for_source

logger = get_logger(__name__)


_SYNC_STATE_REL = "resources/subconscious/resource_grocery_sync_state.json"


def main():
    parser = argparse.ArgumentParser(description="Sync grocery inventory from recent chat.")
    parser.add_argument("--hours", type=int, default=48, help="Chat lookback window (default 48h).")
    parser.add_argument("--dry-run", action="store_true", help="Detect intents but don't apply.")
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="Ignore previously-scanned set; re-scan everything in the window.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"GROCERY SYNC — lookback={args.hours}h dry_run={args.dry_run}")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    state = _load_sync_state()
    already_scanned = set() if args.scan_all else set(state.get("scanned_chat_msg_ids") or [])
    print(f"\n[1/4] Previously scanned message ids: {len(already_scanned)}")

    # 1. Build scanner context
    print("[2/4] Building scanner context...")
    ctx = _build_scanner_context(hours=args.hours, exclude_msg_ids=already_scanned)
    for k, v in ctx.items():
        v_str = v if isinstance(v, str) else str(v)
        preview = v_str[:80].replace("\n", " ⏎ ")
        print(f"        {k:32s} ({len(v_str):>6} chars) {preview}")

    if not ctx["recent_user_messages"] or ctx["recent_user_messages"].startswith("(no"):
        print("\n[3/4] No new chat to scan. Skipping LLM call.")
        intents: List[Dict[str, Any]] = []
        skipped_ids: List[str] = []
    else:
        # 2. Invoke scanner agent
        print("\n[3/4] Invoking grocery_intent_scanner...")
        agent = DI.agent_factory.create_agent("grocery_intent_scanner")
        if agent is None:
            print("ERROR: failed to create grocery_intent_scanner agent.")
            sys.exit(1)
        scope = load_scope_for_source(
            kind="subsystem",
            source_id="subconscious",
            actor_id="run_grocery_sync",
            identity_overrides={"owner_id": "system", "scope_id": "subconscious::grocery_intent_scanner", "surface": "internal"},
        )
        msg = Message(agent_input=ctx, scope_context=scope)
        try:
            result = agent.action_handler(msg)
        except Exception as e:
            print(f"ERROR: scanner raised: {type(e).__name__}: {e}")
            logger.exception("scanner raised")
            sys.exit(2)
        output = result.data if isinstance(getattr(result, "data", None), dict) else {}
        intents = output.get("intents") or []
        skipped_ids = output.get("skipped_chat_msg_ids") or []
        notes = (output.get("notes") or "").strip()
        if notes:
            print(f"        scanner notes: {notes}")

    print(f"        detected intents: {len(intents)}")
    for intent in intents:
        print(
            f"        • [{intent.get('confidence')}] {intent.get('kind')}: "
            f"{intent.get('chat_msg_text', '')[:80]!r}"
        )
        if intent.get("intention_pod_id"):
            print(f"          → pod: {intent['intention_pod_id']}")
        if intent.get("items"):
            print(f"          → items: {', '.join(intent['items'])}")

    # 3. Apply intents
    if args.dry_run:
        print("\n[4/4] --dry-run — not applying intents or decay.")
    else:
        print("\n[4/4] Applying intents + daily decay...")
        applied_summaries: List[Dict[str, Any]] = []
        for intent in intents:
            if intent.get("confidence") == "low":
                print(f"  - SKIP low-confidence intent: {intent.get('kind')}")
                continue
            summary = _apply_intent(intent)
            applied_summaries.append({"intent": intent, "result": summary})
            print(f"  - applied {intent.get('kind')}: {summary}")

        # Apply decay
        decay_summary = apply_decay()
        print(f"  - decay: {decay_summary}")

        # Update state — mark these chat_msg_ids as scanned
        new_scanned = set(state.get("scanned_chat_msg_ids") or [])
        new_scanned.update(intent.get("chat_msg_id", "") for intent in intents if intent.get("chat_msg_id"))
        new_scanned.update(skipped_ids)
        # Cap state size — only keep last 500 ids
        scanned_list = sorted(new_scanned)[-500:]
        state["scanned_chat_msg_ids"] = scanned_list
        state["last_sync_at_utc"] = datetime.now(timezone.utc).isoformat()
        _save_sync_state(state)

    # Final snapshot
    print("\n" + "─" * 70)
    print("Current inventory snapshot:")
    print(render_inventory_summary())
    print("─" * 70)

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


def _build_scanner_context(*, hours: int, exclude_msg_ids: Set[str]) -> Dict[str, str]:
    """Assemble scanner inputs: recent user chat, active shopping/meal intentions."""
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


# ── sync state ──


def _load_sync_state() -> Dict[str, Any]:
    path = get_repo_root() / _SYNC_STATE_REL
    if not path.is_file():
        return {"last_sync_at_utc": None, "scanned_chat_msg_ids": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_sync_at_utc": None, "scanned_chat_msg_ids": []}


def _save_sync_state(state: Dict[str, Any]) -> None:
    path = get_repo_root() / _SYNC_STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
