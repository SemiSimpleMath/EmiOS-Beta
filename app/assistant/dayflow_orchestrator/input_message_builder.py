from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.routine_manager.utils import write_json_file
from app.assistant.ticket_manager.ticket import TicketState
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir, get_resources_dir
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time, parse_iso_utc_strict

logger = get_logger(__name__)


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _build_item_id(prefix: str, *parts: str) -> str:
    normalized = "|".join(str(p or "").strip().lower() for p in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _ticket_state_to_item_state(ticket_state: str) -> str:
    state = str(ticket_state or "").strip().lower()
    if state in {TicketState.PENDING.value, TicketState.PROPOSED.value}:
        return "closed"
    if state == TicketState.SNOOZED.value:
        return "suppressed"
    if state in {TicketState.ACCEPTED.value, TicketState.EXECUTING.value}:
        return "closed"
    if state in {
        TicketState.COMPLETED.value,
        TicketState.DISMISSED.value,
        TicketState.EXPIRED.value,
        TicketState.FAILED.value,
    }:
        return "closed"
    raise ValueError(f"Unhandled ticket state for dayflow item mapping: '{ticket_state}'")


def _build_ticket_message(ticket: Any, now_utc: datetime) -> Message:
    ticket_id = str(getattr(ticket, "ticket_id", "") or "").strip()
    if not ticket_id:
        raise ValueError("Ticket is missing ticket_id.")
    title = str(getattr(ticket, "title", "") or "").strip()
    if not title:
        raise ValueError(f"Ticket '{ticket_id}' is missing title.")
    source_state = str(getattr(ticket, "state", "") or "").strip().lower()
    if not source_state:
        raise ValueError(f"Ticket '{ticket_id}' is missing state.")

    source_lifecycle_state = _ticket_state_to_item_state(source_state)
    snooze_until = getattr(ticket, "snooze_until", None)
    metadata: Dict[str, Any] = {
        "item_id": f"ticket:{ticket_id}",
        "source_type": "ticket",
        "event_type": "ticket_state_snapshot",
        "created_at": _to_iso(getattr(ticket, "created_at", None)) or now_utc.isoformat(),
        "summary": title,
        "importance": "medium",
        "actionability": "actionable",
        "state": "new",
        "source_lifecycle_state": source_lifecycle_state,
        "state_reason": f"ticket_state:{source_state}",
        "last_reviewed_at": now_utc.isoformat(),
        "cooldown_until": _to_iso(snooze_until),
        "linked_item_ids": [],
        "source_ticket_id": ticket_id,
        "source_ticket_type": str(getattr(ticket, "ticket_type", "") or "").strip(),
        "source_suggestion_type": str(getattr(ticket, "suggestion_type", "") or "").strip(),
    }

    return Message(
        id=metadata["item_id"],
        data_type="dayflow_input_item",
        sub_data_type=["dayflow_orchestrator", "input_layer", "new", "ticket", "dayflow_artifact"],
        sender="ticket_manager",
        content=title,
        timestamp=now_utc,
        room_id="dayflow_orchestrator",
        metadata=metadata,
    )


def _build_calendar_message_from_structured(*, event: Dict[str, Any], now_utc: datetime) -> Message:
    title = str(event.get("title") or "").strip()
    if not title:
        raise ValueError("Structured calendar event is missing title.")
    start_utc = parse_iso_utc_strict(event.get("start_utc"), label="calendar.start_utc")
    end_raw = event.get("end_utc")
    end_utc = parse_iso_utc_strict(end_raw, label="calendar.end_utc") if end_raw else None

    if end_utc is not None and end_utc < start_utc:
        raise ValueError(
            f"Structured calendar event has end before start. title='{title}', "
            f"start={start_utc.isoformat()}, end={end_utc.isoformat()}"
        )

    status = str(event.get("status") or "").strip().lower()
    if status not in {"upcoming", "ongoing", "completed", "cancelled"}:
        raise ValueError(
            f"Structured calendar event has invalid status '{status}' for title '{title}'. "
            "Expected one of: upcoming, ongoing, completed, cancelled."
        )

    # Calendar entries are context artifacts, not direct actions.
    inferred_state = "new"
    if status in ("completed", "cancelled"):
        state_reason = f"calendar_{status}"
    elif status == "ongoing":
        state_reason = "calendar_ongoing"
    else:
        state_reason = "calendar_upcoming"

    calendar_item_id = str(event.get("calendar_item_id") or "").strip()
    if not calendar_item_id:
        calendar_item_id = _build_item_id(
            "calendar",
            title,
            start_utc.isoformat(),
            end_utc.isoformat() if end_utc else "",
        )

    metadata: Dict[str, Any] = {
        "item_id": f"calendar:{calendar_item_id}",
        "source_type": "calendar",
        "event_type": "calendar_structured_event",
        "created_at": now_utc.isoformat(),
        "summary": title,
        "importance": "high" if status == "ongoing" else "medium",
        "actionability": "context_only",
        "state": inferred_state,
        "state_reason": state_reason,
        "last_reviewed_at": now_utc.isoformat(),
        "cooldown_until": None,
        "linked_item_ids": [],
        "scheduled_start_utc": start_utc.isoformat(),
        "scheduled_end_utc": end_utc.isoformat() if end_utc else None,
        "calendar_status": status,
        "calendar_source": str(event.get("source") or "").strip(),
        "calendar_start_local": str(event.get("start_local") or "").strip(),
        "calendar_end_local": str(event.get("end_local") or "").strip(),
    }

    return Message(
        id=metadata["item_id"],
        data_type="dayflow_input_item",
        sub_data_type=["dayflow_orchestrator", "input_layer", "new", "calendar_event", "dayflow_artifact"],
        sender="daily_context_generator",
        content=title,
        timestamp=now_utc,
        room_id="dayflow_orchestrator",
        metadata=metadata,
    )


def _build_email_message(*, email_data: Dict[str, Any], now_utc: datetime) -> Message:
    """
    Convert an event-repository email dict into a dayflow Message.

    Uses a ``dayflow_email:`` ID prefix so the dayflow upsert never collides
    with the raw email record already stored as ``email:{account}:{uid}``.
    """
    uid = str(email_data.get("uid") or email_data.get("message_id") or "").strip()
    if not uid:
        raise ValueError("Email event is missing uid/message_id.")

    account_id = str(email_data.get("account_id") or "").strip()
    subject = str(email_data.get("subject") or "").strip() or "[No Subject]"
    sender = str(email_data.get("sender") or "").strip() or "Unknown"
    summary = str(email_data.get("summary") or "").strip()

    importance_raw = email_data.get("importance")
    if importance_raw is None:
        raise ValueError(f"Email uid={uid} missing importance.")
    importance_int = int(importance_raw)

    if importance_int >= 7:
        inferred_importance = "high"
    elif importance_int >= 5:
        inferred_importance = "medium"
    else:
        inferred_importance = "low"

    action_items = email_data.get("action_items")
    if not isinstance(action_items, list):
        action_items = []

    item_id = f"dayflow_email:{account_id}:{uid}" if account_id else f"dayflow_email:{uid}"
    raw_email_unified_id = f"email:{account_id}:{uid}" if account_id else f"email:{uid}"

    date_received_str = str(email_data.get("date_received") or "").strip()
    email_timestamp = now_utc
    if date_received_str and date_received_str != "[No Date]":
        try:
            from email.utils import parsedate_to_datetime
            parsed_dt = parsedate_to_datetime(date_received_str)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            email_timestamp = parsed_dt.astimezone(timezone.utc)
        except Exception as e:
            logger.error("Could not parse email date_received %r for uid=%s: %s", date_received_str, uid, e)
            logger.debug("email date_received parse exception details", exc_info=True)
            raise

    content_parts = [f"Email from {sender}: {subject}"]
    if summary:
        content_parts.append(summary)
    content = ". ".join(content_parts)

    metadata: Dict[str, Any] = {
        "item_id": item_id,
        "source_type": "email",
        "event_type": "email_inbound",
        "created_at": email_timestamp.isoformat(),
        "created_at_local": email_timestamp.astimezone().strftime("%I:%M %p"),
        "summary": f"{sender}: {subject}",
        "importance": inferred_importance,
        "actionability": "actionable" if action_items else "context_only",
        "state": "new",
        "state_reason": "email_ingested",
        "last_reviewed_at": now_utc.isoformat(),
        "cooldown_until": None,
        "linked_item_ids": [],
        "linked_email_unified_id": raw_email_unified_id,
        "email_uid": uid,
        "email_account_id": account_id,
        "email_thread_id": str(email_data.get("thread_id") or "").strip(),
        "email_sender": sender,
        "email_subject": subject,
        "email_summary": summary,
        "email_importance_score": importance_int,
        "email_action_items": action_items,
        "email_direction": str(email_data.get("direction") or "inbound").strip(),
    }

    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sub_data_type=["dayflow_orchestrator", "input_layer", "new", "email", "dayflow_artifact"],
        sender="email_ingest",
        content=content,
        timestamp=email_timestamp,
        room_id="dayflow_orchestrator",
        metadata=metadata,
    )


