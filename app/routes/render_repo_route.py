# render_repo_route.py
import json
import uuid
from flask import Blueprint, jsonify, current_app

from app.assistant.utils.time_utils import utc_to_local, get_local_timezone
from app.assistant.event_repository.event_repository import EventRepositoryManager

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from app.assistant.utils.logging_config import get_logger

render_repo_route_bp = Blueprint('render_repo_route', __name__)
logger = get_logger(__name__)


def _load_expected_calendar_for_today():
    """Load today's expected calendar from the dayflow pipeline output.

    Returns a list of widget-format dicts if the file exists and is for today,
    otherwise None (caller should fall back to Google Calendar events).
    """
    try:
        from app.assistant.utils.path_utils import get_resources_dir
        path = get_resources_dir() / "dayflow_pipeline_outputs" / "resource_expected_calendar.json"
        if not path.exists():
            return None

        data = json.loads(path.read_text(encoding="utf-8"))
        cal_date = data.get("date", "")

        local_tz = get_local_timezone()
        today_str = datetime.now(local_tz).strftime("%Y-%m-%d")
        if cal_date != today_str:
            return None

        schedule = data.get("expected_schedule")
        if not isinstance(schedule, list) or not schedule:
            return None

        widget_items = []
        for item in schedule:
            start_utc = item.get("start_utc") or ""
            end_utc = item.get("end_utc") or ""
            if not start_utc:
                continue

            widget_items.append({
                "data": {
                    "id": item.get("calendar_item_id") or str(uuid.uuid4()),
                    "summary": item.get("title", "Untitled"),
                    "start": start_utc,
                    "end": end_utc or start_utc,
                    "is_all_day": False,
                    "status": item.get("status", "upcoming"),
                    "source": item.get("source", ""),
                },
                "data_type": "calendar",
            })
        return widget_items if widget_items else None
    except Exception as e:
        logger.debug("Could not load expected calendar: %s", e, exc_info=True)
        return None


