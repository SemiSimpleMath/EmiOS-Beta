import re
import traceback

from sqlalchemy.dialects.sqlite import insert

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.log_ingestion import LogIngestionPolicy
from app.assistant.utils.logging_config import get_maintenance_logger
from app.models.base import get_session

logger = get_maintenance_logger(__name__)


def _extract_links(text: str) -> list[dict]:
    if not isinstance(text, str) or not text.strip():
        return []

    urls = re.findall(r"https?://[^\s<>()\"']+", text)
    out = []
    seen = set()

    for u in urls:
        url = str(u).strip()
        if not url or url in seen:
            continue
        seen.add(url)

        domain = ""
        try:
            from urllib.parse import urlparse
            domain = str(urlparse(url).netloc or "").strip().lower()
        except Exception as e:
            logger.error("Failed parsing URL domain for unified log link: %s", e)
            logger.debug("unified log link domain parse exception details", exc_info=True)

        out.append(
            {
                "url": url,
                "display_text": url,
                "domain": domain,
                "kind": "message_link",
            }
        )

    return out


def _extract_media(metadata_json: dict | None) -> list[dict]:
    if not isinstance(metadata_json, dict):
        return []

    attachments = metadata_json.get("attachments")
    if not isinstance(attachments, list):
        return []

    media = []
    for att in attachments:
        if not isinstance(att, dict):
            continue

        mtype = str(att.get("type") or "").strip().lower()
        path = str(att.get("path") or att.get("url") or "").strip()
        if not path:
            continue

        media.append(
            {
                "type": mtype or "file",
                "url_or_path": path,
                "mime_type": str(att.get("mime_type") or "").strip() or None,
                "title": str(att.get("original_filename") or att.get("filename") or "").strip() or None,
                "source_platform_id": str(att.get("id") or "").strip() or None,
                "caption": str(att.get("caption") or "").strip() or None,
            }
        )

    return media


def _coerce_payload(item: object) -> dict:
    if isinstance(item, dict):
        return dict(item)

    metadata_json = getattr(item, "metadata", None)
    if not isinstance(metadata_json, dict):
        metadata_json = {}

    data_json = getattr(item, "data", None)
    if not isinstance(data_json, dict):
        data_json = {}

    data_type = getattr(item, "data_type", None)
    if data_type:
        metadata_json.setdefault("data_type", data_type)
        data_json.setdefault("data_type", data_type)

    sub_data_type = getattr(item, "sub_data_type", None)
    if isinstance(sub_data_type, list):
        metadata_json.setdefault("sub_data_type", sub_data_type)

    test_mode = bool(getattr(item, "test_mode", False))
    if test_mode:
        metadata_json.setdefault("test_mode", True)

    return {
        "id": getattr(item, "id", None),
        "timestamp": getattr(item, "timestamp", None),
        "role": getattr(item, "role", None),
        "message": getattr(item, "content", None),
        "source": getattr(item, "source", None),
        "processed": bool(getattr(item, "processed", False)),
        "request_id": getattr(item, "request_id", None),
        "room_id": getattr(item, "room_id", None),
        "room_surface": getattr(item, "room_surface", None),
        "room_context_id": getattr(item, "room_context_id", None),
        "direction": getattr(item, "direction", None) or getattr(item, "room_message_direction", None),
        "speaker_id": getattr(item, "speaker_id", None) or getattr(item, "room_speaker_id", None),
        "speaker_name": getattr(item, "speaker_name", None) or getattr(item, "room_speaker_name", None),
        "speaker_role": getattr(item, "speaker_role", None) or getattr(item, "room_speaker_role", None),
        "speaker_external_id": getattr(item, "speaker_external_id", None) or getattr(item, "room_speaker_external_id", None),
        "transport_message_id": getattr(item, "transport_message_id", None),
        "transport_from": getattr(item, "transport_from", None),
        "transport_to": getattr(item, "transport_to", None),
        "content_type": getattr(item, "content_type", None),
        "media_items_json": getattr(item, "media_items_json", None),
        "link_items_json": getattr(item, "link_items_json", None),
        "metadata_json": metadata_json or None,
        "data_json": data_json or None,
    }


