"""
One-shot script: patch plan_task items stuck in 'dispatched' state that already
have execution results — transition them to 'acted_on'.

Usage:
    .venv\Scripts\python.exe scripts\patch_stuck_dispatched.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.database.db_handler import UnifiedLog2026
from app.models.base import get_session
from sqlalchemy import select, update

DAYFLOW_ITEM_SOURCE = "dayflow_item"

with get_session() as session:
    rows = session.execute(
        select(UnifiedLog2026)
        .where(UnifiedLog2026.source == DAYFLOW_ITEM_SOURCE)
    ).scalars().all()

    patched = 0
    for row in rows:
        meta = {}
        if row.metadata_json:
            try:
                meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
            except (json.JSONDecodeError, TypeError):
                continue
        if not isinstance(meta, dict):
            continue

        source_type = meta.get("source_type", "")
        state = meta.get("state", "")
        has_result = bool(meta.get("execution_result_summary") or meta.get("execution_result"))

        if source_type != "plan_task":
            continue
        if state != "dispatched":
            continue
        if not has_result:
            continue

        meta["state"] = "acted_on"
        meta["state_reason"] = "action_completed (patched: stuck dispatched)"
        meta["last_reviewed_at"] = datetime.now(timezone.utc).isoformat()

        session.execute(
            update(UnifiedLog2026)
            .where(UnifiedLog2026.id == row.id)
            .values(metadata_json=json.dumps(meta, ensure_ascii=False))
        )
        patched += 1
        short_id = meta.get("short_id", "")
        summary = (meta.get("summary") or "")[:80]
        print(f"  patched task {short_id} ({row.id}): dispatched -> acted_on | {summary}")

    session.commit()
    print(f"\nDone. Patched {patched} items.")
