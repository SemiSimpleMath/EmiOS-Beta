from collections import defaultdict
from datetime import timedelta
from email.utils import parsedate_to_datetime

from datetime import timezone

from datetime import datetime
from app.assistant.utils.time_utils import utc_to_local

def format_time_short(dt):
    return dt.strftime("%b %d %Y, %I:%M%p").lower()



def prune_events_by_type(category: str, events: list) -> list:
    if category == "calendar":
        return prune_calendar_events(events)
    elif category == "email":
        return prune_email_events(events)
    elif category == "todo_task":
        return prune_todo_events(events)
    else:
        return events  # fallback: return unmodified


def prune_calendar_events(events: list) -> list:
    """
    Deduplicate and prune calendar events:
    - Only events from today (midnight) through the same day next week (midnight)
    - Range is anchored to midnight and doesn't change throughout the day
    - Example: Tuesday 00:00 through next Tuesday 23:59 (7 full days)
    - One entry per recurring series (by recurring_event_id)
    - All single events retained
    - Times converted to local and formatted via utc_to_local() & format_time_short()
    """
    pruned = []
    seen_recurring = set()
    
    # Anchor to today's midnight (doesn't change throughout the day)
    now_local = utc_to_local(datetime.now(timezone.utc))
    today_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Show 7 full days: today 00:00:00 through (today + 7 days - 1 second) = 23:59:59 of day 7
    # Example: Tuesday 00:00:00 to Monday 23:59:59 (next week)
    cutoff_end = today_midnight + timedelta(days=7)

    for ev in events:
        d = ev.get("data", {})
        summary = d.get("summary", "").strip()
        start_iso = d.get("start", "")
        end_iso = d.get("end", "")
        # convert & format times
        try:
            start_dt = utc_to_local(datetime.fromisoformat(start_iso.replace("Z", "+00:00")))
            end_dt   = utc_to_local(datetime.fromisoformat(end_iso.replace("Z", "+00:00")))
            
            # Filter: only include events from today midnight through next week same day
            # Example: Tuesday 00:00 to next Tuesday 00:00 (includes all of today through end of next Monday)
            if start_dt < today_midnight or start_dt >= cutoff_end:
                continue
            
            start = format_time_short(start_dt)
            end   = format_time_short(end_dt)
        except Exception:
            # If we can't parse the date, skip this event
            continue

        recurring_id = d.get("recurring_event_id")
        recurrence_rule = d.get("recurrence_rule")

        if recurring_id:
            # skip if we've already added this series
            if recurring_id in seen_recurring:
                continue
            seen_recurring.add(recurring_id)
            pruned.append({
                "summary": summary,
                "start": start,
                "end": end,
                "recurrence_rule": recurrence_rule
            })
        else:
            # one-off event
            pruned.append({
                "summary": summary,
                "start": start,
                "end": end,
            })

    return pruned



def prune_email_events(events: list) -> list:
    """
    Keep only recent emails (last 10 hours). Strip promotional subjects.
    Keep: sender, subject, summary, date, action_items, id
    """
    from datetime import timedelta
    now_local = utc_to_local(datetime.now(timezone.utc))
    cutoff = now_local - timedelta(hours=10)

    pruned = []
    for e in events:
        d = e.get("data", {})
        subject = d.get("subject", "").strip()
        sender = d.get("sender", "").strip()
        summary = d.get("summary", "").strip()
        date_str = d.get("date_received", "").strip()
        action_items = d.get("action_items", [])
        event_id = d.get("id")
        importance = d.get("importance")

        if importance and importance < 4:
            continue
        try:
            email_datetime = utc_to_local(parsedate_to_datetime(date_str))
        except Exception:
            continue

        # Only include emails from the last 60 minutes
        if email_datetime < cutoff:
            continue

        keep = {
            "sender": sender,
            "subject": subject,
            "summary": summary,
            "date": date_str,
        }
        if action_items:
            keep["action_items"] = action_items
        if event_id:
            keep["id"] = event_id

        pruned.append(keep)
    return pruned