def _load_emails_from_event_repo(*, now_utc: datetime) -> List[Dict[str, Any]]:
    """Load today's important emails (importance >= 5) from the event repository."""
    import json

    from app.assistant.event_repository.event_repository import EventRepositoryManager
    from app.assistant.utils.time_utils import utc_to_local

    now_local = utc_to_local(now_utc)
    today_midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    repo = EventRepositoryManager()
    raw_json = repo.search_events(data_type="email")
    all_events: list = json.loads(raw_json) if raw_json else []

    result: List[Dict[str, Any]] = []
    for event in all_events:
        data = event.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as e:
                logger.error("_load_emails_from_event_repo: unparseable event data JSON: %s", e)
                logger.debug("_load_emails_from_event_repo data JSON parse exception details", exc_info=True)
                raise

        importance = data.get("importance")
        if importance is None:
            continue
        try:
            if int(importance) < 5:
                continue
        except (ValueError, TypeError):
            continue

        date_str = (data.get("date_received") or "").strip()
        if date_str and date_str != "[No Date]":
            try:
                from email.utils import parsedate_to_datetime
                email_dt_local = utc_to_local(parsedate_to_datetime(date_str))
                if email_dt_local < today_midnight_local:
                    continue
            except Exception as e:
                logger.error("_load_emails_from_event_repo: could not parse date %r: %s", date_str, e)
                logger.debug("_load_emails_from_event_repo date parse exception details", exc_info=True)
                raise

        result.append(data)

    return result


