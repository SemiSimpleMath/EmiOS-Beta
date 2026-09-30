from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir, get_resources_dir
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time, parse_iso_utc_strict, utc_to_local

logger = get_logger(__name__)


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _build_item_id(prefix: str, *parts: str) -> str:
    normalized = "|".join(str(p or "").strip().lower() for p in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


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
    Convert an email record (email_pods.email_record) into a dayflow Message.

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
    # First ~2000 chars of the email body — enough to capture the salutation
    # ("Dear Parent of <name>", account IDs, first paragraph of context) which
    # is what disambiguates the subject person for transactional/school emails.
    body_full = str(email_data.get("body") or "").strip()
    body_excerpt = body_full[:2000]

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
        # utc_to_local respects TIMEZONE env var; bare .astimezone() would
        # use the host tz, which can differ from the user's configured tz.
        "created_at_local": utc_to_local(email_timestamp).strftime("%I:%M %p"),
        "summary": f"{sender}: {subject}",
        "importance": inferred_importance,
        "actionability": "actionable" if action_items else "context_only",
        # Wake context only (state_mover), never steward intake: the brain reads every email.
        "state": "artifact",
        "state_reason": "email_wake_context",
        "last_reviewed_at": now_utc.isoformat(),
        "cooldown_until": None,
        "linked_item_ids": [],
        "linked_email_unified_id": raw_email_unified_id,
        # The pod holding the FULL body, so an agent has a handle it can actually open. The email
        # IS that pod (email_pods.py), so the handle is exact. Prompts used to reference an item
        # only by `short_id`, an integer no tool accepts and which is not unique: on 2026-09-13 a
        # worker told its answer was in "newsletter [7667]" asked the user three times and then
        # rebuilt from the open web what was sitting in a 1,978-character pod the whole time.
        "pod_id": email_data["pod_id"],
        "email_uid": uid,
        "email_account_id": account_id,
        "email_thread_id": str(email_data.get("thread_id") or "").strip(),
        "email_sender": sender,
        "email_subject": subject,
        "email_summary": summary,
        "email_body_excerpt": body_excerpt,
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


def _build_pod_message(*, pod: Any, now_utc: datetime) -> Message:
    """Convert a Pod into a dayflow input Message.

    Used by `_ingest_pods` for pod kinds the dayflow access.json
    allowlist permits (camera-derived image pods today; extensible
    to email pods, document pods, etc. as those land).

    The pod's metadata.captured_at_utc anchors the timestamp when
    available (so a doorbell ring at 3:42 PM doesn't time-shift to
    "now" if the resolver caught up later). Falls back to pod
    creation time, then now.
    """
    metadata = dict(pod.metadata) if isinstance(pod.metadata, dict) else {}
    pod_kind = str(getattr(pod, "kind", "") or "").strip()
    source_kind = str(metadata.get("source_kind") or "").strip()
    camera_name = str(metadata.get("camera_name") or "").strip()
    one_liner = str(getattr(pod, "one_liner", "") or "").strip()
    body = str(getattr(pod, "body", "") or "").strip()

    item_id = f"dayflow_pod:{pod.pod_id}"

    captured_at_str = str(metadata.get("captured_at_utc") or "").strip()
    pod_timestamp = now_utc
    if captured_at_str:
        try:
            parsed = datetime.fromisoformat(captured_at_str.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            pod_timestamp = parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    elif getattr(pod, "created_at", None):
        try:
            ts = pod.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            pod_timestamp = ts.astimezone(timezone.utc)
        except Exception:
            pass

    # Importance map. Anything that made it past pod_policy is at least
    # medium by definition. Bedroom emergencies escalate to high; ordinary
    # camera frames stay medium; everything else low.
    importance_label = "medium"
    if metadata.get("is_emergency") is True:
        importance_label = "high"
    elif source_kind == "ring_doorbell_significant":
        importance_label = "medium"
    elif source_kind == "ring_bedroom_notable":
        importance_label = "medium"

    summary_text = one_liner or body[:140] or f"{source_kind or pod_kind} pod"
    content_parts: List[str] = []
    if camera_name:
        content_parts.append(f"Camera ({camera_name}): {summary_text}")
    elif source_kind:
        content_parts.append(f"{source_kind}: {summary_text}")
    else:
        content_parts.append(summary_text)
    content = ". ".join(content_parts)

    item_metadata: Dict[str, Any] = {
        "item_id": item_id,
        "source_type": "pod",
        "event_type": "pod_inbound",
        "created_at": pod_timestamp.isoformat(),
        "created_at_local": utc_to_local(pod_timestamp).strftime("%I:%M %p"),
        "summary": summary_text,
        "importance": importance_label,
        "actionability": "context_only",
        "state": "new",
        "state_reason": "pod_ingested",
        "last_reviewed_at": now_utc.isoformat(),
        "cooldown_until": None,
        "linked_item_ids": [],
        "pod_id": pod.pod_id,
        "pod_kind": pod_kind,
        "pod_source_kind": source_kind,
        "pod_one_liner": one_liner,
        "pod_body_excerpt": body[:2000],
        "camera_id": metadata.get("camera_id"),
        "camera_name": camera_name,
        "captured_at_utc": captured_at_str,
    }
    # Carry through scalar fields from pod metadata so downstream agents
    # don't need to re-fetch the pod for filtering. Skip nested types.
    for k, v in metadata.items():
        if k in item_metadata:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            item_metadata[f"pod_meta_{k}"] = v

    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sub_data_type=["dayflow_orchestrator", "input_layer", "new", "pod", "dayflow_artifact"],
        sender="pod_ingest",
        content=content,
        timestamp=pod_timestamp,
        room_id="dayflow_orchestrator",
        metadata=item_metadata,
    )


def _load_todays_emails(*, now_utc: datetime) -> List[Dict[str, Any]]:
    """Today's important emails (email_parser importance >= 5, received since local midnight), read
    from the email pods — the only email store."""
    from app.assistant.pod_store.email_pods import recent_emails

    now_local = utc_to_local(now_utc)
    today_midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return recent_emails(received_since=today_midnight_local.astimezone(timezone.utc))


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
    write_json_atomic(expected_calendar_path, payload)
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



def serialize_messages(messages: List[Message]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for msg in messages:
        if hasattr(msg, "model_dump"):
            serialized.append(msg.model_dump(mode="json"))
            continue
        serialized.append(msg.dict())
    return serialized
