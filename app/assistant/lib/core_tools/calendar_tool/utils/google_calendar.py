import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

# Setup Logging

from app.assistant.utils.logging_config import get_logger
from app.assistant.lib.google_auth.account_ids import CALENDAR_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth.google_credentials import load_google_credentials

logger = get_logger(__name__)

# Define the scope
SCOPES = ['https://www.googleapis.com/auth/calendar']

## Google needs everything to be in the local timezone at the time of creation.  Otherwise there are problems with DST

from app.assistant.utils.time_utils import utc_to_local, get_local_timezone, to_rfc3339_z, utc_now


def convert_utc_to_local_event_times(event_dict, all_day=False):
    """
    Converts UTC start and end times in an event dictionary to local time.
    For all-day events, converts to date format instead of dateTime.

    Args:
        event_dict (dict): Dictionary containing event details, including 'start' and 'end' in UTC.
        all_day (bool): If True, creates all-day event using date format.

    Returns:
        dict: Updated event dictionary with 'start' and 'end' converted appropriately.
    """
    local_timezone = get_local_timezone().key  # Fetch local timezone (e.g., 'America/Los_Angeles')

    event_dict = event_dict.copy()  # Ensure we don't modify the original dictionary

    if all_day:
        # All-day events use 'date' format (YYYY-MM-DD) without time
        # Google Calendar expects end date to be exclusive (next day)
        if "start" in event_dict and event_dict["start"]:
            start_local = utc_to_local(event_dict["start"])
            event_dict["start"] = {
                "date": start_local.strftime("%Y-%m-%d")
            }
        
        if "end" in event_dict and event_dict["end"]:
            end_local = utc_to_local(event_dict["end"])
            event_dict["end"] = {
                "date": end_local.strftime("%Y-%m-%d")
            }
    else:
        # Timed events use 'dateTime' format with timezone
        if "start" in event_dict and event_dict["start"]:
            event_dict["start"] = {
                "dateTime": utc_to_local(event_dict["start"]).isoformat(),
                "timeZone": local_timezone
            }

        if "end" in event_dict and event_dict["end"]:
            event_dict["end"] = {
                "dateTime": utc_to_local(event_dict["end"]).isoformat(),
                "timeZone": local_timezone
            }

    return event_dict


def authenticate_google_api():
    """
    Authenticate the user and return the Google Calendar service.
    """
    try:
        creds = load_google_credentials(
            account_id=CALENDAR_GOOGLE_ACCOUNT_ID,
            required_scopes=SCOPES,
        )
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Failed to create Google Calendar service: {e}")
        return None


