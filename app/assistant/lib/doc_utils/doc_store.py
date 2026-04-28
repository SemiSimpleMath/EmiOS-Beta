"""
Doc store — read/write doc drafts to unified_log.

Mirrors the task_spec_store pattern: each doc is a Message in unified_log
with a stable id, source='doc_draft', and room_id='doc_editor::{doc_id}'.
Metadata holds doc_type, doc_name, google_doc_id, watermark, timestamps.

Usage:
    from app.assistant.lib.doc_utils.doc_store import (
        create_doc_draft, load_doc_draft, save_doc_draft,
    )
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import insert, select, update as sql_update

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

DOC_SOURCE = "doc_draft"
ROOM_PREFIX = "doc_editor"


def make_doc_id() -> str:
    return f"doc_{uuid.uuid4().hex[:12]}"


def make_room_id(doc_id: str) -> str:
    return f"{ROOM_PREFIX}::{doc_id}"


def make_doc_item_id(doc_id: str) -> str:
    return f"doc::{doc_id}"


def create_doc_draft(
    *,
    doc_id: Optional[str] = None,
    doc_markdown: str = "",
    doc_type: str = "md",
    doc_name: str = "",
    google_doc_id: str = "",
    caller: str = "",
) -> Dict[str, Any]:
    """Create a new doc draft in unified_log. Returns metadata dict."""
    from app.models.base import get_session
    from app.assistant.database.db_handler import UnifiedLog2026

    if not doc_id:
        doc_id = make_doc_id()

    item_id = make_doc_item_id(doc_id)
    room_id = make_room_id(doc_id)
    now_utc = datetime.now(timezone.utc)

    meta = {
        "doc_id": doc_id,
        "room_id": room_id,
        "doc_type": doc_type or "md",
        "doc_name": doc_name or "",
        "google_doc_id": google_doc_id or "",
        "created_at": now_utc.isoformat(),
        "updated_at": now_utc.isoformat(),
        "watermark": 0,
    }

    with get_session() as session:
        stmt = insert(UnifiedLog2026).values(
            id=item_id,
            timestamp=now_utc,
            role="system",
            message=doc_markdown or "",
            source=DOC_SOURCE,
            processed=False,
            room_id=room_id,
            metadata_json=meta,
            data_json={"data_type": "doc_draft"},
            content_type="text",
        )
        session.execute(stmt)
        session.commit()

    logger.info("create_doc_draft: doc_id=%s room_id=%s caller=%s", doc_id, room_id, caller)
    return {"doc_id": doc_id, "room_id": room_id, "doc_markdown": doc_markdown or "", **meta}


def load_doc_draft(doc_id: str) -> Optional[Dict[str, Any]]:
    """Load a doc draft from unified_log. Returns dict with doc_markdown + metadata, or None."""
    from app.models.base import get_session
    from app.assistant.database.db_handler import UnifiedLog2026

    item_id = make_doc_item_id(doc_id)

    with get_session() as session:
        row = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.id == item_id)
            .where(UnifiedLog2026.source == DOC_SOURCE)
        ).scalar_one_or_none()

        if row is None:
            return None

        meta = _read_metadata(row)
        return {
            "doc_id": doc_id,
            "room_id": make_room_id(doc_id),
            "doc_markdown": str(row.message or ""),
            **meta,
        }


def save_doc_draft(
    doc_id: str,
    *,
    doc_markdown: Optional[str] = None,
    watermark: Optional[int] = None,
    doc_type: Optional[str] = None,
    doc_name: Optional[str] = None,
    google_doc_id: Optional[str] = None,
    caller: str = "",
) -> Dict[str, Any]:
    """Upsert a doc draft in unified_log. Only provided fields are updated."""
    from app.models.base import get_session
    from app.assistant.database.db_handler import UnifiedLog2026

    item_id = make_doc_item_id(doc_id)
    room_id = make_room_id(doc_id)
    now_utc = datetime.now(timezone.utc)

    with get_session() as session:
        row = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.id == item_id)
            .where(UnifiedLog2026.source == DOC_SOURCE)
        ).scalar_one_or_none()

        if row is not None:
            meta = _read_metadata(row)
            meta["updated_at"] = now_utc.isoformat()
            if watermark is not None:
                meta["watermark"] = watermark
            if doc_type is not None:
                meta["doc_type"] = doc_type
            if doc_name is not None:
                meta["doc_name"] = doc_name
            if google_doc_id is not None:
                meta["google_doc_id"] = google_doc_id

            values: Dict[str, Any] = {
                "metadata_json": meta,
                "timestamp": now_utc,
            }
            if doc_markdown is not None:
                values["message"] = doc_markdown

            session.execute(
                sql_update(UnifiedLog2026)
                .where(UnifiedLog2026.id == item_id)
                .values(**values)
            )
            session.commit()
            logger.info("save_doc_draft UPDATE: doc_id=%s caller=%s", doc_id, caller)
            current_markdown = doc_markdown if doc_markdown is not None else str(row.message or "")
            return {"doc_id": doc_id, "room_id": room_id, "doc_markdown": current_markdown, **meta}

        # Create new.
        meta = {
            "doc_id": doc_id,
            "room_id": room_id,
            "doc_type": doc_type or "md",
            "doc_name": doc_name or "",
            "google_doc_id": google_doc_id or "",
            "created_at": now_utc.isoformat(),
            "updated_at": now_utc.isoformat(),
            "watermark": watermark or 0,
        }
        stmt = insert(UnifiedLog2026).values(
            id=item_id,
            timestamp=now_utc,
            role="system",
            message=doc_markdown or "",
            source=DOC_SOURCE,
            processed=False,
            room_id=room_id,
            metadata_json=meta,
            data_json={"data_type": "doc_draft"},
            content_type="text",
        )
        session.execute(stmt)
        session.commit()
        logger.info("save_doc_draft CREATE: doc_id=%s caller=%s", doc_id, caller)
        return {"doc_id": doc_id, "room_id": room_id, "doc_markdown": doc_markdown or "", **meta}


def _read_metadata(row) -> Dict[str, Any]:
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