def _load_expected_calendar() -> Dict[str, Any]:
    resource_manager = getattr(DI, "resource_manager", None)
    if resource_manager is None:
        raise RuntimeError("resource_manager service is not registered.")
    payload = resource_manager.get_resource(
        scope_context={"resources": {"allowed_global_resources": ["resource_expected_calendar"]}},
        resource_id="resource_expected_calendar",
        required=False,
    )
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(
            "resource_expected_calendar must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    return payload


def _build_expected_schedule_from_api(*, boundary_date_local: str) -> List[Dict[str, Any]]:
    from app.assistant.pipelines.dayflow.utils.context_sources import get_calendar_events_structured_for_day

    events = get_calendar_events_structured_for_day(str(boundary_date_local))
    expected_schedule: List[Dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        expected_schedule.append(
            {
                "title": str(ev.get("title") or "Untitled"),
                "start_local": str(ev.get("start_local") or ""),
                "end_local": str(ev.get("end_local") or ""),
                "status": str(ev.get("status") or "upcoming"),
                "source": "calendar",
                "calendar_item_id": str(ev.get("calendar_item_id") or ""),
                "start_utc": str(ev.get("start_utc") or ""),
                "end_utc": str(ev.get("end_utc") or ""),
            }
        )
    if not expected_schedule:
        logger.info("No calendar events found for boundary date %s (normal for fresh installs).", boundary_date_local)
    return expected_schedule


def _seed_expected_calendar_from_api(*, now_utc: datetime) -> Dict[str, Any]:
    from app.assistant.pipelines.context import boundary_date_local_str

    resource_manager = getattr(DI, "resource_manager", None)
    if resource_manager is None:
        raise RuntimeError("resource_manager service is not registered.")

    local_now = get_local_time()
    boundary_date_local = boundary_date_local_str(local_now)
    expected_schedule = _build_expected_schedule_from_api(boundary_date_local=boundary_date_local)
    payload: Dict[str, Any] = {
        "date": boundary_date_local,
        "expected_schedule": expected_schedule,
        "source": "api_seed",
        "last_updated": local_now.strftime("%Y-%m-%d %I:%M %p"),
        "last_updated_utc": now_utc.isoformat(),
    }
    # Persist canonical file directly, then hydrate resource cache.
    expected_calendar_path = get_resources_dir() / "dayflow_pipeline_outputs" / "resource_expected_calendar.json"
    write_json_file(expected_calendar_path, payload)
    resource_manager.update_resource("resource_expected_calendar", payload, persist=False)
    logger.info(
        "Auto-seeded resource_expected_calendar from API (%d item(s), boundary=%s).",
        len(expected_schedule),
        boundary_date_local,
    )
    return payload


def _boundary_hour_local() -> int:
    try:
        from app.assistant.routine_manager.utils import read_json_file

        cfg = read_json_file(get_configs_dir() / "dayflow_pipeline.json") or {}
        daily_reset = cfg.get("daily_reset") if isinstance(cfg, dict) else {}
        if not isinstance(daily_reset, dict):
            raise ValueError("dayflow_pipeline.daily_reset must be a JSON object.")
        if "boundary_hour_local" not in daily_reset:
            raise ValueError("dayflow_pipeline.daily_reset.boundary_hour_local is required.")
        return int(daily_reset["boundary_hour_local"])
    except Exception as e:
        logger.error("Failed reading dayflow boundary hour config: %s", e)
        logger.debug("dayflow boundary-hour config read exception details", exc_info=True)
        raise


def _expected_calendar_needs_init(*, payload: Dict[str, Any], now_local: datetime, boundary_date_local: str) -> tuple[bool, str]:
    if not payload:
        return True, "missing_resource"
    if not isinstance(payload, dict):
        raise ValueError(f"resource_expected_calendar must be a JSON object, got {type(payload).__name__}")

    payload_date = str(payload.get("date") or "").strip()
    if not payload_date:
        return True, "missing_date"
    if payload_date == boundary_date_local:
        return False, "fresh"

    boundary_hour = _boundary_hour_local()
    if now_local.hour >= boundary_hour:
        return True, f"stale_after_boundary(payload_date={payload_date},boundary_date={boundary_date_local})"
    return False, "pre_boundary_allow_previous"


def _resolve_expected_schedule() -> List[Dict[str, Any]]:
    from app.assistant.pipelines.context import boundary_date_local_str

    now_local = get_local_time()
    boundary_date_local = boundary_date_local_str(now_local)
    expected_calendar = _load_expected_calendar()
    needs_init, reason = _expected_calendar_needs_init(
        payload=expected_calendar,
        now_local=now_local,
        boundary_date_local=boundary_date_local,
    )
    if needs_init:
        logger.info("Initializing resource_expected_calendar (%s).", reason)
        expected_calendar = _seed_expected_calendar_from_api(now_utc=datetime.now(timezone.utc))

    expected_schedule = expected_calendar.get("expected_schedule") if isinstance(expected_calendar, dict) else None
    if not isinstance(expected_schedule, list):
        raise ValueError("resource_expected_calendar.expected_schedule must be a list.")
    if not expected_schedule:
        raise ValueError("resource_expected_calendar.expected_schedule must not be empty.")
    return expected_schedule


def _load_dayflow_requests(*, now_utc: datetime) -> List[Dict[str, Any]]:
    """Load unprocessed user delegation requests from the unified log.

    Only returns requests that haven't been picked up yet (no
    ``ingested_at`` in metadata). After the item is built and passes
    through triage, ``_mark_dayflow_request_ingested`` stamps the row
    so it isn't re-loaded on the next tick.
    """
    import json
    from sqlalchemy import select
    from app.models.db_manager import get_db_manager
    from app.assistant.database.db_handler import UnifiedLog2026

    results: List[Dict[str, Any]] = []
    with get_db_manager().read_session() as session:
        rows = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.source == "dayflow_request")
            .where(UnifiedLog2026.room_id == "dayflow_orchestrator")
        ).scalars().all()

        for row in rows:
            meta = {}
            if row.metadata_json:
                try:
                    meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(meta, dict):
                continue
            # Skip already-ingested requests.
            if meta.get("ingested_at"):
                continue
            results.append({
                "request_id": str(meta.get("request_id") or row.id or "").strip(),
                "summary": str(meta.get("summary") or row.message or "").strip(),
                "created_at": str(meta.get("created_at") or "").strip(),
                "request_type": str(meta.get("request_type") or "user_delegation").strip(),
                "_db_row_id": row.id,
            })

    logger.info("_load_dayflow_requests: found %d unprocessed request(s).", len(results))
    return results


