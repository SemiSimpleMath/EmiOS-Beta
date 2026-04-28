from app.assistant.lib.core_tools.calendar_tool.calendar_tool import CalendarTool
from app.assistant.utils.pydantic_classes import ToolResult, ToolMessage
from datetime import datetime, timedelta, timezone
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import local_to_utc
from app.assistant.embeddings.embedder import embed_text
import numpy as np

logger = get_logger(__name__)


def get_embedding(text: str):
    """Create embedding for text."""
    return embed_text(text)

def cosine_similarity(vec1, vec2):
    """Calculate cosine similarity between two vectors"""
    vec1 = np.array(vec1)
    vec2 = np.array(vec2)
    norm_product = np.linalg.norm(vec1) * np.linalg.norm(vec2)
    if norm_product == 0:
        return 0.0
    return np.dot(vec1, vec2) / norm_product


class FindCalendarEventByName(CalendarTool):
    """
    Tool to find calendar events by name using semantic search.
    Returns the top 5 best matching events based on similarity scores.
    Inherits from CalendarTool to reuse calendar access functionality.
    """

    def __init__(self):
        super().__init__()
        self.tool_name = 'find_calendar_event_by_name'

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """
        Finds calendar events by name using semantic similarity.
        
        Parameters:
        - tool_message (ToolMessage): Contains event_name and optional start_date/end_date
        
        Returns:
        - ToolResult: Contains the top 5 best matching events sorted by similarity score
        """
        arguments = tool_message.tool_data.get('arguments', {})
        event_name = arguments.get('event_name')
        start_date = arguments.get('start_date')
        end_date = arguments.get('end_date')

        if not event_name:
            logger.error("Error: 'event_name' argument is required.")
            return ToolResult(
                result_type="error",
                content="Error: 'event_name' argument is required."
            )

        # Set default date range if not provided (search 1 year back and 1 year forward)
        if not start_date:
            start_date = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
            logger.info(f"No start_date provided, defaulting to 1 year ago: {start_date}")
        
        if not end_date:
            end_date = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
            logger.info(f"No end_date provided, defaulting to 1 year from now: {end_date}")

        # Convert local time to UTC with proper timezone formatting
        # (Agents provide naive local timestamps, we need to convert to UTC for Google Calendar API)
        try:
            utc_start = local_to_utc(start_date)
            start_date_utc = utc_start.isoformat().replace('+00:00', 'Z')
            logger.debug(f"Converted start_date: {start_date} -> {start_date_utc}")
        except Exception as e:
            logger.error(f"Error converting start_date '{start_date}': {e}")
            return ToolResult(result_type="error", content=f"Invalid start_date format: {start_date}")
        
        try:
            utc_end = local_to_utc(end_date)
            end_date_utc = utc_end.isoformat().replace('+00:00', 'Z')
            logger.debug(f"Converted end_date: {end_date} -> {end_date_utc}")
        except Exception as e:
            logger.error(f"Error converting end_date '{end_date}': {e}")
            return ToolResult(result_type="error", content=f"Invalid end_date format: {end_date}")

        try:
            # Initialize calendar credentials
            self.init_credentials()
            
            # Fetch all calendar events in the date range (read-only, no sync to repo)
            logger.info(f"Fetching calendar events from {start_date_utc} to {end_date_utc} (search mode - no repo sync)")
            fetch_result = self.handle_get_calendar_events({
                'start_date': start_date_utc,
                'end_date': end_date_utc,
                'calendar_names': None,  # Search all calendars
                'single_events': False,  # Get master events (not expanded instances)
                'max_results': 10000,    # Large limit to get all events in range
                'sync_to_repo': False    # Read-only mode: don't store or sync to repository
            })

            if fetch_result.result_type == "error":
                return fetch_result

            events = fetch_result.data_list
            if not events:
                logger.info("No events found in the specified date range.")
                return ToolResult(
                    result_type="success",
                    content=f"No calendar events found in the date range {start_date} to {end_date}.",
                    data_list=[]
                )

            logger.info(f"Found {len(events)} events. Deduplicating by title...")

            # STEP 1: Deduplicate by title - keep only one representative event per unique title
            unique_events = {}
            for event in events:
                title = event.get('summary', '').strip()
                if not title:
                    continue
                
                # Keep the first occurrence of each unique title
                if title not in unique_events:
                    unique_events[title] = event

            logger.info(f"After deduplication: {len(unique_events)} unique event titles")

            if not unique_events:
                logger.info("No events with titles found.")
                return ToolResult(
                    result_type="success",
                    content=f"No events matching '{event_name}' were found.",
                    data_list=[]
                )

            # STEP 2: Get embedding for the search query (once)
            query_embedding = get_embedding(event_name)

            # STEP 3: Compute similarity for each unique event title
            event_scores = []

            for title, event in unique_events.items():
                # Create a searchable text from event summary and description
                event_text = f"{event.get('summary', '')} {event.get('description', '')}"

                # Get embedding and compute similarity
                event_embedding = get_embedding(event_text)
                similarity = cosine_similarity(query_embedding, event_embedding)

                logger.debug(f"Event: '{title}' - Similarity: {similarity:.4f}")
                
                event_scores.append({
                    'event': event,
                    'similarity': similarity
                })

            if not event_scores:
                logger.info("No matching events found.")
                return ToolResult(
                    result_type="success",
                    content=f"No events matching '{event_name}' were found.",
                    data_list=[]
                )

            # Sort by similarity (highest first) and take top 5
            event_scores.sort(key=lambda x: x['similarity'], reverse=True)
            top_k = event_scores[:5]

            logger.info(f"Top match: '{top_k[0]['event'].get('summary', 'No Title')}' with similarity {top_k[0]['similarity']:.4f}")
            logger.info(f"Returning top {len(top_k)} matches")

            # Extract just the events for data_list
            top_events = [item['event'] for item in top_k]
            
            # Build a readable summary
            summary_lines = [f"Found {len(top_k)} matching events:"]
            for i, item in enumerate(top_k, 1):
                summary_lines.append(f"{i}. '{item['event'].get('summary', 'No Title')}' (similarity: {item['similarity']:.4f})")
            
            return ToolResult(
                result_type="success",
                content="\n".join(summary_lines),
                data_list=top_events
            )

        except Exception as e:
            logger.error(f"Error finding calendar event: {e}", exc_info=True)
            return ToolResult(
                result_type="error",
                content=f"Error finding calendar event: {str(e)}"
            )


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return FindCalendarEventByName