def get_calendar_by_name(service, calendar_name: str) -> Optional[str]:
    """
    Resolves a calendar name to its calendarId. Handles 'primary' as a special case.

    Args:
        service: Authenticated Google Calendar service instance.
        calendar_name (str): Name of the calendar to resolve.

    Returns:
        str: The calendarId if found, otherwise None.
    """

    try:
        if service is None:
            logger.error("Google Calendar service is not initialized (auth failed).")
            return None
        # Handle the special case for 'primary'
        if calendar_name.lower() == 'primary':
            return 'primary'

        # Fetch all calendars (handle pagination)
        calendar_list = []
        page_token = None
        while True:
            response = service.calendarList().list(pageToken=page_token).execute()
            calendar_list.extend(response.get('items', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break

        # Debug: List all available calendar summaries
        available_calendars = [calendar.get('summary', '').lower() for calendar in calendar_list]
        logger.info(f"Available calendars: {available_calendars}")

        # Match the calendar name (case-insensitive)
        for calendar in calendar_list:
            if calendar.get('summary', '').lower() == calendar_name.lower():
                logger.debug(f"Found calendar: {calendar['summary']} (ID: {calendar['id']})")
                return calendar['id']

        logger.warning(f"Calendar '{calendar_name}' not found.")
        return None
    except Exception as e:
        logger.error(f"Error resolving calendar name '{calendar_name}': {e}")
        return None


def create_event(service, event_dict, calendarId='primary'):
    """
    Create a single event on the Google Calendar with proper time zone handling.
    Supports extended properties for custom fields like 'flexibility'.
    Supports Google's native 'transparency' field via 'blocking' parameter.
    """
    summary = event_dict.get('event_name')
    start = event_dict.get('start')
    end = event_dict.get('end')
    description = event_dict.get('description')
    location = event_dict.get('location')
    link = event_dict.get('link')
    participants = event_dict.get('participants')
    flexibility = event_dict.get('flexibility')
    blocking = event_dict.get('blocking', True)

    if service is None:
        logger.error("Google Calendar service is not initialized (auth failed).")
        return None
    if not summary or not start or not end:
        logger.error("Event dictionary missing required fields.")
        return None

    # Convert UTC times to local time before sending to Google Calendar
    event_dict = convert_utc_to_local_event_times(event_dict)

    event = {
        'summary': summary,
        'start': event_dict["start"],  # Now correctly in local timezone
        'end': event_dict["end"],  # Now correctly in local timezone
    }

    # Add optional fields
    if description:
        event['description'] = description
    if location:
        event['location'] = location
    if link:
        desc = event.get('description', "")
        event['description'] = (desc + "\n" if desc else "") + f"Link: {link}"
    if participants and isinstance(participants, list):
        event['attendees'] = participants
    
    # Google's transparency field: opaque = blocking, transparent = non-blocking
    event['transparency'] = 'opaque' if blocking else 'transparent'
    
    # Add extended properties for custom fields (persists in Google Calendar)
    extended_props = {}
    if flexibility:
        extended_props['flexibility'] = flexibility
    if extended_props:
        event['extendedProperties'] = {'private': extended_props}

    try:
        created_event = service.events().insert(calendarId=calendarId, body=event).execute()
        logger.info(f"Event created: {created_event.get('htmlLink')}")
        return created_event
    except Exception as e:
        logger.error(f"An error occurred while creating the event: {e}")
        return None


def create_repeating_event(service, event_dict, calendarId='primary'):
    """
    Create a repeating event on Google Calendar and log full request details.
    Supports extended properties for custom fields like 'flexibility'.
    Supports Google's native 'transparency' field via 'blocking' parameter.
    Supports all-day events via 'all_day' parameter.
    Includes idempotency check to prevent duplicate event creation.
    """
    # Extract required fields
    summary = event_dict.get('event_name')
    start = event_dict.get('start')
    end = event_dict.get('end')
    recurrence_rule = event_dict.get('recurrence_rule')
    all_day = event_dict.get('all_day', False)

    # Extract optional fields
    description = event_dict.get('description')
    location = event_dict.get('location')
    link = event_dict.get('link')
    participants = event_dict.get('participants')
    flexibility = event_dict.get('flexibility')
    blocking = event_dict.get('blocking', True)

    if service is None:
        logger.error("Google Calendar service is not initialized (auth failed).")
        return None
    if not summary or not start or not end:
        logger.error("Event dictionary missing required fields.")
        return None

    # Generate a deterministic dedupe key for idempotency
    # Format: calendar|summary|start_date|recurrence_pattern
    import hashlib
    from datetime import datetime
    
    start_date = start.split('T')[0] if 'T' in start else start
    # Extract key parts of recurrence rule for deduplication
    recurrence_parts = recurrence_rule.replace('RRULE:', '').split(';')
    recurrence_key = '|'.join(sorted([p for p in recurrence_parts if any(x in p for x in ['FREQ', 'BYMONTH', 'BYMONTHDAY'])]))
    
    dedupe_string = f"{calendarId}|{summary.lower().strip()}|{start_date}|{recurrence_key}"
    dedupe_key = hashlib.md5(dedupe_string.encode()).hexdigest()
    
    logger.info(f"Idempotency check - dedupe_key: {dedupe_key} (from: {dedupe_string})")
    
    # Check for existing event with same dedupe_key
    try:
        # Search for events with this dedupe_key in the past 30 days and next 365 days
        now = utc_now()
        time_min = to_rfc3339_z(now - timedelta(days=30))
        time_max = to_rfc3339_z(now + timedelta(days=365))
        
        existing_events = service.events().list(
            calendarId=calendarId,
            timeMin=time_min,
            timeMax=time_max,
            q=summary,  # Pre-filter by summary for efficiency
            maxResults=100,
            singleEvents=False  # Get parent recurring events
        ).execute()
        
        for event in existing_events.get('items', []):
            # Check if this event has our dedupe_key
            extended_props = event.get('extendedProperties', {})
            private_props = extended_props.get('private', {})
            event_dedupe_key = private_props.get('dedupe_key')
            
            if event_dedupe_key == dedupe_key:
                logger.info(f"Found existing event with same dedupe_key: {event.get('id')} - {event.get('summary')}")
                logger.info(f"Returning existing event instead of creating duplicate")
                return event
        
        logger.info("No existing event found with this dedupe_key, proceeding with creation")
    except Exception as e:
        logger.warning(f"Error during idempotency check: {e}. Proceeding with creation.")

    # Convert UTC→local for Google payload (handles all_day flag)
    times = convert_utc_to_local_event_times({
        "event_name": summary,
        "start": start,
        "end": end
    }, all_day=all_day)

    event = {
        'summary': summary,
        'start': times["start"],
        'end': times["end"],
        'recurrence': [recurrence_rule],
    }

    # Optional fields
    if description:
        event['description'] = description
    if location:
        event['location'] = location
    if link:
        desc = event.get('description', "")
        event['description'] = (desc + "\n" if desc else "") + f"Link: {link}"

    # Build attendees list from a list of dicts
    if isinstance(participants, list):
        attendees = []
        for p in participants:
            email = p.get('email')
            name = p.get('displayName') or p.get('name') or ""
            if email:
                attendee = {'email': email}
                if name:
                    attendee['displayName'] = name
                attendees.append(attendee)
        event['attendees'] = attendees
    
    # Google's transparency field: opaque = blocking, transparent = non-blocking
    event['transparency'] = 'opaque' if blocking else 'transparent'
    
    # Add extended properties for custom fields (persists in Google Calendar)
    extended_props = {}
    if flexibility:
        extended_props['flexibility'] = flexibility
    # Add dedupe_key for idempotency
    extended_props['dedupe_key'] = dedupe_key
    if extended_props:
        event['extendedProperties'] = {'private': extended_props}

    logger.info(f"Creating repeating event with payload: {event}")

    try:
        created_event = service.events().insert(
            calendarId=calendarId,
            body=event
        ).execute()
        logger.info(f"Repeating event created successfully: {created_event}")
        return created_event

    except Exception as e:
        logger.error(f"An error occurred while creating the repeating event: {e}")
        return None



def get_events(
        service,
        calendar_names: Optional[List[str]] = None,
        calendar_ids: Optional[List[str]] = None,
        max_results: int = 10,
        start_str: Optional[str] = None,
        end_str: Optional[str] = None,
        single_events=False
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch events from specified Google Calendars. Supports both a time range and upcoming events.

    Args:
        service: Authenticated Google Calendar service instance.
        calendar_names (Optional[List[str]]): List of calendar names to fetch events from.
        calendar_ids (Optional[List[str]]): List of calendar IDs to fetch events from.
        max_results (int): Maximum number of events per calendar.
        start_str (Optional[str]): Start datetime in RFC3339 format with timezone.
                                   Defaults to current time if not provided.
        end_str (Optional[str]): End datetime in RFC3339 format with timezone.

    Returns:
        List of event dictionaries or None if an error occurs.
    """
    try:
        if service is None:
            logger.error("Google Calendar service is not initialized (auth failed).")
            return None
        # Resolve calendar IDs for the provided names
        resolved_calendar_ids = []
        if calendar_names:
            for name in calendar_names:
                print(name)
                calendar_id = get_calendar_by_name(service, name)
                print(calendar_id)
                if calendar_id:
                    resolved_calendar_ids.append(calendar_id)
                else:
                    logger.warning(f"Skipping unresolved calendar name: {name}")

        # Include any direct calendar IDs provided (e.g., 'Birthday')
        if calendar_ids:
            print("Calendard ID DEBUG")
            print(calendar_ids)
            resolved_calendar_ids.extend(calendar_ids)
            print(resolved_calendar_ids)

        if not resolved_calendar_ids:
            logger.warning("No valid calendar IDs resolved. Defaulting to 'primary'.")
            resolved_calendar_ids = ['primary']

        # Use current time as default start_str if not provided
        now = utc_now().isoformat().replace("+00:00", "Z")
        start_str = start_str or now

        all_events = []
        for calendar_id in resolved_calendar_ids:
            try:
                logger.info(f"Fetching events from calendar ID: {calendar_id}")
                events_result = service.events().list(
                    calendarId=calendar_id,
                    timeMin=start_str,
                    timeMax=end_str,
                    maxResults=max_results,
                    singleEvents=single_events,
                    orderBy='startTime' if single_events else None,
                    timeZone='UTC'
                ).execute()

                events = events_result.get('items', [])

                logger.info(f"Fetched {len(events)} events from calendar '{calendar_id}'.")
                all_events.extend(events)

            except Exception as e:
                logger.error(f"Error fetching events for calendar ID '{calendar_id}': {e}")

        return all_events if all_events else None

    except Exception as e:
        logger.error(f"Error in get_events: {e}")
        return None


def list_calendars(service):
    """
    Lists all available calendars with their summaries and IDs.

    Args:
        service: Authenticated Google Calendar service instance.
    """
    try:
        if service is None:
            logger.error("Google Calendar service is not initialized (auth failed).")
            return
        calendar_list = []
        page_token = None
        while True:
            response = service.calendarList().list(pageToken=page_token).execute()
            calendar_list.extend(response.get('items', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break

        logger.info("Available Calendars:")
        for calendar in calendar_list:
            logger.info(f"Name: {calendar.get('summary')}, ID: {calendar.get('id')}")
    except Exception as e:
        logger.error(f"Failed to list calendars: {e}")


def search_event_by_name(service, query: str) -> Optional[List[Dict[str, Any]]]:
    """
    Search events by name or keyword in the summary.
    """
    try:
        if service is None:
            logger.error("Google Calendar service is not initialized (auth failed).")
            return None
        events_result = service.events().list(
            calendarId='primary',
            q=query,
            singleEvents=True,
            orderBy='startTime',
            timeZone='UTC'
        ).execute()
        events = events_result.get('items', [])
        if not events:
            logger.info(f'No events found for query: {query}')
            return []

        event_details = []
        for event in events:
            # Extract flexibility from extendedProperties
            extended_props = event.get('extendedProperties', {})
            private_props = extended_props.get('private', {})
            flexibility = private_props.get('flexibility', 'fixed')
            
            # Extract blocking from transparency (opaque=blocking, transparent=non-blocking)
            transparency = event.get('transparency', 'opaque')
            blocking = transparency != 'transparent'
            
            event_info = {
                "id": event.get('id'),
                "summary": event.get('summary', 'No Title'),
                "start": event['start'].get('dateTime', event['start'].get('date')),
                "end": event['end'].get('dateTime', event['end'].get('date')),
                "link": event.get('htmlLink', 'No link available'),
                "location": event.get('location', ''),
                "flexibility": flexibility,
                "blocking": blocking,
            }
            event_details.append(event_info)

        logger.info(f"Found {len(event_details)} events matching '{query}'.")
        return event_details
    except Exception as e:
        logger.error(f"An error occurred while searching for events: {e}")
        return None


def edit_event(service, event_id: str, updates: Dict[str, Any], scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Edit an existing event by its ID.
    
    Args:
        service: Authenticated Google Calendar service instance.
        event_id: The ID of the event to edit.
        updates: Dictionary of fields to update.
        scope: For recurring events:
               - "single": Update only this occurrence (creates an exception)
               - "all": Update all occurrences (modifies parent event)
               - None: Defaults to "single" for instances, "all" for non-recurring
    
    Returns:
        Updated event dictionary or None if failed.
    """
    try:
        if service is None:
            logger.error("Google Calendar service is not initialized (auth failed).")
            return None
        # Fetch the existing event
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        logger.debug(f"Fetched event: {event.get('id')} - {event.get('summary')}")

        # Check if this is part of a recurring series
        recurring_event_id = event.get("recurringEventId")
        is_recurring_instance = recurring_event_id is not None
        
        # Determine which event ID to update based on scope
        if is_recurring_instance:
            if scope == "all":
                # Update the parent event (affects all occurrences)
                target_event_id = recurring_event_id
                # Need to fetch the parent event to update it
                event = service.events().get(calendarId='primary', eventId=recurring_event_id).execute()
                logger.info(f"Updating ALL occurrences of recurring event (parent: {recurring_event_id})")
            else:
                # Default to "single" - update just this instance (creates exception)
                target_event_id = event_id
                # For single instance updates, remove recurrence field if present in updates
                # (single instances cannot have recurrence rules)
                updates.pop("recurrence", None)
                logger.info(f"Updating SINGLE occurrence: {event_id} (parent: {recurring_event_id})")
        else:
            # Non-recurring event - just update it directly
            target_event_id = event_id
            logger.info(f"Updating non-recurring event: {event_id}")

        # Ensure recurrence is fully replaced, not merged (only for parent/non-recurring)
        if "recurrence" in updates and isinstance(updates["recurrence"], list):
            updates["recurrence"] = [updates["recurrence"][0]]  # Ensure it's a full override list

        # Handle datetime conversions correctly
        # The dateTime field might be a datetime object or a string
        for time_field in ["start", "end"]:
            if time_field in updates and isinstance(updates[time_field], dict):
                dt_value = updates[time_field].get("dateTime")
                if dt_value is not None:
                    # Convert datetime object to ISO string if needed
                    if hasattr(dt_value, 'isoformat'):
                        dt_str = dt_value.isoformat()
                    else:
                        dt_str = str(dt_value)
                    # Remove trailing Z if present (Google wants local time format)
                    updates[time_field]["dateTime"] = dt_str.replace("Z", "")

        # Handle flexibility via extendedProperties
        if 'flexibility' in updates:
            flexibility = updates.pop('flexibility')
            # Merge with existing extendedProperties if present
            existing_props = event.get('extendedProperties', {})
            private_props = existing_props.get('private', {})
            private_props['flexibility'] = flexibility
            updates['extendedProperties'] = {'private': private_props}

        # Handle blocking via Google's transparency field
        if 'blocking' in updates:
            blocking = updates.pop('blocking')
            updates['transparency'] = 'opaque' if blocking else 'transparent'

        # Build the patch body - only include fields we want to update
        # This is cleaner than modifying the full event object
        patch_body = {}
        allowed_fields = ['summary', 'description', 'start', 'end', 'location', 'recurrence', 'attendees', 'reminders', 'extendedProperties', 'transparency']
        for field in allowed_fields:
            if field in updates:
                patch_body[field] = updates[field]
        
        if not patch_body:
            logger.warning("No valid fields to update")
            return event  # Nothing to update, return original
        
        logger.debug(f"Patching event {target_event_id} with: {patch_body}")
        
        # Use patch instead of update - patch only sends changed fields
        # This is safer for single instance updates
        updated_event = service.events().patch(
            calendarId='primary', 
            eventId=target_event_id, 
            body=patch_body,
            sendUpdates='all'  # Notify attendees of the change
        ).execute()
        
        logger.info(f"Event updated successfully: {updated_event.get('htmlLink')}")
        return updated_event
        
    except Exception as e:
        logger.error(f"An error occurred while updating the event: {e}", exc_info=True)
        return None


def delete_event(service, event_id: str) -> bool:
    """
    Delete a single calendar event.

    If event_id refers to a single (non-recurring) event, that event is deleted.
    If event_id refers to a specific instance of a recurring series (e.g.
    "<base>_<UTC_timestamp>Z"), Google creates a cancellation exception for
    just that occurrence — the recurring series itself is untouched.

    This function never escalates to the parent series. For series-wide
    deletion, callers must use delete_recurring_event.
    """
    try:
        if service is None:
            logger.error("Google Calendar service is not initialized (auth failed).")
            return False
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        logger.info(f"Event deleted successfully: {event_id}")
        return True
    except Exception as e:
        logger.error(f"An error occurred while deleting the event: {e}")
        return False


def delete_recurring_event(service, event_id: str) -> bool:
    """
    Delete an entire recurring event series (all instances).

    event_id may be either the base series id or any single instance id. If it's
    an instance, we resolve to the parent via `recurringEventId` and delete that.
    If it's already a base series id, we delete it directly. Non-recurring
    events are rejected — callers should use delete_event for those.
    """
    try:
        if service is None:
            logger.error("Google Calendar service is not initialized (auth failed).")
            return False

        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        parent_event_id = event.get("recurringEventId")
        if parent_event_id:
            target_event_id = parent_event_id
            logger.info(
                "delete_recurring_event: instance %s belongs to series %s; deleting series.",
                event_id, target_event_id,
            )
        else:
            # event_id IS the base event. It must be a recurring series for this tool.
            if not event.get("recurrence"):
                logger.error(
                    "delete_recurring_event: %s is not a recurring event; use delete_event instead.",
                    event_id,
                )
                return False
            target_event_id = event_id

        service.events().delete(calendarId='primary', eventId=target_event_id).execute()
        logger.info(f"Recurring series deleted successfully: {target_event_id}")
        return True
    except Exception as e:
        logger.error(f"An error occurred while deleting the recurring event: {e}")
        return False


if __name__ == "__main__":
    # Initialize the Google Calendar service
    service = authenticate_google_api()
    if not service:
        logger.error("Google Calendar service initialization failed.")
        print(f"\n{'=' * 80}")
        print(f"🛑 FATAL: Google Calendar service initialization failed")
        print("   Check that credentials exist at: app/assistant/lib/credentials/credentials.json")
        print("   Check that token exists at: app/assistant/lib/credentials/token.pickle")
        print(f"{'=' * 80}\n")
        exit(1)

    # List all available calendars for verification
    list_calendars(service)

    # Test: Fetch events from multiple calendars using explicit UTC strings
    try:
        # Define start and end times in explicit UTC (ISO 8601 with 'Z')
        start_time = "2025-02-21T00:00:00Z"  # Jan 1, 2025 at midnight UTC
        end_time = "2025-03-10T23:59:59Z"

        # List of calendar names to fetch events from
        calendar_names = ['primary', 'Birthday', 'Food', 'Work']

        logger.info("Fetching events from multiple calendars (fixed period)...")
        events = get_events(
            service=service,
            calendar_names=calendar_names,
            start_str=start_time,
            end_str=end_time,
            max_results=100
            # Ensure get_events() passes timeZone='UTC' in its API call.
        )

        if events:
            logger.info(f"Total events fetched: {len(events)}")
            for event in events:
                summary = event.get('summary', 'No Title')
                # Since we are now receiving events in UTC, no conversion is necessary.
                event_start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date'))
                event_end = event.get('end', {}).get('dateTime', event.get('end', {}).get('date'))
                link = event.get('htmlLink', 'No link available')
                logger.info(f"Event: {summary}")
                logger.info(f"Start: {event_start}")
                logger.info(f"End: {event_end}")
                logger.info(f"Link: {link}\n")
        else:
            logger.info("No events found for the specified calendars and time range.")
    except Exception as e:
        logger.error(f"Error fetching events: {e}")

    # # Example: Creating a single event with explicit UTC times
    # single_event = {
    #     "event_name": "Party at Los Angeles",
    #     "start": "2024-05-20T20:00:00Z",  # Explicit UTC time
    #     "end": "2024-05-20T23:59:00Z",  # Explicit UTC time
    #     "time_zone": "UTC",
    #     "description": "An exciting party with food, drinks, and music.",
    #     "location": "Downtown Los Angeles",
    #     "link": "https://zoom.us/j/123456789",
    #     "participants": {
    #         "John Doe": "john.doe@example.com",
    #         "Jane Smith": "jane.smith@example.com"
    #     }
    # }
    # result = create_event(service, single_event)
    # if result:
    #     print("Created single event:")
    #     print(result)
    #
    # # Example: Creating a repeating event with explicit UTC times
    # repeating_event = {
    #     "event_name": "Gym Session",
    #     "start": "2024-05-20T20:00:00Z",  # Explicit UTC time
    #     "end": "2024-05-20T23:59:00Z",  # Explicit UTC time
    #     "time_zone": "UTC",
    #     "recurrence_rule": "RRULE:FREQ=WEEKLY;COUNT=10",
    #     "description": "Weekly gym session to stay fit and healthy.",
    #     "location": "Gold's Gym, Los Angeles",
    #     "link": "https://zoom.us/j/987654321",
    #     "participants": {
    #         "Trainer Mike": "mike.trainer@example.com",
    #         "Alice Brown": "alice.brown@example.com"
    #     }
    # }
    # result = create_repeating_event(service, repeating_event)
    # if result:
    #     print("Created repeating event:")
    #     print(result)
    #
    # # Search for an event by name
    # events = search_event_by_name(service, "Party")
    # if events:
    #     print("Search results for 'Party':")
    #     print(events)
    #
    # # Edit an event by ID (using explicit UTC times)
    # if events:
    #     event_id = events[0]['id']  # Use the ID of the first matching event
    #     updates = {
    #         "summary": "Updated Gym Session",
    #         "start": {"dateTime": "2024-07-02T08:00:00Z", "timeZone": "UTC"},
    #         "end": {"dateTime": "2024-07-02T09:00:00Z", "timeZone": "UTC"},
    #         "description": "Updated description for the weekly gym session.",
    #         "location": "Updated Gold's Gym, Los Angeles",
    #     }
    #     updated_event = edit_event(service, event_id, updates)
    #     print("Updated event:")
    #     print(updated_event)

    # Delete an event by ID
    if events:
        event_id = events[0]['id']
        delete_event(service, event_id)
        logger.info(f"Deleted event with ID: {event_id}")

    # Additionally: Fetch all events for the current week
    try:
        # Calculate the start (Monday) and end (Sunday) of the current week in UTC.
        now = utc_now()
        start_of_week = now - timedelta(days=now.weekday())
        # End of week: Sunday 23:59:59
        end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

        start_week_str = to_rfc3339_z(start_of_week)
        end_week_str = to_rfc3339_z(end_of_week)

        logger.info("Fetching events for the current week (UTC)...")
        week_events = get_events(
            service=service,
            calendar_names=calendar_names,
            start_str=start_week_str,
            end_str=end_week_str,
            max_results=50
        )

        if week_events:
            logger.info(f"Total events for this week: {len(week_events)}")
            for event in week_events:
                logger.info(json.dumps(event, indent=4))  # Print full event data
            logger.info("No events found for the current week.")
            for event in week_events:
                summary = event.get('summary', 'No Title')
                event_start = event.get('start', {}).get('dateTime', event.get('start', {}).get('date'))
                event_end = event.get('end', {}).get('dateTime', event.get('end', {}).get('date'))
                link = event.get('htmlLink', 'No link available')
                logger.info(f"Weekly Event: {summary}")
                logger.info(f"Start: {event_start}")
                logger.info(f"End: {event_end}")
                logger.info(f"Link: {link}\n")
        else:
            logger.info("No events found for the current week.")
    except Exception as e:
        logger.error(f"Error fetching current week events: {e}")
