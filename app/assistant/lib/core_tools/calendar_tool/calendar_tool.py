# calendar_tools.py
from datetime import datetime, timedelta, timezone

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.event_node_cleanup import cascade_delete_children, cleanup_event_node
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, Message
from app.assistant.utils.time_utils import parse_time_string, normalize_google_event_times, get_local_timezone
from app.assistant.lib.core_tools.calendar_tool.utils.google_calendar import (
    authenticate_google_api,
    create_event,
    create_repeating_event,
    get_events,
    search_event_by_name,
    edit_event,
    delete_event,
    delete_recurring_event,
)

from app.assistant.ServiceLocator.service_locator import DI

from app.assistant.utils.time_utils import local_to_utc, utc_to_local
from typing import Any, Dict, Optional
from pathlib import Path

# Import the repository manager class
from app.assistant.event_repository.event_repository import EventRepositoryManager

# Setup Logging
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Define Valid RRULE Parameters
VALID_FREQ = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
VALID_BYDAY = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}

# Valid flexibility values
VALID_FLEXIBILITY = {"fixed", "flexible", "soft_block", "aspirational"}


def _extract_flexibility(event: Dict[str, Any]) -> str:
    """
    Extract flexibility from Google Calendar's extendedProperties.
    Returns 'fixed' as default if not set.
    """
    try:
        extended_props = event.get('extendedProperties', {})
        private_props = extended_props.get('private', {})
        flexibility = private_props.get('flexibility', 'fixed')
        return flexibility if flexibility in VALID_FLEXIBILITY else 'fixed'
    except Exception:
        return 'fixed'


def _extract_blocking(event: Dict[str, Any]) -> bool:
    """
    Extract blocking from Google Calendar's transparency field.
    Returns True (opaque/busy) or False (transparent/free).
    """
    try:
        transparency = event.get('transparency', 'opaque')
        return transparency != 'transparent'
    except Exception:
        return True


def _extract_meeting_link(event: Dict[str, Any]) -> str:
    """The conference JOIN url (Zoom / Google Meet) from structured
    conferenceData entry points, else the legacy hangoutLink. Empty string when
    none — Gmail-auto-created events carry a placeholder description, so the join
    link lives only in these structured fields, not the description.
    """
    conf = event.get("conferenceData")
    if isinstance(conf, dict):
        for ep in (conf.get("entryPoints") or []):
            if isinstance(ep, dict) and ep.get("entryPointType") == "video":
                uri = str(ep.get("uri") or "").strip()
                if uri:
                    return uri
    return str(event.get("hangoutLink") or "").strip()


def format_rrule(
    freq: str, 
    start: str, 
    byday: Optional[str] = None, 
    until: Optional[str] = None,
    bymonth: Optional[str] = None,
    bymonthday: Optional[str] = None,
    count: Optional[str] = None
) -> str:
    """
    Formats an RRULE string using UTC for the UNTIL parameter.
    Preserves BYMONTH, BYMONTHDAY, and other common RRULE components.
    """
    freq = freq.upper()
    if freq not in VALID_FREQ:
        raise ValueError(f"Invalid FREQ value: {freq}. Must be one of {VALID_FREQ}.")

    rrule = f"RRULE:FREQ={freq}"

    if byday:
        days = [day.strip().upper() for day in byday.split(',')]
        invalid_days = [day for day in days if day not in VALID_BYDAY]
        if invalid_days:
            raise ValueError(f"Invalid BYDAY value(s): {', '.join(invalid_days)}. Must be among {VALID_BYDAY}.")
        rrule += f";BYDAY={','.join(days)}"
    
    if bymonth:
        # BYMONTH: 1-12 (comma-separated list allowed)
        rrule += f";BYMONTH={bymonth}"
    
    if bymonthday:
        # BYMONTHDAY: day of month, can be negative (e.g., -1 for last day)
        rrule += f";BYMONTHDAY={bymonthday}"
    
    if count:
        # COUNT: number of occurrences
        rrule += f";COUNT={count}"

    if until:
        # Parse using the existing helper and then force UTC conversion
        parsed_until = parse_time_string(until)
        if not parsed_until:
            raise ValueError(f"Invalid UNTIL date format: {until}")
        from datetime import timezone as dt_timezone
        until_utc = parsed_until.astimezone(dt_timezone.utc)
        formatted_until = until_utc.strftime('%Y%m%dT%H%M%SZ')
        rrule += f";UNTIL={formatted_until}"
        logger.debug(f"Formatted UNTIL: {formatted_until}")

    logger.debug(f"Final RRULE: {rrule}")
    return rrule