@render_repo_route_bp.route('/render_repo_route', methods=['POST'])
def render_repo_route():
    """
    Fetch all repo data (calendar, scheduler, email, weather, todo_task, news)
    and format it properly for UI widget rendering.

    For today's calendar, prefers the expected calendar produced by the dayflow
    pipeline (enriched with statuses and user-added events).  Falls back to raw
    Google Calendar events when no expected calendar is available.
    """
    try:
        event_repo = EventRepositoryManager()
        categories = ["calendar", "scheduler", "email", "weather", "todo_task", "news"]
        widget_data = []

        category_counts = {}
        errors = []

        expected_cal = _load_expected_calendar_for_today()

        for category in categories:
            try:
                events = event_repo.search_events(data_type=category)
                events = json.loads(events)

                if category == "email":
                    before = len(events)
                    events = [
                        e for e in events
                        if int((e.get("data") or {}).get("importance") or 0) > 4
                    ]
                    logger.debug(f"Email: Filtered {before} -> {len(events)} (importance > 4)")

                if category == "calendar":
                    if expected_cal is not None:
                        local_tz = get_local_timezone()
                        today_str = datetime.now(local_tz).strftime("%Y-%m-%d")

                        future_events = []
                        for event in events:
                            event_data = event.get("data", {})
                            start_val = event_data.get("start") or ""
                            if isinstance(start_val, dict):
                                start_str = start_val.get("dateTime") or start_val.get("date") or ""
                            else:
                                start_str = start_val
                            if not start_str:
                                continue
                            try:
                                start_dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00"))
                                event_date_str = start_dt.astimezone(local_tz).strftime("%Y-%m-%d")
                                if event_date_str > today_str:
                                    future_events.append(event)
                            except (ValueError, TypeError):
                                future_events.append(event)

                        events = []
                        widget_data.extend(expected_cal)
                        category_counts["calendar_expected"] = len(expected_cal)

                        for event in future_events:
                            data = event["data"]
                            payload = data.get("event_payload", {})
                            if "title" not in data and "title" in payload:
                                data["title"] = payload["title"]
                            widget_data.append({
                                "data": data,
                                "data_type": event["data_type"],
                            })
                        category_counts[category] = len(future_events)
                        continue

                    current_datetime_utc = datetime.now(timezone.utc)
                    filtered_events = []
                    for event in events:
                        event_data = event.get("data", {})
                        end_val = event_data.get("end") or event_data.get("start")

                        if isinstance(end_val, dict):
                            end_str = end_val.get("dateTime") or end_val.get("date")
                        else:
                            end_str = end_val

                        if end_str:
                            try:
                                if isinstance(end_str, str):
                                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                                    if end_dt >= current_datetime_utc:
                                        filtered_events.append(event)
                                else:
                                    filtered_events.append(event)
                            except ValueError:
                                filtered_events.append(event)
                        else:
                            filtered_events.append(event)
                    logger.debug(f"Calendar: Filtered {len(events)} -> {len(filtered_events)} events (removed ended)")
                    events = filtered_events
                
                if category == "scheduler":
                    logger.debug(f"Scheduler: Loaded {len(events)} events before filtering")
                    current_datetime_utc = datetime.now(timezone.utc)

                    filtered_events = []
                    for event in events:
                        event_data = event.get("data", {})
                        occurrence_str = event_data.get("occurrence") or event_data.get("start_date")

                        if occurrence_str:
                            try:
                                occurrence_dt_utc = datetime.fromisoformat(occurrence_str).astimezone(timezone.utc)
                                if occurrence_dt_utc >= current_datetime_utc:
                                    event['data']['occurrence'] = occurrence_str
                                    filtered_events.append(event)
                            except ValueError as e:
                                logger.warning(f"Scheduler: Could not parse occurrence '{occurrence_str}': {e}")
                                continue

                    events = filtered_events
                    events = summarize_repeating_events(events)

                for event in events:
                    data = event["data"]
                    if category in ["calendar", "scheduler", "email", "weather", "news"]:
                        payload = data.get("event_payload", {})
                        if "title" not in data and "title" in payload:
                            data["title"] = payload["title"]

                    widget_data.append({
                        "data": data,
                        "data_type": event["data_type"],
                    })
                
                category_counts[category] = len(events)
                
            except Exception as e:
                logger.error(f"Error loading {category}: {e}")
                errors.append(f"{category}: {str(e)}")
                category_counts[category] = 0

        if errors:
            logger.warning(f"render_repo_route had partial errors: {errors}")

        repo_message = {"widget_data": widget_data}
        
        response = {'success': True, 'repo_data': repo_message}
        if errors:
            response['warnings'] = errors
        
        return jsonify(response), 200

    except Exception as e:
        logger.error(f"render_repo_route ERROR: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Error retrieving repo data: {str(e)}'}), 500

def summarize_repeating_events(events: list) -> list:
    """
    Converts all event times to local time using utc_to_local, then:
      - For one-time events, includes them as is.
      - For repeating (interval) events, groups by the local day and event_id.
        For each group:
          - If the local date is today, only considers events after the current local time.
          - Otherwise, picks the event with the earliest occurrence.
    Finally, the returned events still have their UTC occurrence strings for the UI.
    """
    daily_events = defaultdict(list)
    skipped_events = []
    current_datetime_local = utc_to_local(datetime.now(timezone.utc))

    for event in events:
        event_data = event.get("data", {})
        occurrence_str = event_data.get("occurrence")
        if not occurrence_str:
            logger.debug(f"Skipping event {event_data.get('event_id', 'unknown')} - No 'occurrence' found.")
            skipped_events.append(event)
            continue

        try:
            occurrence_dt_utc = datetime.fromisoformat(occurrence_str).astimezone(timezone.utc)
            occurrence_dt_local = utc_to_local(occurrence_dt_utc)
            event_type = event_data.get("event_type")
            if event_type == "interval":
                group_key = (occurrence_dt_local.date(), event_data.get("event_id"))
            else:
                group_key = (occurrence_dt_local.date(), None)
            daily_events[group_key].append((occurrence_dt_local, event))
        except ValueError as e:
            logger.debug(f"Skipping event {event_data.get('event_id', 'unknown')} due to date parsing error: {e}")
            skipped_events.append(event)

    summarized = []

    for group, events_in_group in sorted(daily_events.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        if isinstance(group, tuple):
            group_date = group[0]
        else:
            group_date = group

        repeating = []
        one_time = []
        for occurrence_dt_local, event in events_in_group:
            if event.get("data", {}).get("event_type") == "interval":
                repeating.append((occurrence_dt_local, event))
            else:
                one_time.append(event)

        summarized.extend(one_time)

        if repeating:
            if group_date == current_datetime_local.date():
                valid = [(dt, ev) for dt, ev in repeating if dt > current_datetime_local]
            else:
                valid = repeating
            if valid:
                next_event = min(valid, key=lambda x: x[0])[1]
                summarized.append(next_event)

    logger.debug(f"Summarized {len(summarized)} events. Skipped {len(skipped_events)} events.")
    return summarized

events_debug = []

def test_summarize_repeating_events(events: list) -> list:
    """
    Converts all event times to local time using utc_to_local, then:
      - For one-time events, includes them as is.
      - For repeating (interval) events, groups by the local day and event_id.
        For each group:
          - If the local date is today, only considers events after the current local time.
          - Otherwise, picks the event with the earliest occurrence.
    Finally, the returned events still have their UTC occurrence strings for the UI.
    """
    daily_events = defaultdict(list)
    skipped_events = []
    # Convert current time to local for proper comparisons
    current_datetime_local = utc_to_local(datetime.now(timezone.utc))

    # Bucket events by local day (and event_id for interval events)
    for event in events:
        event_data = event.get("data", {})
        occurrence_str = event_data.get("occurrence")
        if not occurrence_str:
            skipped_events.append(event)
            continue

        try:
            # Parse the original UTC occurrence
            occurrence_dt_utc = datetime.fromisoformat(occurrence_str).astimezone(timezone.utc)
            # Convert to local time using your utility function
            occurrence_dt_local = utc_to_local(occurrence_dt_utc)
            # Group key: for repeating events, group by (local date, event_id), else just local date
            event_type = event_data.get("event_type")
            if event_type == "interval":
                group_key = (occurrence_dt_local.date(), event_data.get("event_id"))
            else:
                group_key = occurrence_dt_local.date()
            daily_events[group_key].append((occurrence_dt_local, event))
        except ValueError as e:
            logger.debug("Could not parse occurrence for event %s: %s",
                         event_data.get("event_id", "unknown"), e)
            skipped_events.append(event)

    summarized = []

    # Process each group (either a day for one-time events or (day, event_id) for repeating events)
    for group, events_in_group in sorted(daily_events.items()):
        # Determine the local date for filtering
        if isinstance(group, tuple):
            group_date = group[0]
        else:
            group_date = group

        repeating = []
        one_time = []
        for occurrence_dt_local, event in events_in_group:
            if event.get("data", {}).get("event_type") == "interval":
                repeating.append((occurrence_dt_local, event))
            else:
                one_time.append(event)

        # Always include one-time events
        summarized.extend(one_time)

        # For repeating events in this group, select the next occurrence
        if repeating:
            if group_date == current_datetime_local.date():
                # For today's events, only consider those after current local time
                valid = [(dt, ev) for dt, ev in repeating if dt > current_datetime_local]
            else:
                valid = repeating
            if valid:
                next_event = min(valid, key=lambda x: x[0])[1]
                summarized.append(next_event)

    return summarized

if __name__ == "__main__":


    # Call the summarizer and pretty-print the result
    summarized_events = summarize_repeating_events(events_debug)
    print("\nSummarized Events:")
    print(json.dumps(summarized_events, indent=2))