def _to_2026_record(msg: dict, source: str) -> dict:
    metadata_json = dict(msg.get("metadata_json") or {}) if isinstance(msg.get("metadata_json"), dict) else {}
    # Accept both "data_json" and "data" keys (room_session_manager uses "data").
    raw_data = msg.get("data_json") or msg.get("data")
    data_json = dict(raw_data) if isinstance(raw_data, dict) else {}

    message_content = str(msg.get("message") or "")
    media_items = msg.get("media_items_json")
    if not isinstance(media_items, list):
        media_items = _extract_media(metadata_json)

    link_items = msg.get("link_items_json")
    if not isinstance(link_items, list):
        link_items = _extract_links(message_content)

    content_type = str(msg.get("content_type") or "").strip()
    if not content_type:
        if media_items and message_content.strip():
            content_type = "text_with_media"
        elif media_items and not message_content.strip():
            content_type = "media_only"
        else:
            content_type = "text"

    return {
        "id": msg.get("id"),
        "timestamp": msg.get("timestamp"),
        "role": msg.get("role") or "unknown",
        "message": message_content,
        "source": msg.get("source") or source,
        "processed": bool(msg.get("processed", False)),
        "request_id": msg.get("request_id"),
        "room_id": msg.get("room_id"),
        "room_surface": msg.get("room_surface"),
        "room_context_id": msg.get("room_context_id"),
        "direction": msg.get("direction") or msg.get("room_message_direction"),
        "speaker_id": msg.get("speaker_id") or msg.get("room_speaker_id"),
        "speaker_name": msg.get("speaker_name") or msg.get("room_speaker_name"),
        "speaker_role": msg.get("speaker_role") or msg.get("room_speaker_role"),
        "speaker_external_id": msg.get("speaker_external_id") or msg.get("room_speaker_external_id"),
        "transport_message_id": msg.get("transport_message_id"),
        "transport_from": msg.get("transport_from"),
        "transport_to": msg.get("transport_to"),
        "content_type": content_type,
        "media_items_json": media_items or None,
        "link_items_json": link_items or None,
        "metadata_json": metadata_json or None,
        "data_json": data_json or None,
    }


def save_proactive_chat_message(*, content: str, sender: str, sub_data_type: list | None = None) -> None:
    from datetime import datetime, timezone
    import uuid

    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    content_str = str(content or "").strip()
    if not content_str:
        return

    payload = {
        "id": msg_id,
        "timestamp": now,
        "role": "assistant",
        "message": content_str,
        "source": "chat",
        "processed": False,
        "request_id": None,
        "room_id": "master_room",
        "room_surface": "ui",
        "room_context_id": "main",
        "direction": "outbound",
        "speaker_id": "system:proactive",
        "speaker_name": sender,
        "speaker_role": "assistant",
        "speaker_external_id": None,
        "transport_message_id": None,
        "transport_from": None,
        "transport_to": None,
        "content_type": "text",
        "media_items_json": None,
        "link_items_json": _extract_links(content_str) or None,
        "metadata_json": {"sub_data_type": sub_data_type or [], "source": "proactive"},
        "data_json": None,
    }

    session = get_session()
    try:
        stmt = insert(UnifiedLog2026).values([payload]).on_conflict_do_nothing(index_elements=["id"])
        session.execute(stmt)
        session.commit()
        logger.debug("save_proactive_chat_message: persisted id=%s sender=%s", msg_id, sender)
    except Exception as e:
        session.rollback()
        logger.error("save_proactive_chat_message: failed to persist: %s", e)
        logger.debug("save_proactive_chat_message exception details", exc_info=True)
        raise
    finally:
        session.close()