def mark_dayflow_requests_ingested(requests: List[Dict[str, Any]]) -> None:
    """Stamp ingested_at on processed dayflow requests so they aren't re-loaded."""
    if not requests:
        return
    import json
    from sqlalchemy import select, update
    from app.models.db_manager import get_db_manager
    from app.assistant.database.db_handler import UnifiedLog2026

    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_manager().transaction(op="dayflow.mark_requests_ingested") as session:
        for req in requests:
            row_id = req.get("_db_row_id")
            if not row_id:
                continue
            row = session.execute(
                select(UnifiedLog2026).where(UnifiedLog2026.id == row_id)
            ).scalar_one_or_none()
            if not row:
                continue
            meta = {}
            if row.metadata_json:
                try:
                    meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta["ingested_at"] = now_iso
            session.execute(
                update(UnifiedLog2026)
                .where(UnifiedLog2026.id == row_id)
                .values(metadata_json=json.dumps(meta, ensure_ascii=False))
            )
    logger.info("mark_dayflow_requests_ingested: stamped %d request(s).", len(requests))


def _build_delegation_message(*, request: Dict[str, Any], now_utc: datetime) -> Message:
    """Convert a user delegation request into a dayflow intake Message."""
    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("Delegation request missing request_id.")
    summary = str(request.get("summary") or "").strip()
    if not summary:
        raise ValueError(f"Delegation request {request_id} missing summary.")

    created_at_raw = str(request.get("created_at") or "").strip()
    if created_at_raw:
        ts = parse_iso_utc_strict(created_at_raw, label="delegation.created_at")
    else:
        ts = now_utc

    # Build as a dayflow input item — same shape as emails/tickets.
    # Triage decides importance, state, actionability.
    item_id = f"delegate:{request_id.split(':')[-1]}" if ":" in request_id else f"delegate:{request_id}"

    metadata: Dict[str, Any] = {
        "item_id": item_id,
        "source_type": "user_request",
        "event_type": "delegated_from_master_room",
        "created_at": ts.isoformat(),
        "summary": summary,
        "importance": "high",
        "actionability": "actionable",
        "state": "new",
        "state_reason": "delegated_from_master_room",
        "last_reviewed_at": now_utc.isoformat(),
        "cooldown_until": None,
        "linked_item_ids": [],
    }

    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sub_data_type=["dayflow_orchestrator", "input_layer", "new", "user_request"],
        sender="master_room",
        content=summary,
        timestamp=ts,
        room_id="dayflow_orchestrator",
        metadata=metadata,
    )


