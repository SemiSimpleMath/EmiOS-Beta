"""Unified-log source for the gut.

Reads chat-like inbound messages from ``unified_log_2026``. This is the
real substrate — messages that bypass GlobalBlackBoard still land here, so
polling unified_log is strictly more comprehensive than the legacy
blackboard-based scan.

## Inclusion

Filters by ``room_id`` using an explicit positive list:
- exact room_ids (default: ``["master_room"]``)
- room_id prefix patterns (default: ``["slack/", "tg_", "telegram/"]``)

Anything outside the list is skipped entirely, which excludes
``dayflow_orchestrator``, ``task_create``, ``doc_editor``, task spec rooms,
and other internal/operational writers.

## Cursor

``rowid``-based. SQLite's implicit rowid is monotonic and unique per
table, so tracking the max rowid seen is a clean ordered cursor — no
timestamp-collision issues at poll boundaries.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import literal_column, or_, text

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.ingest.contracts import IngestEnvelope
from app.assistant.ingest.cursors import IngestCursorStore
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


_CURSOR_KEY = "unified_log_last_rowid"

_DEFAULT_EXACT_ROOMS = ("master_room",)
_DEFAULT_ROOM_PREFIXES = ("slack/", "tg_", "telegram/")


class UnifiedLogSource:
    """Pulls new chat-like rows from unified_log_2026."""

    name = "unified_log"

    def __init__(
        self,
        cursor_store: Optional[IngestCursorStore] = None,
        included_rooms: Optional[List[str]] = None,
        included_room_prefixes: Optional[List[str]] = None,
    ) -> None:
        self._cursor_store = cursor_store or IngestCursorStore()
        self._included_rooms: tuple[str, ...] = tuple(
            included_rooms if included_rooms is not None else _DEFAULT_EXACT_ROOMS
        )
        self._included_room_prefixes: tuple[str, ...] = tuple(
            included_room_prefixes if included_room_prefixes is not None else _DEFAULT_ROOM_PREFIXES
        )
        self._last_rowid: Optional[int] = self._load_cursor_or_pin_to_now()

    def _load_cursor_or_pin_to_now(self) -> int:
        """Return persisted cursor, or — on first run — pin to current max(rowid).

        Pinning to max(rowid) on first run is critical: without it, a fresh
        install polls every master_room / slack / telegram row ever written
        and invokes the watcher LLM on each one. Mirrors the "don't
        re-ingest history" behavior EmailRepoSource uses.
        """
        raw = self._cursor_store.get(_CURSOR_KEY)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                logger.error(
                    "UnifiedLogSource: unparseable cursor value=%r; pinning to current max rowid",
                    raw,
                )

        session = get_session()
        try:
            max_rid = session.execute(
                text("SELECT MAX(rowid) FROM unified_log_2026")
            ).scalar()
            pinned = int(max_rid) if max_rid is not None else 0
        finally:
            session.close()

        self._cursor_store.set(source_key=_CURSOR_KEY, cursor_value=str(pinned))
        logger.info(
            "UnifiedLogSource: first run — pinned cursor to current max rowid=%d "
            "(will not re-ingest historical rows)",
            pinned,
        )
        return pinned

    def pull(self) -> List[IngestEnvelope]:
        rowid_col = literal_column("unified_log_2026.rowid")

        session = get_session()
        try:
            q = session.query(UnifiedLog2026, rowid_col.label("rid"))
            if self._last_rowid is not None:
                q = q.filter(rowid_col > self._last_rowid)

            room_clauses = [UnifiedLog2026.room_id == r for r in self._included_rooms]
            for prefix in self._included_room_prefixes:
                room_clauses.append(UnifiedLog2026.room_id.like(f"{prefix}%"))
            if not room_clauses:
                return []
            q = q.filter(or_(*room_clauses))
            q = q.order_by(rowid_col)

            results = q.all()
        finally:
            session.close()

        if not results:
            return []

        envelopes: List[IngestEnvelope] = []
        max_rid: Optional[int] = None
        for row, rid in results:
            if row.message is None or not str(row.message).strip():
                if max_rid is None or rid > max_rid:
                    max_rid = rid
                continue
            envelope = self._row_to_envelope(row)
            envelopes.append(envelope)
            if max_rid is None or rid > max_rid:
                max_rid = rid

        if max_rid is not None:
            self._cursor_store.set(source_key=_CURSOR_KEY, cursor_value=str(max_rid))
            self._last_rowid = max_rid

        return envelopes

    @staticmethod
    def _row_to_envelope(row: UnifiedLog2026) -> IngestEnvelope:
        occurred_at_utc = row.timestamp.isoformat() if row.timestamp else ""
        source_id = (
            str(row.speaker_id or row.speaker_name or row.transport_from or "unknown").strip()
            or "unknown"
        )
        signal_type = str(row.source or "unified_log_row")
        data = row.data_json if isinstance(row.data_json, dict) else {}
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}

        metadata = {
            "room_id": row.room_id,
            "room_surface": row.room_surface,
            "room_context_id": row.room_context_id,
            "direction": row.direction,
            "speaker_role": row.speaker_role,
            "speaker_name": row.speaker_name,
            "speaker_external_id": row.speaker_external_id,
            "transport_message_id": row.transport_message_id,
            "source": row.source,
            "unified_log_metadata": meta,
        }

        envelope = IngestEnvelope(
            signal_id=str(row.id),
            source_type="unified_log",
            source_id=source_id,
            occurred_at_utc=occurred_at_utc,
            signal_type=signal_type,
            content=str(row.message or ""),
            data=data,
            metadata=metadata,
        )
        envelope.validate()
        return envelope