def prune_todo_events(events: list) -> list:
    """
    Return all non-completed todos.
    Keep: title, due, status
    """
    pruned = []
    seen = set()

    for e in events:
        d = e.get("data", {})
        title = d.get("title", "").strip()
        due = d.get("due", "").strip()
        status = d.get("status", "needsAction").lower()

        if status == "completed" or not title:
            continue

        key = (title, due)
        if key in seen:
            continue
        seen.add(key)

        keep = {
            "title": title,
            "status": status,
        }
        if due:
            try:
                dt = utc_to_local(datetime.fromisoformat(due.replace("Z", "+00:00")))
                keep["due"] = format_time_short(dt)
            except Exception:
                keep["due"] = due


        pruned.append(keep)

    return pruned


def filter_scheduler_events(cutoff_date=None) -> list:
    """
    Fetch scheduler events and return only those whose local occurrence date == cutoff_date.
    cutoff_date: local date to match (defaults to today in local time)
    """
    from app.assistant.event_repository.event_repository import EventRepositoryManager
    import json

    if cutoff_date is None:
        cutoff_date = utc_to_local(datetime.now(timezone.utc)).date()

    repo = EventRepositoryManager()
    raw = repo.search_events(data_type="scheduler")
    events = json.loads(raw)

    filtered = []
    for event in events:
        data = event.get("data", {})
        occurrence_str = data.get("occurrence") or data.get("start_date")
        if not occurrence_str:
            continue
        try:
            occurrence_utc = datetime.fromisoformat(occurrence_str)
            occurrence_local = utc_to_local(occurrence_utc)
        except Exception:
            continue

        if occurrence_local.date() == cutoff_date:
            data["occurrence"] = occurrence_str  # ensure it's set for summarizer
            filtered.append(event)

    return filtered


def summarize_repeating_events(events: list) -> list:
    daily_events = defaultdict(list)
    skipped_events = []
    # Convert current time to local for proper comparisons
    current_datetime_local = utc_to_local(datetime.now(timezone.utc))

    # Bucket events by local day (and event_id for interval events)
    # print(events)
    for event in events:
        event_data = event.get("data", {})
        occurrence_str = event_data.get("occurrence")
        if not occurrence_str:
            print(f"Skipping event {event_data.get('event_id', 'unknown')} - No 'occurrence' found.")
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
                group_key = (occurrence_dt_local.date(), None)
            daily_events[group_key].append((occurrence_dt_local, event))
        except ValueError as e:
            print(f"Skipping event {event_data.get('event_id', 'unknown')} due to date parsing error: {e}")
            skipped_events.append(event)

    summarized = []

    # Process each group (either a day for one-time events or (day, event_id) for repeating events)
    for group, events_in_group in sorted(daily_events.items(), key=lambda item: (item[0][0], item[0][1] or "")):
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

    print(f"Summarized {len(summarized)} events. Skipped {len(skipped_events)} events.")
    return summarized


def prune_scheduler_events(events):
    pruned_events = []
    for event in events:
        data = event['data']
        event_payload = data['event_payload']
        title = event_payload['title']
        importance = event_payload['importance']
        interval = event_payload.get('interval', None)

        try:
            dt = utc_to_local(datetime.fromisoformat(data['occurrence'].replace("Z", "+00:00")))
            occurrence = format_time_short(dt)
        except Exception:
            occurrence = data['occurrence']


        pruned_event = {"title": title, "importance": importance, "interval": interval, "occurrence": occurrence}

        pruned_events.append(pruned_event)

    return pruned_events

if __name__ == "__main__":
    import json
    from pprint import pprint
    from app.assistant.event_repository.event_repository import EventRepositoryManager

    print("🔍 Running pruning test from repo...\n")
    repo = EventRepositoryManager()
    categories = ["calendar", "email", "todo_task"]

    for cat in categories:
        print(f"\n=== {cat.upper()} ===")
        raw = repo.search_events(data_type=cat)
        try:
            events = json.loads(raw)
            print(events)
        except Exception as e:
            print(f"❌ Failed to parse events for {cat}: {e}")
            continue

        pruned = prune_events_by_type(cat, events)
        pprint(pruned)

    # Optional: run scheduler test separately
    future_scheduler_events = filter_scheduler_events()
    summarized = summarize_repeating_events(future_scheduler_events)
    pruned_scheduler_events = prune_scheduler_events(summarized)
    print("\n=== SCHEDULER (SUMMARIZED) ===")
    pprint(summarized)
    print(pruned_scheduler_events)
