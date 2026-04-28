import json
import json

def prune_calendar_data(raw_data):
    """
    Prunes calendar event data to remove unnecessary fields while keeping full timestamps.
    """
    pruned_events = []
    seen_events = set()  # Track duplicates

    for event in raw_data:
        # Extract actual event data safely
        event_data = event.get("data")
        if not isinstance(event_data, dict):
            print(f"Skipping event with missing or invalid 'data' field: {event}")
            continue  # Skip if there's no valid event data

        # Extract important fields
        summary = event_data.get("summary", "").strip()
        start = event_data.get("start",{}).get("dateTime", "").strip()  # Keep full timestamp
        end = event_data.get("end",{}).get("dateTime", "").strip()  # Keep full timestamp
        is_recurring = event_data.get("is_recurring", False)

        # Skip routine work hours to reduce clutter
        if summary.lower() in ["work hours"]:
            continue

        # Skip duplicates (same title on the same start time)
        event_key = (summary, start)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)

        # Build pruned event
        pruned_event = {
            "title": summary,
            "start": start,  # Full timestamp preserved
            "end": end       # Full timestamp preserved
        }
        if is_recurring:
            pruned_event["recurring"] = True  # Only include if true

        pruned_events.append(pruned_event)

    return pruned_events




def prune_scheduler_data(raw_data):
    """
    Prunes scheduler event data while keeping full timestamps.
    """
    pruned_events = []
    seen_titles = set()  # Track duplicate event titles

    for event in raw_data:
        # Extract actual event data
        event_data = event.get("data", {})  # Fix: Extract nested "data" field
        title = event_data.get("title", "").strip()
        message = event_data.get("payload_message", "").strip()
        start = event_data.get("start_date", "").strip()  # Preserve full timestamp
        interval = event_data.get("interval")

        # Skip duplicates based on title
        if title in seen_titles:
            continue
        seen_titles.add(title)

        # Could filter by importance

        # Format interval nicely
        repeat = None
        if interval:
            if interval >= 86400:
                repeat = "Daily"
            elif interval >= 3600:
                repeat = f"{interval // 3600}h"
            elif interval >= 60:
                repeat = f"{interval // 60}m"

        # Build pruned event
        pruned_event = {
            "title": title,
            "message": message,
            "start": start,  # Full timestamp preserved
        }
        if repeat:
            pruned_event["repeat"] = repeat  # Only include repeat if applicable

        pruned_events.append(pruned_event)

    return pruned_events



def prune_email_data(raw_data):
    """
    Prunes email data to remove unnecessary fields and optimize for summarization.
    """
    pruned_emails = []

    for event in raw_data:
        # Extract actual event data
        email = event.get("data", {})  # Fix: Extract nested "data" field
        sender = email.get("sender", "").strip()
        subject = email.get("subject", "").strip()
        date = email.get("date", "").split(",")[-1].strip().split()[0]  # Extract YYYY-MM-DD
        summary = email.get("summary", "").strip()
        action_items = email.get("action_items", [])

        # Skip purely promotional emails (based on keywords in subject)
        promo_keywords = ["sale", "discount", "limited time", "offer", "last chance", "valentine"]
        if any(word in subject.lower() for word in promo_keywords):
            continue

        pruned_email = {
            "sender": sender,
            "subject": subject,
            "date": date,
            "summary": summary,
        }

        # Include action items if available
        if action_items:
            pruned_email["action_items"] = action_items

        pruned_emails.append(pruned_email)

    return pruned_emails

def prune_todo_data(raw_data):
    """
    Prunes todo task data to remove unnecessary fields while keeping full timestamps.
    """
    pruned_tasks = []
    seen_tasks = set()  # To track duplicates

    for task in raw_data:
        # Extract actual task data safely
        task_data = task.get("data")
        if not isinstance(task_data, dict):
            print(f"Skipping task with missing or invalid 'data' field: {task}")
            continue  # Skip if there's no valid task data

        # Extract important fields
        title = task_data.get("title", "").strip()
        due_date = task_data.get("due", "").strip() if task_data.get("due") else None  # Preserve full timestamp
        status = task_data.get("status", "needsAction")

        # Skip completed tasks (if we only care about pending ones)
        if status.lower() == "completed":
            continue

        # Skip duplicates (same title on the same exact due date-time)
        task_key = (title, due_date)
        if task_key in seen_tasks:
            continue
        seen_tasks.add(task_key)

        # Build pruned task
        pruned_task = {
            "title": title,
            "due": due_date,  # Full timestamp preserved
            "status": status
        }

        pruned_tasks.append(pruned_task)

    return pruned_tasks
