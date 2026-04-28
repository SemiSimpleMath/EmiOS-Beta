"""
Task spec store — read/write task specs to unified_log.

Follows the dayflow_item_writer pattern: each spec is a Message in unified_log
with a stable id, source='task_spec_draft', and room_id='task_spec::{task_id}'.
Metadata holds task_id, editor watermark, and other state.

Usage:
    from app.assistant.lib.task_utils.task_spec_store import (
        create_task_spec, load_task_spec_draft, save_task_spec_draft,
    )
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import insert, select, update as sql_update

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

TASK_SPEC_SOURCE = "task_spec_draft"
ROOM_PREFIX = "task_spec"


def make_task_id() -> str:
    """Generate a new short task_id."""
    return f"task_{uuid.uuid4().hex[:12]}"


def make_room_id(task_id: str) -> str:
    """Build the room_id for a task spec session."""
    return f"{ROOM_PREFIX}::{task_id}"


def make_spec_item_id(task_id: str) -> str:
    """Build the unified_log id for a task spec draft."""
    return f"spec::{task_id}"


def create_task_spec(
    *,
    task_id: Optional[str] = None,
    spec_markdown: str = "",
    caller: str = "",
) -> Dict[str, Any]:
    """Create a new task spec draft in unified_log. Returns metadata dict."""
    from app.models.base import get_session
    from app.assistant.database.db_handler import UnifiedLog2026

    if not task_id:
        task_id = make_task_id()

    item_id = make_spec_item_id(task_id)
    room_id = make_room_id(task_id)
    now_utc = datetime.now(timezone.utc)

    blank_spec = spec_markdown or (
        "---\n"
        f"task_id: {task_id}\n"
        "---\n"
        "## Title\n[New Task]\n\n"
        "## Description\n[TBD]\n\n"
        "## Steps\n[No steps defined yet]\n\n"
        "## Completion\n[TBD]"
    )

    meta = {
        "task_id": task_id,
        "room_id": room_id,
        "created_at": now_utc.isoformat(),
        "updated_at": now_utc.isoformat(),
        "editor_watermark": 0,
    }

    with get_session() as session:
        stmt = insert(UnifiedLog2026).values(
            id=item_id,
            timestamp=now_utc,
            role="system",
            message=blank_spec,
            source=TASK_SPEC_SOURCE,
            processed=False,
            room_id=room_id,
            metadata_json=meta,
            data_json={"data_type": "task_spec_draft"},
            content_type="text",
        )
        session.execute(stmt)
        session.commit()

    logger.info("create_task_spec: task_id=%s room_id=%s caller=%s", task_id, room_id, caller)
    return {"task_id": task_id, "room_id": room_id, "spec_markdown": blank_spec, **meta}


def load_task_spec_draft(task_id: str) -> Optional[Dict[str, Any]]:
    """Load a task spec draft from unified_log. Returns dict with spec_markdown + metadata, or None."""
    from app.models.base import get_session
    from app.assistant.database.db_handler import UnifiedLog2026

    item_id = make_spec_item_id(task_id)

    with get_session() as session:
        row = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.id == item_id)
            .where(UnifiedLog2026.source == TASK_SPEC_SOURCE)
        ).scalar_one_or_none()

        if row is None:
            return None

        meta = _read_metadata(row)
        return {
            "task_id": task_id,
            "room_id": make_room_id(task_id),
            "spec_markdown": str(row.message or ""),
            **meta,
        }


def save_task_spec_draft(
    task_id: str,
    *,
    spec_markdown: str,
    watermark: Optional[int] = None,
    caller: str = "",
) -> Dict[str, Any]:
    """Save (upsert) a task spec draft to unified_log. Returns metadata dict."""
    from app.models.base import get_session
    from app.assistant.database.db_handler import UnifiedLog2026

    item_id = make_spec_item_id(task_id)
    room_id = make_room_id(task_id)
    now_utc = datetime.now(timezone.utc)

    with get_session() as session:
        row = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.id == item_id)
            .where(UnifiedLog2026.source == TASK_SPEC_SOURCE)
        ).scalar_one_or_none()

        if row is not None:
            # Update existing.
            meta = _read_metadata(row)
            meta["updated_at"] = now_utc.isoformat()
            if watermark is not None:
                meta["editor_watermark"] = watermark

            session.execute(
                sql_update(UnifiedLog2026)
                .where(UnifiedLog2026.id == item_id)
                .values(
                    message=spec_markdown,
                    metadata_json=meta,
                    timestamp=now_utc,
                )
            )
            session.commit()
            logger.info("save_task_spec_draft UPDATE: task_id=%s caller=%s", task_id, caller)
            return {"task_id": task_id, "room_id": room_id, "spec_markdown": spec_markdown, **meta}

        else:
            # Create new.
            meta = {
                "task_id": task_id,
                "room_id": room_id,
                "created_at": now_utc.isoformat(),
                "updated_at": now_utc.isoformat(),
                "editor_watermark": watermark or 0,
            }
            stmt = insert(UnifiedLog2026).values(
                id=item_id,
                timestamp=now_utc,
                role="system",
                message=spec_markdown,
                source=TASK_SPEC_SOURCE,
                processed=False,
                room_id=room_id,
                metadata_json=meta,
                data_json={"data_type": "task_spec_draft"},
                content_type="text",
            )
            session.execute(stmt)
            session.commit()
            logger.info("save_task_spec_draft CREATE: task_id=%s caller=%s", task_id, caller)
            return {"task_id": task_id, "room_id": room_id, "spec_markdown": spec_markdown, **meta}


def _read_metadata(row) -> Dict[str, Any]:
    """Safely extract metadata dict from a UnifiedLog2026 row."""
    import json
    raw = row.metadata_json
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, str):
                return json.loads(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
    return {}