class CalendarTool(BaseTool):
    """
    Tool to interact with Google Calendar for creating, fetching, updating, and deleting events.
    Also synchronizes events with the local repository.
    """

    def __init__(self):
        super().__init__('calendar_tool')
        self.service = None
        # Initialize the repository manager
        self.repo_manager = EventRepositoryManager()

    def init_credentials(self):
        if self.service is None:  # Only authenticate if not already initialized

            self.service = authenticate_google_api()

    def _to_repo_calendar_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(event, dict):
            raise ValueError(f"Expected Google event dict, got {type(event)}")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("Google event is missing required 'id'.")

        start_utc, end_utc = normalize_start_end(event)
        if not start_utc or not end_utc:
            raise ValueError(f"Google event '{event_id}' missing parseable start/end.")

        is_recurring = "recurringEventId" in event or bool(event.get("recurrence"))
        recurring_event_id = event.get("recurringEventId")
        recurrence_rule = event.get("recurrence")
        start_info = event.get("start", {})
        is_all_day = isinstance(start_info, dict) and ("date" in start_info) and ("dateTime" not in start_info)

        return {
            "id": event_id,
            "summary": event.get("summary", "No Title"),
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "start": start_utc,
            "end": end_utc,
            "link": event.get("htmlLink"),
            "meeting_link": _extract_meeting_link(event),
            "is_recurring": is_recurring,
            "is_all_day": is_all_day,
            "recurring_event_id": recurring_event_id,
            "recurrence_rule": recurrence_rule,
            "flexibility": _extract_flexibility(event),
            "blocking": _extract_blocking(event),
            "participants": [
                {
                    "email": attendee.get("email"),
                    "response_status": attendee.get("responseStatus"),
                }
                for attendee in event.get("attendees", [])
            ],
            "data_type": "calendar",
        }

    def execute(self, tool_message: 'ToolMessage') -> ToolMessage:
        """
        Initializes credentials, converts incoming time fields to UTC,
        and then dispatches the tool message to the appropriate handler.
        """
        request_id = tool_message.request_id

        logger.debug(f"Received tool_message: {tool_message}")
        try:
            # Ensure credentials/service are initialized before doing any work.
            self.init_credentials()
            if self.service is None:
                # Most common cause: OAuth token expired/revoked (invalid_grant) during refresh.
                # Do not continue; downstream calls will produce noisy NoneType errors.
                return self.publish_error(
                    tool_message,
                    (
                        "Google Calendar authentication failed (token expired or revoked). "
                        "Re-authenticate via the app's Google OAuth UI to regenerate the token "
                        "(you can also open /oauth/google/start)."
                    ),
                    request_id,
                )

            logger.info("Starting CalendarTool execution.")
            arguments = tool_message.tool_data.get('arguments', {})
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'

            if not tool_name:
                raise ValueError("No tool_name specified.")

            # Convert time fields from local to UTC using the global local timezone.
            # Keys that likely represent time values.
            for key in ['start', 'end', 'start_date', 'end_date']:
                if key in arguments and arguments[key]:
                    # local_to_utc uses the global config for local timezone.
                    original = arguments[key]
                    # Convert to UTC and format as RFC3339 with 'Z' suffix
                    utc_dt = local_to_utc(arguments[key])
                    iso_str = utc_dt.isoformat()
                    # Ensure timezone is present - if naive, add 'Z'
                    if utc_dt.tzinfo is None:
                        logger.warning(f"Converted datetime for {key} is naive! Adding 'Z' suffix.")
                        arguments[key] = iso_str + 'Z'
                    else:
                        arguments[key] = iso_str.replace('+00:00', 'Z')
                    logger.debug(f"Converted {key}: {original} -> {arguments[key]} (tzinfo={utc_dt.tzinfo})")

            handler_method = getattr(self, f"handle_{tool_name}", None)
            if not handler_method:
                raise ValueError(f"Unsupported tool_name '{tool_name}'.")

            tool_result = handler_method(arguments)
            if request_id:
                self.publish_result(tool_message, tool_result, request_id)
            else:
                return tool_result

        except Exception as e:
            logger.error("Error in CalendarTool: %s", e)
            logger.debug("error in CalendarTool exception details", exc_info=True)
            return self.publish_error(tool_message, str(e), request_id)

    def publish_result(self, tool_message: 'ToolMessage', tool_result: Any, request_id: str) -> ToolMessage:
        tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'
        result_msg = ToolMessage(
            data_type='tool_result',
            sender='CalendarTool',
            receiver=tool_message.sender,
            task=tool_message.task,
            tool_name=tool_name,
            tool_data=tool_message.tool_data,
            tool_result=tool_result,
            group_id=tool_message.group_id,
            request_id=request_id
        )

        return result_msg

    def publish_error(self, tool_message: 'ToolMessage', error_message: str, request_id: str) -> ToolMessage:
        tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or "calendar_tool"
        error_result = make_tool_error(
            error_code="calendar_publish_error",
            message=error_message,
            abort_policy="abort_tool",
            retryable=False,
        )
        result_msg = ToolMessage(
            data_type='tool_result',
            sender='CalendarTool',
            receiver=tool_message.sender,
            task=tool_message.task,
            tool_name=tool_name,
            tool_data=tool_message.tool_data,
            tool_result=error_result,
            group_id=tool_message.group_id,
            request_id=request_id
        )

        return result_msg

    def handle_create_calendar_event(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Creates a single calendar event and writes it to the repository.
        Expects 'start' and 'end' in local time; they are already converted to UTC.
        """
        try:
            event_name = arguments.get('event_name')
            start = arguments.get('start')
            end = arguments.get('end')
            description = arguments.get('description')
            location = arguments.get('location')
            link = arguments.get('link')
            participants = arguments.get('participants')
            flexibility = arguments.get('flexibility', 'fixed')
            blocking = arguments.get('blocking', True)

            time_zone = "UTC"
            calendar_name = arguments.get('calendar_name', 'primary')

            # Build attendees list from participants
            attendees = []
            if isinstance(participants, list):
                for p in participants:
                    email = p.get('email')
                    name = p.get('name') or p.get('displayName') or ""
                    if email:
                        attendee = {'email': email}
                        if name:
                            attendee['displayName'] = name
                        attendees.append(attendee)

            event = create_event(self.service, {
                'event_name': event_name,
                'start': start,
                'end': end,
                'time_zone': time_zone,
                'description': description,
                'location': location,
                'link': link,
                'participants': attendees,
                'flexibility': flexibility,
                'blocking': blocking,
            })
            event_id = event.get('id')
            if not event:
                return make_tool_error(
                    error_code="calendar_event_not_created",
                    message="Error, event not created.",
                    abort_policy="abort_tool",
                    retryable=False,
                )

            repo_event = self._to_repo_calendar_event(event)
            self.repo_manager.store_event(event_id, event_data=repo_event, data_type="calendar")

            repo_msg = Message(
                data_type="repo_update",
                sender="CalenderTool",
                receiver=None,
                data={
                    "data_type": "calendar_task",
                    "action": "create",
                    "entity_id": event_id
                }
            )
            event_hub = DI.event_hub
            repo_msg.event_topic = 'repo_update'
            event_hub.publish(repo_msg)

            return ToolResult(result_type="success", content="Event created successfully")
        except Exception as e:
            logger.error(f"Failed to handle_create_calendar_event: {e}")
            logger.debug("Calendar create event exception details", exc_info=True)
            return make_tool_error(
                error_code="calendar_create_event_failed",
                message=f"Error, event not created. {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def handle_create_repeating_calendar_event(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Creates a repeating calendar event and writes it to the repository.
        Expects 'start' and 'end' in local time; they are already converted to UTC.
        """
        try:
            logger.debug(f"Input Arguments: {arguments}")
            event_name = arguments.get('event_name')
            start = arguments.get('start')
            end = arguments.get('end')
            calendar_name = arguments.get('calendar_name', 'primary')
            all_day = arguments.get('all_day', False)
            recurrence_rule_input = arguments.get('recurrence_rule')
            time_zone = "UTC"
            description = arguments.get('description')
            location = arguments.get('location')
            link = arguments.get('link')
            participants = arguments.get('participants')
            flexibility = arguments.get('flexibility', 'fixed')
            blocking = arguments.get('blocking', True)

            if not all([event_name, start, end, recurrence_rule_input]):
                raise ValueError("Missing required fields for creating a repeating event.")

            # strip leading "RRULE:" if present
            if recurrence_rule_input.upper().startswith("RRULE:"):
                recurrence_rule_input = recurrence_rule_input[6:]

            # parse and re‑format RRULE
            parts = dict(item.split('=') for item in recurrence_rule_input.split(';'))
            freq = parts.get('FREQ')
            if not freq:
                raise ValueError("RRULE must include a 'FREQ' parameter.")
            formatted_rrule = format_rrule(
                freq=freq,
                start=start,
                byday=parts.get('BYDAY'),
                until=parts.get('UNTIL'),
                bymonth=parts.get('BYMONTH'),
                bymonthday=parts.get('BYMONTHDAY'),
                count=parts.get('COUNT')
            )

            # build attendees list from your list of dicts
            attendees = []
            if isinstance(participants, list):
                for p in participants:
                    email = p.get('email')
                    name = p.get('name') or ""
                    if email:
                        attendees.append({
                            "email": email,
                            "displayName": name
                        })

            event_dict = {
                "event_name": event_name,
                "start": start,
                "end": end,
                "time_zone": time_zone,
                "all_day": all_day,
                "recurrence_rule": formatted_rrule,
                "description": description,
                "location": location,
                "link": link,
                "participants": attendees,
                "flexibility": flexibility,
                "blocking": blocking,
            }
            logger.debug(f"Prepared Event Dictionary: {event_dict}")

            # Resolve calendar name to calendar ID
            from app.assistant.lib.core_tools.calendar_tool.utils.google_calendar import get_calendar_by_name
            calendar_id = get_calendar_by_name(self.service, calendar_name)
            if not calendar_id:
                logger.warning(f"Calendar '{calendar_name}' not found. Falling back to 'primary'.")
                calendar_id = 'primary'
            
            logger.info(f"Creating event in calendar: {calendar_name} (ID: {calendar_id})")

            event = create_repeating_event(self.service, event_dict, calendarId=calendar_id)
            if not event:
                raise ValueError("Failed to create the repeating event.")

            event_id = event.get('id')
            repo_event = self._to_repo_calendar_event(event)
            self.repo_manager.store_event(event_id, event_data=repo_event, data_type="calendar")

            repo_msg = Message(
                data_type="repo_update",
                sender="CalendarTool",
                receiver=None,
                data={
                    "data_type": "calendar_task",
                    "action": "create_repeating",
                    "entity_id": event_id
                }
            )
            repo_msg.event_topic = 'repo_update'
            DI.event_hub.publish(repo_msg)

            return ToolResult(result_type="success", content="Repeating event created successfully")

        except Exception as e:
            logger.error(f"Failed to handle_create_repeating_calendar_event: {e}")
            return ToolResult(
                result_type="error",
                content=f"Error, event not created. {e}"
            )

    def handle_get_calendar_events(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Fetches events and optionally syncs them into the repository.

        Expects:
          - start_date, end_date: already converted to UTC ISO by execute()
          - calendar_names: optional list of calendar names
          - single_events: whether to expand recurring instances
          - repo_update: if True, update EventRepository (used by scheduler)
                         if False, read only (used by agents)
        """
        max_results = arguments.get("max_results", 100)
        start_iso = arguments.get("start_date")
        end_iso = arguments.get("end_date")
        calendar_names = arguments.get("calendar_names")
        single_events = arguments.get("single_events", True)

        # New unified flag; keep backward compatibility with old sync_to_repo
        repo_update = arguments.get("repo_update", arguments.get("sync_to_repo", False))

        # Optional case-insensitive substring filter against the event summary
        # (title). Lets a planner ask "Friday Night Meats" without pulling
        # back every event in the window and scanning manually. Applied
        # post-fetch — Google Calendar's API doesn't support server-side
        # title filtering for recurring-instance expansion.
        name_contains_raw = arguments.get("event_name_contains")
        name_contains = (
            str(name_contains_raw).strip().lower()
            if isinstance(name_contains_raw, str) and name_contains_raw.strip()
            else None
        )

        if not start_iso or not end_iso:
            from app.assistant.utils.time_utils import get_local_time
            local_now = get_local_time()
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            if not start_iso:
                start_iso = local_to_utc(local_midnight.isoformat()).isoformat().replace('+00:00', 'Z')
            if not end_iso:
                end_of_range = local_midnight + timedelta(days=7, seconds=-1)
                end_iso = local_to_utc(end_of_range.isoformat()).isoformat().replace('+00:00', 'Z')

        logger.info(
            f"Fetching calendar events from {start_iso} to {end_iso} "
            f"max_results={max_results}, repo_update={repo_update}"
        )

        # If no specific calendar_names, resolve all calendars
        calendar_ids = None
        if not calendar_names:
            all_calendar_ids = []
            page_token = None
            while True:
                resp = self.service.calendarList().list(pageToken=page_token).execute()
                all_calendar_ids += [cal["id"] for cal in resp.get("items", [])]
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            calendar_ids = all_calendar_ids

        events = get_events(
            service=self.service,
            start_str=start_iso,
            end_str=end_iso,
            calendar_names=calendar_names,
            calendar_ids=calendar_ids,
            max_results=max_results,
            single_events=single_events,
        )

        if events is None:
            logger.info("No events found in the specified time range.")
            events = []

        # Apply the optional name-contains post-filter. Applied BEFORE repo
        # writes so stored events remain faithful to the raw calendar data;
        # the filter is a query-time concern, not a persistence concern.
        if name_contains is not None:
            before = len(events)
            events = [
                e for e in events
                if name_contains in str(e.get("summary") or "").lower()
            ]
            logger.info(
                "event_name_contains=%r filter: %d -> %d events",
                name_contains, before, len(events),
            )

        server_ids = set()
        result_events = []

        for event in events:
            event_id = event.get("id")
            is_recurring = "recurringEventId" in event
            recurring_event_id = event.get("recurringEventId")
            recurrence_rule = event.get("recurrence")

            # For recurring instances, try to pull recurrence from parent
            if is_recurring and recurring_event_id and not recurrence_rule:
                try:
                    parent_event = self.service.events().get(
                        calendarId="primary", eventId=recurring_event_id
                    ).execute()
                    recurrence_rule = parent_event.get("recurrence")
                except Exception as e:
                    logger.warning(
                        f"Failed to retrieve parent event {recurring_event_id}: {e}"
                    )

            start_utc, end_utc = normalize_start_end(event)
            
            # Detect all-day events: Google Calendar uses 'date' for all-day, 'dateTime' for timed
            start_info = event.get("start", {})
            is_all_day = "date" in start_info and "dateTime" not in start_info

            parsed_event = {
                "id": event_id,
                "summary": event.get("summary", "No Title"),
                "description": event.get("description", ""),
                "location": event.get("location", ""),
                "start": start_utc,
                "end": end_utc,
                "link": event.get("htmlLink"),
                "meeting_link": _extract_meeting_link(event),
                "is_recurring": is_recurring,
                "is_all_day": is_all_day,
                "recurring_event_id": recurring_event_id,
                "recurrence_rule": recurrence_rule,
                "flexibility": _extract_flexibility(event),
                "blocking": _extract_blocking(event),
                "participants": [
                    {
                        "email": attendee.get("email"),
                        "response_status": attendee.get("responseStatus"),
                    }
                    for attendee in event.get("attendees", [])
                ],
                "created": event.get("created", ""),
                "updated": event.get("updated", ""),
                "data_type": "calendar",
            }

            result_events.append(parsed_event)
            server_ids.add(event_id)

        # Batch write to database AFTER all processing is complete
        # This minimizes DB lock time
        if repo_update and result_events:
            logger.debug(f"Batch writing {len(result_events)} calendar events to repo")
            for parsed_event in result_events:
                self.repo_manager.store_event(
                    parsed_event["id"],
                    event_data=parsed_event,
                    data_type="calendar",
                )
            # Enforce stable 7 day window: repo will match current fetch set
            self.repo_manager.sync_events_with_server(list(server_ids), "calendar")

        # Create human-readable summary. Agents only see `content`, not
        # `data_list`, so the summary must include enough per-event detail
        # for a planner to answer questions like "what's the Zoom link for
        # Friday Night Meats?" without extra round-trips. Include title +
        # time + location + a short description snippet (which is where
        # Google Calendar stores the conference join URL for recurring
        # meetings).
        event_count = len(result_events)
        recurring_count = sum(1 for e in result_events if e.get('is_recurring'))
        header = f"Found {event_count} calendar event{'s' if event_count != 1 else ''}"
        if recurring_count > 0:
            header += f" ({recurring_count} recurring)"
        header += f" from {start_iso} to {end_iso}."

        # Deduplicate recurring instances by title — same event repeated
        # across weeks should only appear once in the summary.
        seen_titles: set[str] = set()
        lines: list[str] = [header]
        for ev in result_events:
            title = str(ev.get("summary") or "").strip() or "(no title)"
            key = title.lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            start = str(ev.get("start") or "").strip()
            location = str(ev.get("location") or "").strip()
            meeting_link = str(ev.get("meeting_link") or "").strip()
            description = str(ev.get("description") or "").strip()
            attendee_emails = [
                str(p.get("email") or "").strip()
                for p in (ev.get("participants") or [])
                if isinstance(p, dict) and str(p.get("email") or "").strip()
            ]
            line = f"- {title}"
            if start:
                line += f" @ {start}"
            if location:
                line += f"  location={location[:200]}"
            if meeting_link:
                line += f"  meeting_link={meeting_link}"
            # Attendees are the field agents most often need ("who's in X?") and
            # are small (a handful of emails) — render the FULL list. Omitting
            # them (data trapped in data_list, invisible in content) sent a
            # participant lookup down a brute-force email-mining path that
            # exhausted the manager's cycle budget (2026-06 Friday-Night-Meats).
            if attendee_emails:
                line += f"  attendees={', '.join(attendee_emails)}"
            if description:
                # Generous cap: normal descriptions render in full; only a rare
                # pasted wall-of-text gets clipped. The join URL is already lifted
                # into meeting_link, so clipping never drops it.
                snippet = description.replace("\n", " ").strip()
                line += f"  description={snippet[:1500]}"
            lines.append(line)

        summary = "\n".join(lines)

        return ToolResult(
            result_type="calendar_events",
            content=summary,
            data_list=result_events,
        )


    def handle_search_event(self, arguments: Dict[str, Any]) -> ToolResult:
        try:
            query = arguments.get('query')
            if not query:
                raise ValueError("Missing 'query' parameter for searching events.")

            events = search_event_by_name(self.service, query)
            result_events = []
            for event in events:
                event.update({'data_type': 'calendar'})
                result_events.append(event)
            return ToolResult(result_type="calendar", data_list=result_events)
        except Exception as e:
            logger.error(f"Failed to handle_search_event: {e}")
            logger.debug("Calendar search event exception details", exc_info=True)
            return make_tool_error(
                error_code="calendar_search_event_failed",
                message=f"Error, event search failed. {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def handle_update_calendar_event(self, arguments: Dict[str, Any]) -> ToolResult:
        try:
            # Filter out empty values
            arguments = {k: v for k, v in arguments.items() if v not in (None, "")}
            event_id = arguments.get("event_id")
            if not event_id:
                raise ValueError("Missing 'event_id' for updating event.")

            # Extract scope for recurring events (remove from arguments dict)
            scope = arguments.pop("scope", None)
            
            # Extract flexibility (stored in extendedProperties)
            flexibility = arguments.pop("flexibility", None)
            
            # Extract blocking (stored in transparency field)
            blocking = arguments.pop("blocking", None)

            # Convert times to local timezone (e.g., PST) using utc_to_local
            local_tz = get_local_timezone().key  # Should be 'America/Los_Angeles'
            for key in ["start", "end"]:
                if key in arguments:
                    original = arguments[key]
                    # Assuming original is an ISO time string, e.g., "2025-07-02T08:00:00Z"
                    converted = utc_to_local(original)
                    arguments[key] = {"dateTime": converted, "timeZone": local_tz}
                    logger.debug(f"Updated {key} for event update: {original} -> {arguments[key]}")

            # Ensure recurrence rule is explicitly replaced
            if "recurrence" in arguments:
                if not isinstance(arguments["recurrence"], list):
                    arguments["recurrence"] = [arguments["recurrence"]]

            # Add flexibility and blocking back to arguments for edit_event to handle
            if flexibility:
                arguments['flexibility'] = flexibility
            if blocking is not None:
                arguments['blocking'] = blocking

            # Call the edit_event function with scope for recurring event handling
            updated_event = edit_event(self.service, event_id, arguments, scope=scope)
            if not updated_event:
                raise ValueError("Failed to update the event.")

            # Normalize times from Google before storing, then store in canonical repo shape.
            updated_event = normalize_google_event_times(updated_event)
            repo_event = self._to_repo_calendar_event(updated_event)

            repo_msg = Message(
                data_type="repo_update",
                sender="CalenderTool",
                receiver=None,
                data={
                    "data_type": "calendar_task",
                    "action": "update",
                    "entity_id": event_id
                }
            )
            event_hub = DI.event_hub
            repo_msg.event_topic = 'repo_update'
            event_hub.publish(repo_msg)

            self.repo_manager.store_event(event_id, event_data=repo_event, data_type="calendar")

            return ToolResult(result_type="success", content=f"Event {event_id} updated successfully")
        except Exception as e:
            logger.error(f"Failed to handle_update_calendar_event: {e}")
            logger.debug("Calendar update event exception details", exc_info=True)
            return make_tool_error(
                error_code="calendar_update_event_failed",
                message=f"Error, event not updated. {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def handle_delete_calendar_event(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Delete a single calendar event.

        For a non-recurring event: deletes the event.
        For an instance of a recurring series (event_id has the "<base>_<UTC>Z"
        form): cancels just that occurrence; the series itself is unaffected.

        This tool NEVER deletes an entire recurring series. To remove every
        occurrence of a recurring event, use delete_repeating_calendar_event.

        cascade=True also deletes linked children (reminders, sub-events) from
        the local repository.
        """
        try:
            event_id = arguments.get('event_id')
            cascade = arguments.get('cascade', False)

            if not event_id:
                raise ValueError("Missing 'event_id' for deleting event.")

            deleted_children = []

            # Handle cascade deletion
            if cascade:
                deleted_children = cascade_delete_children('google_calendar', event_id, delete_source_item=self._delete_source_item, include_direct_children=True)

            success = delete_event(self.service, event_id)
            if not success:
                raise ValueError("Failed to delete the event.")

            repo_msg = Message(
                data_type="repo_update",
                sender="CalenderTool",
                receiver=None,
                data={
                    "data_type": "calendar_task",
                    "action": "delete",
                    "entity_id": event_id
                }
            )
            event_hub = DI.event_hub
            repo_msg.event_topic = 'repo_update'
            event_hub.publish(repo_msg)

            self.repo_manager.delete_event(event_id, data_type="calendar")
            
            # Clean up EventNode if it exists
            cleanup_event_node('google_calendar', event_id)

            if cascade and deleted_children:
                return ToolResult(
                    result_type="success", 
                    content=f"Event deleted successfully. Also deleted {len(deleted_children)} linked children: {deleted_children}"
                )
            return ToolResult(result_type="success", content="Event deleted successfully.")
        except Exception as e:
            logger.error(f"Failed to handle_delete_event: {e}")
            logger.debug("Calendar delete event exception details", exc_info=True)
            return make_tool_error(
                error_code="calendar_delete_event_failed",
                message=f"Error, event not deleted. {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def handle_delete_repeating_calendar_event(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Delete an entire recurring event series (every occurrence).

        event_id may be the series' base id or any single instance id — both are
        resolved to the base and deleted. Non-recurring events are rejected;
        use delete_calendar_event for those.

        This tool is destructive across the full series. Use only when the
        user has explicitly asked to cancel the whole recurring event.
        """
        try:
            event_id = arguments.get('event_id')
            cascade = arguments.get('cascade', False)

            if not event_id:
                raise ValueError("Missing 'event_id' for deleting recurring event.")

            deleted_children = []
            if cascade:
                deleted_children = cascade_delete_children('google_calendar', event_id, delete_source_item=self._delete_source_item, include_direct_children=True)

            success = delete_recurring_event(self.service, event_id)
            if not success:
                raise ValueError(
                    "Failed to delete the recurring event. The event may not be recurring; "
                    "use delete_calendar_event for single events."
                )

            repo_msg = Message(
                data_type="repo_update",
                sender="CalenderTool",
                receiver=None,
                data={
                    "data_type": "calendar_task",
                    "action": "delete",
                    "entity_id": event_id,
                }
            )
            event_hub = DI.event_hub
            repo_msg.event_topic = 'repo_update'
            event_hub.publish(repo_msg)

            self.repo_manager.delete_event(event_id, data_type="calendar")
            cleanup_event_node('google_calendar', event_id)

            if cascade and deleted_children:
                return ToolResult(
                    result_type="success",
                    content=f"Recurring event series deleted. Also deleted {len(deleted_children)} linked children: {deleted_children}",
                )
            return ToolResult(result_type="success", content="Recurring event series deleted successfully.")
        except Exception as e:
            logger.error(f"Failed to handle_delete_repeating_calendar_event: {e}")
            logger.debug("Calendar delete recurring event exception details", exc_info=True)
            return make_tool_error(
                error_code="calendar_delete_repeating_event_failed",
                message=f"Error, recurring event not deleted. {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def _delete_source_item(self, source: dict) -> str:
        """Delete an item from its source system."""
        try:
            source_system = source.get('source_system')
            source_id = source.get('source_id')
            
            if source_system == 'scheduler':
                # Delete from scheduler
                DI.scheduler.event_scheduler.delete_event(source_id)
                self.repo_manager.delete_event(source_id, data_type="scheduler")
                return f"scheduler:{source_id}"
            elif source_system == 'google_calendar':
                # Delete from calendar (recursive - be careful)
                delete_event(self.service, source_id)
                self.repo_manager.delete_event(source_id, data_type="calendar")
                return f"google_calendar:{source_id}"
            elif source_system == 'google_tasks':
                # TODO: Implement Google Tasks deletion
                return f"google_tasks:{source_id} (not deleted - not implemented)"
        except Exception as e:
            logger.error("Error deleting source item %s: %s", source, e)
            logger.debug("source item delete exception", exc_info=True)
        return None
    
def normalize_start_end(event: dict, calendar_timezone: str = None) -> (str, str):
    """
    Normalize event start and end into UTC ISO strings.

    If calendar_timezone is provided, it will be used for interpreting all-day events.
    Otherwise, falls back to system local timezone.
    """
    start_info = event.get("start", {})
    end_info = event.get("end", {})

    tzinfo = get_local_timezone()
    if calendar_timezone:
        try:
            from zoneinfo import ZoneInfo
            tzinfo = ZoneInfo(calendar_timezone)
        except Exception as e:
            logger.debug(f"Invalid calendar_timezone='{calendar_timezone}', falling back to local timezone: {e}", exc_info=True)

    def convert(field_info):
        if 'dateTime' in field_info:
            return parse_time_string(field_info['dateTime'])  # already tz-aware
        elif 'date' in field_info:
            local_midnight = datetime.strptime(field_info['date'], "%Y-%m-%d")
            local_midnight = local_midnight.replace(tzinfo=tzinfo)
            return local_midnight.astimezone(timezone.utc)
        else:
            return None

    start_dt = convert(start_info)
    end_dt = convert(end_info)

    return (
        start_dt.isoformat() if start_dt else None,
        end_dt.isoformat() if end_dt else None,
    )
