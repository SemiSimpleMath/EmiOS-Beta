from __future__ import annotations

import threading
from typing import List, Optional

from app.assistant.signal_router.contracts import WatchRegistration
from app.assistant.signal_router.models import SignalRouterCursorRow, SignalRouterDedupeRow, SignalRouterWatchRow
from app.models.base import Base, get_session
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class SignalRouterStateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        session = get_session()
        try:
            engine = session.bind
            Base.metadata.create_all(
                engine,
                tables=[
                    SignalRouterWatchRow.__table__,
                    SignalRouterDedupeRow.__table__,
                    SignalRouterCursorRow.__table__,
                ],
                checkfirst=True,
            )
        finally:
            session.close()

    def list_active_watches(self) -> List[WatchRegistration]:
        with self._lock:
            session = get_session()
            try:
                rows = (
                    session.query(SignalRouterWatchRow)
                    .filter(SignalRouterWatchRow.status == "active")
                    .all()
                )
                watches: List[WatchRegistration] = []
                for row in rows:
                    try:
                        watch = WatchRegistration(
                            registration_id=str(row.registration_id),
                            watch_key=str(row.watch_key),
                            event_name=str(row.event_name),
                            watch_type=str(row.watch_type),
                            predicate=row.predicate_json if isinstance(row.predicate_json, dict) else {},
                            dedupe_window_seconds=int(row.dedupe_window_seconds),
                            status="active",
                            expires_at_utc=str(row.expires_at_utc) if row.expires_at_utc else None,
                            created_at_utc=str(row.created_at_utc),
                            metadata=row.metadata_json if isinstance(row.metadata_json, dict) else {},
                        )
                        watch.validate()
                        watches.append(watch)
                    except Exception as e:
                        logger.error(
                            "list_active_watches: skipping invalid persisted watch registration_id=%s error=%s",
                            getattr(row, "registration_id", "<unknown>"),
                            e,
                        )
                        logger.debug("list_active_watches invalid row details", exc_info=True)
                return watches
            finally:
                session.close()

    def upsert_watch(self, watch: WatchRegistration) -> None:
        watch.validate()
        with self._lock:
            session = get_session()
            try:
                row = session.query(SignalRouterWatchRow).filter_by(registration_id=watch.registration_id).one_or_none()
                if row is None:
                    row = SignalRouterWatchRow(registration_id=watch.registration_id)
                row.watch_key = watch.watch_key
                row.event_name = watch.event_name
                row.watch_type = watch.watch_type
                row.predicate_json = watch.predicate
                row.dedupe_window_seconds = watch.dedupe_window_seconds
                row.status = watch.status
                row.expires_at_utc = watch.expires_at_utc
                row.created_at_utc = watch.created_at_utc
                row.metadata_json = watch.metadata
                session.add(row)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def cancel_watches_by_key_prefix(self, *, prefix: str) -> int:
        """Cancel all active watches whose watch_key starts with `prefix`. Returns count cancelled."""
        prefix = str(prefix or "").strip()
        if not prefix:
            raise ValueError("prefix is required")
        with self._lock:
            session = get_session()
            try:
                rows = (
                    session.query(SignalRouterWatchRow)
                    .filter(
                        SignalRouterWatchRow.status == "active",
                        SignalRouterWatchRow.watch_key.like(f"{prefix}%"),
                    )
                    .all()
                )
                count = 0
                for row in rows:
                    row.status = "cancelled"
                    count += 1
                if count:
                    session.commit()
                return count
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def list_active_watch_keys_by_prefix(self, *, prefix: str) -> list[str]:
        """Return all distinct watch_keys for active watches matching prefix."""
        prefix = str(prefix or "").strip()
        if not prefix:
            raise ValueError("prefix is required")
        with self._lock:
            session = get_session()
            try:
                rows = (
                    session.query(SignalRouterWatchRow.watch_key)
                    .filter(
                        SignalRouterWatchRow.status == "active",
                        SignalRouterWatchRow.watch_key.like(f"{prefix}%"),
                    )
                    .all()
                )
                return [str(r.watch_key) for r in rows]
            finally:
                session.close()

    def set_watch_status(self, *, registration_id: str, status: str, not_found_ok: bool = False) -> None:
        registration_id = str(registration_id or "").strip()
        status = str(status or "").strip()
        if not registration_id:
            raise ValueError("registration_id is required")
        if not status:
            raise ValueError("status is required")
        with self._lock:
            session = get_session()
            try:
                row = session.query(SignalRouterWatchRow).filter_by(registration_id=registration_id).one_or_none()
                if row is None:
                    if not_found_ok:
                        logger.debug("set_watch_status: registration_id=%s not found, skipping (not_found_ok=True)", registration_id)
                        return
                    raise ValueError(f"Watch registration not found: {registration_id}")
                row.status = status
                session.add(row)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def has_dedupe_key(self, dedupe_key: str) -> bool:
        if not str(dedupe_key or "").strip():
            raise ValueError("dedupe_key is required")
        with self._lock:
            session = get_session()
            try:
                row = session.query(SignalRouterDedupeRow).filter_by(dedupe_key=dedupe_key).one_or_none()
                return row is not None
            finally:
                session.close()

    def store_dedupe_key(self, *, dedupe_key: str, registration_id: str) -> None:
        dedupe_key = str(dedupe_key or "").strip()
        registration_id = str(registration_id or "").strip()
        if not dedupe_key:
            raise ValueError("dedupe_key is required")
        if not registration_id:
            raise ValueError("registration_id is required")
        with self._lock:
            session = get_session()
            try:
                existing = session.query(SignalRouterDedupeRow).filter_by(dedupe_key=dedupe_key).one_or_none()
                if existing is None:
                    session.add(SignalRouterDedupeRow(dedupe_key=dedupe_key, registration_id=registration_id))
                    session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def get_cursor(self, source_key: str) -> Optional[str]:
        source_key = str(source_key or "").strip()
        if not source_key:
            raise ValueError("source_key is required")
        with self._lock:
            session = get_session()
            try:
                row = session.query(SignalRouterCursorRow).filter_by(source_key=source_key).one_or_none()
                if row is None:
                    return None
                return str(row.cursor_value)
            finally:
                session.close()

    def set_cursor(self, *, source_key: str, cursor_value: str) -> None:
        source_key = str(source_key or "").strip()
        cursor_value = str(cursor_value or "").strip()
        if not source_key:
            raise ValueError("source_key is required")
        if not cursor_value:
            raise ValueError("cursor_value is required")
        with self._lock:
            session = get_session()
            try:
                row = session.query(SignalRouterCursorRow).filter_by(source_key=source_key).one_or_none()
                if row is None:
                    row = SignalRouterCursorRow(source_key=source_key)
                row.cursor_value = cursor_value
                session.add(row)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