def build_dayflow_input_messages(
    *, now_utc: Optional[datetime] = None, max_tickets: int = 50,
) -> tuple[List[Message], List[Dict[str, Any]]]:
    """
    Build normalized Message items for dayflow orchestrator stateful input.

    Returns (messages, ingested_requests) where ingested_requests is a list
    of raw request dicts that were loaded from the DB and should be marked
    as ingested after the pipeline processes them.
    """
    now = now_utc or datetime.now(timezone.utc)
    output: List[Message] = []

    from app.assistant.ticket_manager import get_ticket_manager
    from app.assistant.ticket_manager.ticket import TicketType

    _ACTIVE_TICKET_STATES = [TicketState.PENDING, TicketState.PROPOSED, TicketState.SNOOZED]
    _DAYFLOW_TICKET_TYPES = [
        TicketType.DAYFLOW_ADVICE,
        TicketType.DAYFLOW_NOTIFY,
        TicketType.DAYFLOW_DECISION,
    ]
    tm = get_ticket_manager()
    for tt in _DAYFLOW_TICKET_TYPES:
        tickets = tm.get_tickets(
            states=_ACTIVE_TICKET_STATES,
            ticket_type=tt.value,
            limit=max_tickets,
            order_by="updated_at",
        )
        for ticket in tickets:
            output.append(_build_ticket_message(ticket, now))

    # Calendar events are NOT ingested as dayflow items. The planner
    # already sees them via resource_expected_calendar and creates
    # plans from them. Ingesting them as items just creates noise.

    email_events = _load_emails_from_event_repo(now_utc=now)
    for email_data in email_events:
        output.append(_build_email_message(email_data=email_data, now_utc=now))

    # User delegation requests from master_room.
    delegation_requests = _load_dayflow_requests(now_utc=now)
    for req in delegation_requests:
        output.append(_build_delegation_message(request=req, now_utc=now))

    output.sort(key=lambda m: getattr(m, "timestamp", now))
    return output, delegation_requests


def serialize_messages(messages: List[Message]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for msg in messages:
        if hasattr(msg, "model_dump"):
            serialized.append(msg.model_dump(mode="json"))
            continue
        serialized.append(msg.dict())
    return serialized