# === TEST BLOCK ===
if __name__ == "__main__":
    import app.assistant.tests.test_setup  # This is just run for the import
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import ToolMessage

    # Create a mock ToolMessage for testing
    mock_message = ToolMessage(
        data_type="tool_request",
        sender="test_user",
        receiver=None,
        task="find_calendar_event",
        tool_name="find_calendar_event_by_name",
        tool_data={
            "arguments": {
                "event_name": "Riley's Art",
                # Optional: Specify date range
                # "start_date": "2024-11-01T00:00:00",
                # "end_date": "2026-11-02T23:59:59"
            }
        },
        request_id=None,
    )

    # Instantiate the tool and execute
    find_event_tool = FindCalendarEventByName()
    result = find_event_tool.execute(mock_message)

    # Print results
    print("\n" + "="*80)
    print("RESULT:")
    print("="*80)
    print(result.content)
    print(f"\nReturned {len(result.data_list)} events:")
    print("="*80)
    
    for i, event in enumerate(result.data_list, 1):
        print(f"\n[{i}] {event.get('summary', 'No Title')}")
        print(f"    ID: {event.get('id')}")
        print(f"    Description: {event.get('description', 'No description')}")
        print(f"    Start: {event.get('start_dateTime')}")
        print(f"    End: {event.get('end_dateTime')}")
        print(f"    Is Recurring: {event.get('is_recurring')}")
        print(f"    Recurring Event ID: {event.get('recurring_event_id')}")
        print(f"    Recurrence Rule: {event.get('recurrence_rule')}")
        print(f"    Link: {event.get('link')}")
    
    print("\n" + "="*80)

