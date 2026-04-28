"""
One-shot script: patch action_dispatch / action_result items from the last 24h
so their state is 'artifact' instead of 'closed'.

Usage:
    .venv\Scripts\python.exe scripts\patch_dispatch_artifacts.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.database.db_handler import UnifiedLog2026
from app.models.base import get_session
from sqlalchemy import select, update

DAYFLOW_ITEM_SOURCE = "dayflow_item"
CUTOFF = datetime.now(timezone.utc) - timedelta(hours=24)

with get_session() as session:
    rows = session.execute(
        select(UnifiedLog2026)
        .where(UnifiedLog2026.source == DAYFLOW_ITEM_SOURCE)
        .where(UnifiedLog2026.timestamp >= CUTOFF)
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
        if source_type not in ("action_dispatch", "action_result"):
            continue
        if meta.get("state") == "artifact":
            continue

        old_state = meta.get("state", "")
        meta["state"] = "artifact"

        session.execute(
            update(UnifiedLog2026)
            .where(UnifiedLog2026.id == row.id)
            .values(metadata_json=json.dumps(meta, ensure_ascii=False))
        )
        patched += 1
        print(f"  patched {row.id}: {old_state} -> artifact")

    session.commit()
    print(f"\nDone. Patched {patched} items.")