def save_to_unified_db(messages, source: str, db_session=None, force_test_db=False):
    """
    Save messages to UnifiedLog2026.

    Behavior:
    - Inserts new rows.
    - Updates existing rows by id.
    - Caller must provide stable ids for records that may be rewritten later.
    """
    own_session = False
    if db_session is None:
        db_session = get_session(force_test_db=force_test_db)
        own_session = True

    try:
        if not messages:
            logger.debug("No %s messages to save.", source)
            return

        records_2026 = []
        for raw in messages:
            msg = _coerce_payload(raw)
            allowed, reason = LogIngestionPolicy.should_persist_payload(source, msg)
            if not allowed:
                logger.debug("[%s] Skipping log payload id=%s reason=%s", source, msg.get("id"), reason)
                continue

            record = _to_2026_record(msg, source)
            if not record.get("id"):
                logger.warning("[%s] Skipping payload with empty id: %r", source, msg)
                continue

            records_2026.append(record)

        if not records_2026:
            logger.debug("[%s] No messages qualified after policy filter.", source)
            return

        # Guard against pre-serialized JSON strings in JSON columns.
        # SQLAlchemy's JSON type auto-serializes dicts; passing a string
        # causes double-encoding (string-of-JSON stored as JSON string).
        import json as _json
        for rec in records_2026:
            for json_key in ("metadata_json", "data_json"):
                val = rec.get(json_key)
                if isinstance(val, str):
                    try:
                        rec[json_key] = _json.loads(val)
                    except (ValueError, TypeError):
                        rec[json_key] = None

        # Detect cross-source ID collisions before upsert.
        rec_ids = [r["id"] for r in records_2026 if r.get("id")]
        if rec_ids:
            from sqlalchemy.sql import select as _select
            existing_rows = db_session.execute(
                _select(UnifiedLog2026.id, UnifiedLog2026.source)
                .where(UnifiedLog2026.id.in_(rec_ids))
            ).all()
            existing_source_by_id = {r.id: r.source for r in existing_rows}
            for rec in records_2026:
                rid = rec.get("id")
                if rid and rid in existing_source_by_id:
                    old_source = existing_source_by_id[rid]
                    new_source = rec.get("source") or source
                    if old_source and new_source and old_source != new_source:
                        logger.error(
                            "CROSS-SOURCE ID COLLISION: id='%s' exists with source='%s', "
                            "would be overwritten by source='%s'. This should never happen.",
                            rid, old_source, new_source,
                        )

        stmt = insert(UnifiedLog2026).values(records_2026)

        update_columns = {
            "timestamp": stmt.excluded.timestamp,
            "role": stmt.excluded.role,
            "message": stmt.excluded.message,
            "source": stmt.excluded.source,
            "processed": stmt.excluded.processed,
            "request_id": stmt.excluded.request_id,
            "room_id": stmt.excluded.room_id,
            "room_surface": stmt.excluded.room_surface,
            "room_context_id": stmt.excluded.room_context_id,
            "direction": stmt.excluded.direction,
            "speaker_id": stmt.excluded.speaker_id,
            "speaker_name": stmt.excluded.speaker_name,
            "speaker_role": stmt.excluded.speaker_role,
            "speaker_external_id": stmt.excluded.speaker_external_id,
            "transport_message_id": stmt.excluded.transport_message_id,
            "transport_from": stmt.excluded.transport_from,
            "transport_to": stmt.excluded.transport_to,
            "content_type": stmt.excluded.content_type,
            "media_items_json": stmt.excluded.media_items_json,
            "link_items_json": stmt.excluded.link_items_json,
            "metadata_json": stmt.excluded.metadata_json,
            "data_json": stmt.excluded.data_json,
        }

        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_=update_columns,
        )

        result = db_session.execute(stmt)
        db_session.commit()

        logger.debug(
            "[%s] Messages saved. Rows affected unified_log_2026=%s",
            source,
            result.rowcount,
        )

    except Exception as e:
        db_session.rollback()
        logger.error("[%s] Error saving messages: %s", source, e)
        logger.error(traceback.format_exc())
        raise

    finally:
        if own_session:
            db_session.close()