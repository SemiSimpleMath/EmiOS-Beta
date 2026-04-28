# scheduler_tool.py
from typing import Dict, Any
from datetime import datetime, timezone, timedelta
from app.assistant.utils.time_utils import parse_time_string
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, Message
from app.assistant.utils.time_utils import local_to_utc
from app.assistant.scheduler.pydantic_types.base_event_data import BaseEventData

# Import the repository manager
from app.assistant.event_repository.event_repository import EventRepositoryManager

from app.assistant.ServiceLocator.service_locator import DI

import uuid


from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


class SchedulerTool(BaseTool):
    """
    Unified tool to manage scheduler events.
    Based on the 'action' provided in the arguments, this tool can:
      - Create an interval-based repeating event.
      - Create a one-time event.
      - Delete an existing scheduler event.
      - Fetch scheduler events.

    All incoming times are assumed to be in local time and are converted to UTC for internal processing.
    When sending results back to the agent, times are converted back to local time.
    """

    def __init__(self):
        super().__init__('scheduler_tool')
        self.do_lazy_init = True
        self.repo_manager = None

    def lazy_init(self):
        self.repo_manager = EventRepositoryManager()
        self.do_lazy_init = False

    def execute(self, tool_message: 'ToolMessage') -> ToolResult:
        if self.do_lazy_init:
            self.lazy_init()
        try:
            logger.info("Starting SchedulerTool execution.")
            arguments = tool_message.tool_data.get('arguments', {})
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'
            if not tool_name:
                raise ValueError("Missing tool_name in arguments. Please specify one of: "
                                 "'create_scheduler_event', 'create_repeating_scheduler_event', 'delete_scheduler_event', 'get_scheduler_events'.")
            handler_method = getattr(self, f"handle_{tool_name}", None)
            if not handler_method:
                raise ValueError(f"Unsupported action '{tool_name}'.")
            # Call the appropriate handler method
            return handler_method(arguments, tool_message)

        except Exception as e:
            logger.error("Error in SchedulerTool: %s", e)
            logger.debug("error in SchedulerTool exception details", exc_info=True)
            error_result = make_tool_error(
                error_code="scheduler_execute_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )
            return self.publish_error(error_result)

    def publish_result(self, tool_result: ToolResult) -> ToolResult:
        return tool_result

    def publish_error(self, error_result: ToolResult) -> ToolResult:
        return error_result

    ############################################################################
    # Handler for creating an interval event.
    ############################################################################

    def handle_create_repeating_scheduler_event(self, arguments: Dict[str, Any], tool_message: 'ToolMessage') -> ToolResult:
        event_data = arguments
        try:
            logger.info("Handling create_interval_event action.")

            # Convert local times to UTC
            start_date_local = event_data.get('start_date')
            end_date_local = event_data.get('end_date')
            start_date = local_to_utc(start_date_local).isoformat() if start_date_local else None
            end_date = local_to_utc(end_date_local).isoformat() if end_date_local else None

            event_id = str(uuid.uuid4())
            interval = event_data.get('interval')
            title = event_data.get('title')
            payload_message = event_data.get('payload_message')
            task_type = event_data.get('task_type')
            importance = event_data.get('importance', 2)
            sound = event_data.get('sound', 'default').lower()

            event_payload = {
                'title': title,
                'payload_message': payload_message,
                'task_type': task_type,
                'importance': importance,
                'sound': sound,
            }

            event = BaseEventData(
                event_id=event_id,
                event_type='interval',
                interval=interval,
                start_date=start_date,
                end_date=end_date,
                event_payload=event_payload
            )

            # Store in the repo and schedule
            self.repo_manager.store_event(id=event_id, event_data=event.model_dump(), data_type="scheduler")
            logger.info(f"Scheduling event: {event}")
            result = DI.scheduler.event_scheduler.create_event(event)

            self._update_repo_cache()

            return self.publish_result(
                ToolResult(result_type="success", content=f"Interval event scheduled: {result}")
            )

        except Exception as e:
            logger.error("Error in handle_create_interval_event")
            logger.debug("error in handle_create_interval_event exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="scheduler_create_interval_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )


    def handle_create_scheduler_event(self, arguments: Dict[str, Any], tool_message: 'ToolMessage') -> ToolResult:

        event_data = arguments
        try:
            logger.info("Handling create_scheduler_event action (one-time event).")

            # Convert local times to UTC
            start_date_local = event_data.get('start_date')
            end_date_local = event_data.get('end_date')
            start_date = local_to_utc(start_date_local).isoformat() if start_date_local else None
            end_date = local_to_utc(end_date_local).isoformat() if end_date_local else None

            event_id = str(uuid.uuid4())
            title = event_data.get('title')
            payload_message = event_data.get('payload_message')
            task_type = event_data.get('task_type')
            importance = event_data.get('importance', 2)
            sound = event_data.get('sound', 'default').lower()

            event_payload = {
                'title': title,
                'payload_message': payload_message,
                'task_type': task_type,
                'importance': importance,
                'sound': sound,
            }

            event = BaseEventData(
                event_id=event_id,
                event_type='one_time_event',
                interval=None,
                start_date=start_date,
                end_date=end_date,
                event_payload=event_payload
            )

            self.repo_manager.store_event(id=event_id, event_data=event.model_dump(), data_type="scheduler")
            result = DI.scheduler.event_scheduler.create_event(event)
            self._update_repo_cache()

            return self.publish_result(
                ToolResult(result_type="success", content=f"One-time event scheduled: {result}")
            )

        except Exception as e:
            logger.error("Error in handle_create_scheduler_event")
            logger.debug("error in handle_create_scheduler_event exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="scheduler_create_event_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )


    ############################################################################
    # Handler for deleting a scheduler event.
    ############################################################################
    def handle_delete_scheduler_event(self, arguments: Dict[str, Any], tool_message: 'ToolMessage'):
        """
        Delete a scheduler event.
        If cascade=True, also deletes all linked children from the event hierarchy.
        """
        try:
            logger.info("Handling delete_scheduler_event action.")
            event_id = arguments.get('event_id')
            cascade = arguments.get('cascade', False)
            
            if not event_id:
                raise ValueError("Missing 'event_id' for deleting scheduler event.")

            deleted_children = []
            
            # Handle cascade deletion
            if cascade:
                deleted_children = self._cascade_delete_children('scheduler', event_id)

            logger.info(f"Attempting to delete scheduler event with ID: {event_id}")
            # Delete the event from the repository.
            self.repo_manager.delete_event(event_id, data_type="scheduler")

            delete_payload: Dict[str, Any] = {'delete_id': event_id}

            logger.info(f"Requesting delete event for event_id: {event_id}")

            result = DI.scheduler.event_scheduler.delete_event(event_id)
            
            # Clean up EventNode if it exists
            self._cleanup_event_node('scheduler', event_id)

            self._update_repo_cache()

            if cascade and deleted_children:
                success_message = f"{result}. Also deleted {len(deleted_children)} linked children: {deleted_children}"
            else:
                success_message = f"{result}."
            tool_result = ToolResult(result_type="success", content=success_message)
            return self.publish_result(tool_result)

        except Exception as e:
            logger.error("Error in handle_delete_scheduler_event: %s", e)
            logger.debug("error in handle_delete_scheduler_event exception details", exc_info=True)
            error_result = make_tool_error(
                error_code="scheduler_delete_event_failed",
                message=f"Error in DeleteSchedulerEvent: {e}",
                abort_policy="abort_tool",
                retryable=False,
            )
            return self.publish_error(error_result)
    
    def _cascade_delete_children(self, source_system: str, source_id: str) -> list:
        """Delete all children linked to this event in the EventNode graph."""
        deleted = []
        try:
            from app.assistant.event_graph import get_event_node_manager
            mgr = get_event_node_manager()
            
            hierarchy = mgr.get_event_hierarchy(f"{source_system}:{source_id}")
            if not hierarchy:
                return deleted
            
            # Get all descendants and delete them
            subtree = hierarchy.get('subtree', [])
            parent_node_id = hierarchy['node']['node_id']
            
            for node in subtree:
                if node['node_id'] != parent_node_id:
                    node_with_sources = mgr.get_node_with_sources(node['node_id'])
                    if node_with_sources:
                        for source in node_with_sources.get('sources', []):
                            child_deleted = self._delete_source_item(source)
                            if child_deleted:
                                deleted.append(child_deleted)
                                
        except Exception as e:
            logger.warning(f"Error in cascade delete: {e}")
        return deleted
    
    def _delete_source_item(self, source: dict) -> str:
        """Delete an item from its source system."""
        try:
            source_system = source.get('source_system')
            source_id = source.get('source_id')
            
            if source_system == 'scheduler':
                DI.scheduler.event_scheduler.delete_event(source_id)
                self.repo_manager.delete_event(source_id, data_type="scheduler")
                return f"scheduler:{source_id}"
            elif source_system == 'google_calendar':
                # Would need calendar service - skip for now
                logger.info(f"Skipping calendar deletion for {source_id} - use delete_calendar_event")
                return None
            elif source_system == 'google_tasks':
                raise NotImplementedError("Google Tasks deletion is not yet implemented.")
        except Exception as e:
            logger.warning(f"Error deleting {source}: {e}")
        return None
    
    def _cleanup_event_node(self, source_system: str, source_id: str):
        """Remove the EventNode after deleting from source."""
        try:
            from app.assistant.event_graph import get_event_node_manager
            mgr = get_event_node_manager()
            node = mgr.get_node_by_source(source_system, source_id)
            if node:
                mgr.delete_node(node['node_id'], cascade=False)
        except Exception as e:
            logger.debug(f"No EventNode to clean up for {source_system}:{source_id}: {e}")

    ############################################################################
    # Handler for fetching scheduler events.
    ############################################################################
    def handle_get_scheduler_events(self, arguments: Dict[str, Any], tool_message: 'ToolMessage') -> ToolResult:

        start_date_str = arguments.get('start_date')
        end_date_str = arguments.get('end_date')

        # Default to local midnight → +7 days when no dates provided
        if not start_date_str or not end_date_str:
            today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if not start_date_str:
                start_date_str = today_midnight.isoformat()
            if not end_date_str:
                end_date_str = (today_midnight + timedelta(days=7)).isoformat()

        # Convert to UTC if provided
        start_date = local_to_utc(start_date_str) if start_date_str else None
        end_date = local_to_utc(end_date_str) if end_date_str else None

        scheduler = DI.scheduler
        fetched_events = scheduler.event_scheduler.get_events(start_date=start_date, end_date=end_date)

        # PHASE 1: Process all events (no DB writes)
        events_to_store = []
        event_sync_list = []
        
        for event in fetched_events:
            event_id = event.event_id
            event_type = event.event_type

            if event_id:
                # Generate store_id for repeating events
                if event_type == "interval":
                    occurrence = event.event_payload.get("occurrence")
                    if occurrence:
                        store_id = f"{event_id}_{occurrence}"
                    else:
                        store_id = event_id  # Fallback if no start_date
                else:
                    store_id = event_id  # Non-repeating events use event_id directly

                data = event.model_dump()
                if isinstance(data.get("event_payload"), dict):
                    data["occurrence"] = data["event_payload"].get("occurrence")

                events_to_store.append((store_id, data, event_id))
                event_sync_list.append(store_id)

        # PHASE 2: Batch write to database
        if events_to_store:
            logger.debug(f"Batch writing {len(events_to_store)} scheduler events to repo")
            for store_id, data, event_id in events_to_store:
                self.repo_manager.store_event(
                    id=store_id,
                    event_data=data,
                    data_type="scheduler",
                    event_id=event_id
                )
            self.repo_manager.sync_events_with_server(event_sync_list, "scheduler")

        fetch_events_result = ToolResult(
            result_type="scheduler_events",
            content="Successfully retrieved scheduler events.",
            data_list=[e.model_dump() for e in fetched_events]
        )
        return self.publish_result(fetch_events_result)

    def _apply_filters(self, event, args: dict) -> bool:
        """
        Applies filtering criteria to the event using the provided arguments.

        Expected filter arguments:
          - start_date (str, optional): For one-time events, include only if event.start_date >= start_date.
          - end_date (str, optional): For one-time events, include only if event.start_date <= end_date.

        Note: 'tool_name' and 'event_type' are used for routing and are ignored.
              Recurring events (with event_type 'interval_event') are not filtered by date.
              Any other provided arguments are compared for exact equality.
        """
        # Keys to ignore for filtering purposes.
        ignore_keys = {'tool_name', 'event_type', 'start_date', 'end_date'}

        # Check if event is recurring.
        event_type = getattr(event, "event_type", None)
        if event_type == "interval_event":
            # For recurring events, ignore date filters.
            for key, filter_value in args.items():
                if key in ignore_keys:
                    continue
                event_attr = getattr(event, key, None)
                if event_attr != filter_value:
                    logger.debug(
                        f"Recurring event {getattr(event, 'event_id', 'unknown')} filtered out: "
                        f"{key} ({event_attr}) != {filter_value}."
                    )
                    return False
            return True

        # For one-time events, process date filters.
        start_filter = args.get('start_date')
        end_filter = args.get('end_date')

        if start_filter or end_filter:
            event_start = getattr(event, 'start_date', None)
            if not event_start:
                logger.debug(f"Event {getattr(event, 'event_id', 'unknown')} missing 'start_date'.")
                return False

            # Parse event_start.
            if isinstance(event_start, str):
                try:
                    event_start_dt = parse_time_string(event_start)
                except Exception as e:
                    logger.error(
                        f"Error parsing event start_date for event {getattr(event, 'event_id', 'unknown')}: {e}"
                    )
                    return False
            elif isinstance(event_start, datetime):
                event_start_dt = event_start if event_start.tzinfo else event_start.replace(tzinfo=timezone.utc)
            else:
                return False

            if start_filter:
                try:
                    start_filter_dt = parse_time_string(start_filter) if isinstance(start_filter, str) else start_filter
                except Exception as e:
                    logger.error(f"Error parsing start_date filter: {e}")
                    return False
                if event_start_dt < start_filter_dt:
                    logger.debug(
                        f"One-time event {getattr(event, 'event_id', 'unknown')} filtered out: "
                        f"{event_start_dt} is before {start_filter_dt}."
                    )
                    return False

            if end_filter:
                try:
                    end_filter_dt = parse_time_string(end_filter) if isinstance(end_filter, str) else end_filter
                except Exception as e:
                    logger.error(f"Error parsing end_date filter: {e}")
                    return False
                if event_start_dt > end_filter_dt:
                    logger.debug(
                        f"One-time event {getattr(event, 'event_id', 'unknown')} filtered out: "
                        f"{event_start_dt} is after {end_filter_dt}."
                    )
                    return False

        # Process any additional filters via exact equality.
        for key, filter_value in args.items():
            if key in ignore_keys:
                continue

            event_attr = getattr(event, key, None)
            if event_attr is None:
                logger.debug(f"Event {getattr(event, 'event_id', 'unknown')} missing attribute '{key}'.")
                return False

            if event_attr != filter_value:
                logger.debug(
                    f"Event {getattr(event, 'event_id', 'unknown')} filtered out: {key} ({event_attr}) != {filter_value}."
                )
                return False

        return True

    def _update_repo_cache(self):
        ## Logic to update the repo cache

        # Calculate today's midnight and seven days from midnight.
        today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) # Use local time so we get proper day
        seven_days_later = today_midnight + timedelta(days=7)

        # Format the dates in ISO 8601 format.
        start_date_str = today_midnight.isoformat()
        end_date_str = seven_days_later.isoformat()

        # Call the cache refresh method with these date boundaries.

        fetched_events = self._refresh_events_cache(start_date_str, end_date_str)

        # Notifies the UI to get refreshed events from repo
        repo_msg = Message(
            data_type="repo_update",
            sender="SchedulerTool",
            receiver=None,
            data={
                "data_type": "scheduler",
                "action": "refresh",
            }
        )
        repo_msg.event_topic = "repo_update"
        DI.event_hub.publish(repo_msg)
        ##

    def _refresh_events_cache(self, start_date_str: str = None, end_date_str: str = None) -> list:
        """
        Refresh the events cache by fetching events from the server and updating the local repository.

        Parameters:
          start_date_str (str, optional): Local start date filter.
          end_date_str (str, optional): Local end date filter.

        Returns:
          list: The list of fetched events.
        """
        logger.info("Refreshing events cache")
        start_date = local_to_utc(start_date_str) if start_date_str else None
        end_date = local_to_utc(end_date_str) if end_date_str else None

        scheduler = DI.scheduler
        fetched_events = scheduler.event_scheduler.get_events(start_date=start_date, end_date=end_date)

        # PHASE 1: Process all events (no DB writes)
        events_to_store = []
        event_sync_list = []
        
        for event in fetched_events:
            event_id = event.event_id
            event_type = event.event_type
            if not event_id:
                continue

            # For interval events, get occurrence from payload
            if event_type == "interval":
                occurrence = event.event_payload.get("occurrence")
                if occurrence:
                    store_id = f"{event_id}_{occurrence}"
                else:
                    store_id = event_id

                # Flatten occurrence into the root level
                data = event.model_dump()
                if isinstance(data.get("event_payload"), dict):
                    data["occurrence"] = data["event_payload"].get("occurrence")
            else:
                store_id = event_id
                data = event.model_dump()

            events_to_store.append((store_id, data, event_id))
            event_sync_list.append(store_id)

        # PHASE 2: Batch write to database
        if events_to_store:
            logger.debug(f"Batch writing {len(events_to_store)} scheduler events to repo")
            for store_id, data, event_id in events_to_store:
                self.repo_manager.store_event(
                    id=store_id,
                    event_data=data,
                    data_type="scheduler",
                    event_id=event_id
                )
            self.repo_manager.sync_events_with_server(event_sync_list, "scheduler")
        
        return fetched_events


if __name__ == "__main__":
    from app.assistant.utils.pydantic_classes import ToolMessage
    from app.assistant.lib.tools.get_scheduler_events.tool_forms.tool_forms import (
        get_scheduler_events_args,
        get_scheduler_events_arguments
    )
    # Step 1: Prepare the tool message
    args_model = get_scheduler_events_args(start_date="", end_date="")
    scheduler_args_model = get_scheduler_events_arguments(
        tool_name='get_scheduler_events',
        arguments=args_model
    )
    tool_message = ToolMessage(
        tool_name='get_scheduler_events',
        tool_data=scheduler_args_model.model_dump()
    )

    # Step 2: Initialize the SchedulerTool
    scheduler_tool = SchedulerTool()

    # Step 3: Execute the tool with the prepared message
    result = scheduler_tool.execute(tool_message)

    # Step 4: Print the result
    print(result.model_dump())
