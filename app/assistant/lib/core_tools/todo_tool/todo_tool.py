# app/assistant/lib/core_tools/todo_tools/todo_tool.py


from typing import Any, Dict
from pydantic import ValidationError
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.event_node_cleanup import cascade_delete_children, cleanup_event_node
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error

from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, Message

from app.assistant.lib.core_tools.todo_tool.utils.todo_functions import (
    add_task,
    update_task,
    delete_task,
    find_tasklist_id_by_name,
    create_tasklist,
    retrieve_all_tasks_across_lists,
    filter_tasks_by_date_range,
    TaskError
)
# Import the repository manager
from app.assistant.event_repository.event_repository import EventRepositoryManager

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

from datetime import datetime, timedelta
from app.assistant.utils.time_utils import local_to_utc, parse_time_string, to_rfc3339_z


class ToDoTool(BaseTool):
    """
    Tool to manage To-Do lists and tasks. In addition to the usual operations,
    tasks are also stored in a central repository using the Google ID (if provided)
    """

    def __init__(self):
        super().__init__('todo_tool')
        self.repo_manager = None
        self.handlers = {
            'add_task': self.handle_add_task,
            'get_todo_tasks': self.handle_get_tasks,
            'update_todo_task': self.handle_update_task,
            'delete_todo_task': self.handle_delete_task,
            'create_todo_task': self.handle_add_task,
            'create_tasklist': self.handle_create_tasklist,

        }

        self.do_lazy_init = True

    def lazy_init(self):
        self.repo_manager = EventRepositoryManager()
        self.do_lazy_init = False

    def execute(self, tool_message: 'ToolMessage') -> None:
        if self.do_lazy_init:
            self.lazy_init()
        logger.debug(f"Received tool_message: {tool_message}")
        try:
            arguments = tool_message.tool_data.get('arguments', {})
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'

            if not tool_name:
                raise ValueError("No tool_name specified.")

            handler = self.handlers.get(tool_name)

            if not handler:
                raise ValueError(f"Unsupported tool_name '{tool_name}'.")

            logger.debug(f"Executing handler: {tool_name} with arguments: {arguments}")

            tool_result = handler(arguments)

            return tool_result
        except Exception as e:
            logger.error("Error in ToDoTool execute(): %s", e)
            logger.debug("error in ToDoTool execute() exception details", exc_info=True)
            self.publish_error(tool_message, str(e))

    def publish_result(self, tool_message: 'ToolMessage', tool_result: Any):
        result_msg = ToolMessage(
            data_type='tool_result',
            sender='ToDoTool',
            receiver=None,
            task=tool_message.task,
            tool_name=tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool',
            tool_data=tool_message.tool_data,
            tool_result=tool_result,

        )

        return result_msg

    def publish_error(self, tool_message: 'ToolMessage', error_message: str):
        error_result = make_tool_error(
            error_code="todo_tool_publish_error",
            message=error_message,
            abort_policy="abort_tool",
            retryable=False,
        )
        result_msg = ToolMessage(
            data_type='tool_result',
            sender='ToDoTool',
            receiver=None,
            task=tool_message.task,
            tool_name=tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool',
            tool_data=tool_message.tool_data,
            tool_result=error_result,

        )

        return result_msg

    # Handler Methods

    def handle_add_task(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Creates a new to-do task.
        """
        try:
            tasklist_name = arguments.get('tasklist_name')
            task_name = arguments.get('task_name')
            priority = arguments.get('priority', 1)  # Default to 1 if not provided
            parent_task_id = arguments.get('parent_task_id')

            # Get or create task list
            if tasklist_name:
                tasklist_id = find_tasklist_id_by_name(tasklist_name)
                if not tasklist_id:
                    logger.warning(f"List '{tasklist_name}' not found. Using default list.")
                    tasklist_id = '@default'

            else:
                tasklist_id = '@default'
                logger.warning("No tasklist name provided. Using default list.")

            # Always set a default value for due_date
            if arguments.get('due_date'):
                due_date = arguments.get('due_date')
                due_date = to_rfc3339_z(local_to_utc(due_date))

            else:
                # Default to end of day (23:59:59) local time, then convert to UTC.
                now = datetime.now()
                default_local = datetime(now.year, now.month, now.day, 23, 59, 59)
                due_date = to_rfc3339_z(local_to_utc(default_local))
                logger.debug(f"No due date provided. Defaulting to EOD: {default_local}")

            # Create the task on Google Tasks
            task_id = add_task(
                tasklist_id=tasklist_id,
                title=task_name,
                due_date=due_date,
                priority=priority,
                parent=parent_task_id
            )

            task_payload = {
                'id': task_id,
                'tasklist_id': tasklist_id,
                'title': task_name,
                'due': due_date,
                'priority': priority,
                'parent_task_id': parent_task_id,
                'data_type': 'todo_task'
            }
            self.repo_manager.store_event(
                id=task_id,
                event_data=task_payload,
                data_type="todo_task",
            )

            repo_msg = Message(
                data_type="repo_update",
                sender="ToDoTool",
                receiver=None,
                data={
                    "data_type": "todo_task",
                    "action": "create",
                    "entity_id": task_id
                }
            )

            repo_msg.event_topic = 'repo_update'
            event_hub = DI.event_hub
            event_hub.publish(repo_msg)

            return ToolResult(
                result_type="success",
                content=f"Task '{task_name}' created successfully with priority '{priority}'.",
                data_list=[{"task_id": task_id}]
            )

        except ValidationError as ve:
            logger.error(f"Validation error while adding task: {ve}")
            logger.debug("ToDoTool add_task validation exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_add_task_validation_error",
                message=str(ve),
                abort_policy="abort_tool",
                retryable=False,
            )
        except TaskError as e:
            logger.error(f"Failed to add task: {e}")
            logger.debug("ToDoTool add_task task exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_add_task_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            logger.debug("ToDoTool add_task unexpected exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_add_task_unexpected_error",
                message=f"An unexpected error occurred: {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def handle_get_tasks(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Fetches tasks based on provided filters. Agents generally don't know task list names,
        so this retrieves all tasks and filters by task name if provided.
        
        Note: This is a READ operation (repo=False). It does NOT write to EventRepository.
        Only maintenance tools should use repo_update=True.
        """
        try:
            task_name = arguments.get('task_name')
            raw_start = arguments.get('start_date', None)
            raw_end = arguments.get('end_date', None)
            include_completed = bool(arguments.get("include_completed", False))
            repo_update = arguments.get('repo_update', False)  # Default False for agent reads

            # Retrieve ALL tasks first (since agents don't know tasklist)
            tasks = retrieve_all_tasks_across_lists()

            if not include_completed:
                tasks = [task for task in tasks if str(task.get("status") or "").strip().lower() != "completed"]

            if task_name:
                tasks = [task for task in tasks if task_name.lower() in task['title'].lower()]
                logger.debug(f"Filtered tasks by name '{task_name}': {len(tasks)} found.")

            if raw_start is not None or raw_end is not None:
                start_date = parse_time_string(raw_start) if raw_start else None
                end_date = parse_time_string(raw_end) if raw_end else None
                tasks = filter_tasks_by_date_range(
                    tasks,
                    start_date=start_date,
                    end_date=end_date
                )
                logger.debug(f"Filtered tasks by date range {start_date} to {end_date}: {len(tasks)} found.")

            # Add data_type to all tasks
            for task in tasks:
                task['data_type'] = 'todo_task'
            
            # Only update repository if explicitly requested (maintenance tools)
            if repo_update:
                logger.debug(f"repo_update=True: Storing {len(tasks)} tasks in EventRepository")
                
                # PHASE 1: Collect tasks to store (no DB writes yet)
                tasks_to_store = []
                server_ids = []
                for task in tasks:
                    # Canonical ID strategy (Option A): always use Google's concrete task 'id'
                    task_id = task.get("id") or task.get("task_id")
                    if not task_id:
                        continue
                    tasks_to_store.append((task_id, task))
                    server_ids.append(task_id)
                
                # PHASE 2: Batch write to database
                if tasks_to_store:
                    logger.debug(f"Batch writing {len(tasks_to_store)} todo tasks to repo")
                    for task_id, task in tasks_to_store:
                        self.repo_manager.store_event(task_id, event_data=task, data_type="todo_task")
                    self.repo_manager.sync_events_with_server(server_ids, "todo_task")

            return ToolResult(
                result_type="todo_tasks",
                content=f"Retrieved {len(tasks)} task(s)." if tasks else "No tasks available.",
                data_list=tasks  # Always return a valid list, even if empty
            )

        except Exception as e:
            logger.error(f"Error retrieving tasks: {e}")
            logger.debug("ToDoTool get_tasks exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_get_tasks_failed",
                message=f"Failed to retrieve tasks: {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def handle_update_task(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Updates an existing task and refreshes its record in the repository.

        Args:
            arguments: Dictionary containing task update parameters.

        Returns:
            ToolResult indicating success or failure.
        """
        try:
            task_id = arguments.get('task_id')
            due_date = arguments.get('due_date')
            priority = arguments.get('priority')
            days_offset = arguments.get('days_offset')
            tasklist_name = arguments.get('tasklist_name')
            completed = arguments.get('completed')
            description = arguments.get('description')

            # Convert the new due date from local to UTC if provided.
            new_due_date = (
                local_to_utc(due_date)
                if due_date else None
            )

            # Directly update the task—`update_task()` will handle `tasklist_id` lookup.
            updated_task = update_task(
                task_id=task_id,
                new_due_date=new_due_date,
                new_priority=priority,
                days_offset=days_offset,
                completed=completed,
                description = description
            )

            updated_task["data_type"] = "todo_task"
            self.repo_manager.store_event(id=task_id, event_data=updated_task, data_type="todo_task")

            repo_msg = Message(
                data_type="repo_update",
                sender="ToDoTool",
                receiver=None,
                data={
                    "data_type": "todo_task",
                    "action": "update",
                    "entity_id": task_id
                }
            )
            repo_msg.event_topic = 'repo_update'
            event_hub = DI.event_hub
            event_hub.publish(repo_msg)

            return ToolResult(
                result_type="success",
                content=f"Task '{task_id}' updated successfully."
            )

        except ValidationError as ve:
            logger.error(f"Validation error while updating task: {ve}")
            logger.debug("ToDoTool update_task validation exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_update_task_validation_error",
                message=str(ve),
                abort_policy="abort_tool",
                retryable=False,
            )
        except TaskError as e:
            logger.error(f"Failed to update task: {e}")
            logger.debug("ToDoTool update_task task exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_update_task_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            logger.debug("ToDoTool update_task unexpected exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_update_task_unexpected_error",
                message=f"An unexpected error occurred: {e}",
                abort_policy="abort_tool",
                retryable=False,
            )

    def handle_delete_task(self, arguments: Dict[str, Any]) -> ToolResult:
        """
        Deletes an existing task and removes it from the repository.
        If cascade=True, also deletes all linked children from the event hierarchy.

        Args:
            arguments: Dictionary containing 'task_id' and optional 'cascade'.

        Returns:
            ToolResult indicating success or failure.
        """
        try:
            task_id = arguments.get('task_id')
            cascade = arguments.get('cascade', False)
            
            if not task_id:
                raise ValueError("Missing required 'task_id' when deleting a task.")

            deleted_children = []
            
            # Handle cascade deletion
            if cascade:
                deleted_children = cascade_delete_children('google_tasks', task_id, delete_source_item=self._delete_source_item)

            delete_task(task_id=task_id)

            # Remove from repository
            self.repo_manager.delete_event(task_id, data_type="todo_task")
            
            # Clean up EventNode if it exists
            cleanup_event_node('google_tasks', task_id)

            repo_msg = Message(
                data_type="repo_update",
                sender="ToDoTool",
                receiver=None,
                data={
                    "data_type": "todo_task",
                    "action": "delete",
                    "entity_id": task_id
                }
            )
            repo_msg.event_topic = 'repo_update'
            event_hub = DI.event_hub
            event_hub.publish(repo_msg)

            if cascade and deleted_children:
                return ToolResult(
                    result_type="success",
                    content=f"Task '{task_id}' deleted successfully. Also deleted {len(deleted_children)} linked children: {deleted_children}"
                )
            return ToolResult(
                result_type="success",
                content=f"Task '{task_id}' deleted successfully."
            )

        except TaskError as e:
            logger.error(f"Failed to delete task: {e}")
            logger.debug("ToDoTool delete_task task exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_delete_task_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )
        except Exception as e:
            logger.error(f"Unexpected error during delete: {e}")
            logger.debug("ToDoTool delete_task unexpected exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_delete_task_unexpected_error",
                message=f"An unexpected error occurred: {e}",
                abort_policy="abort_tool",
                retryable=False,
            )
    
    def _delete_source_item(self, source: dict) -> str:
        """Delete an item from its source system."""
        try:
            source_system = source.get('source_system')
            source_id = source.get('source_id')
            
            if source_system == 'scheduler':
                DI.scheduler.event_scheduler.delete_event(source_id)
                self.repo_manager.delete_event(source_id, data_type="scheduler")
                return f"scheduler:{source_id}"
            elif source_system == 'google_tasks':
                delete_task(task_id=source_id)
                self.repo_manager.delete_event(source_id, data_type="todo_task")
                return f"google_tasks:{source_id}"
            elif source_system == 'google_calendar':
                logger.info(f"Skipping calendar deletion for {source_id} - use delete_calendar_event")
                return None
        except Exception as e:
            logger.warning(f"Error deleting {source}: {e}")
        return None
    
    def handle_create_tasklist(self, arguments: Dict[str, Any]) -> ToolResult:
        try:
            list_name = arguments.get('list_name')
            tasklist_id = create_tasklist(title=arguments.get('list_name'))
            return ToolResult(
                result_type="success",
                content=f"Task list '{list_name}' created successfully.",
                data={"tasklist_id": tasklist_id}
            )
        except ValidationError as ve:
            logger.error(f"Validation error while creating task list: {ve}")
            logger.debug("ToDoTool create_tasklist validation exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_create_tasklist_validation_error",
                message=str(ve),
                abort_policy="abort_tool",
                retryable=False,
            )
        except TaskError as e:
            logger.error(f"Failed to create task list: {e}")
            logger.debug("ToDoTool create_tasklist task exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_create_tasklist_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )
        except Exception as e:
            logger.error(f"Failed to create task list: {e}")
            logger.debug("ToDoTool create_tasklist unexpected exception details", exc_info=True)
            return make_tool_error(
                error_code="todo_create_tasklist_unexpected_error",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            )


def main():

    import app.assistant.tests.test_setup # This is just run for the import
    # Initialize GoogleTasksService (shared OAuth via load_google_credentials: encrypted DB store)
    from app.assistant.lib.core_tools.todo_tool.utils.todo_functions import GoogleTasksService
    GoogleTasksService.get_service()

    # Initialize the ToDoTool
    todo_tool = ToDoTool()

    # Construct a ToolMessage for fetching to-do tasks
    tool_message = ToolMessage(
        tool_name="get_todo_tasks",
        tool_data={
            "tool_name": "get_todo_tasks",  # Required inside tool_data
            "arguments": {
                "start_date": None,
                "end_date": None
            }
        },
    )
    #
    # # === Step 1: Create a new task ===
    # create_msg = ToolMessage(
    #     tool_name="create_todo_task",
    #     tool_data={
    #         "tool_name": "create_todo_task",
    #         "arguments": {
    #             "tasklist_name": "Test List",
    #             "task_name": "Test Task",
    #             "due_date": datetime.now().isoformat(),
    #             "priority": 2
    #         }
    #     }
    # )
    # result = todo_tool.execute(create_msg)
    # print("✅ Create Result:", result)

    # data_list = result.data_list
    # task_id = data_list[0].get('task_id')
    # === Step 2: Get all tasks ===
    get_msg = ToolMessage(
        tool_name="get_todo_tasks",
        tool_data={
            "tool_name": "get_todo_tasks",
            "arguments": {}
        }
    )
    result = todo_tool.execute(get_msg)

    data_list = result.data_list

    print(len(data_list))

    print("📋 Get Tasks Result:", result)
    #
    #
    # # === Step 3: Update the due date ===
    # new_due_date = (datetime.now() + timedelta(days=1)).isoformat()
    # update_msg = ToolMessage(
    #     tool_name="update_todo_task",
    #     tool_data={
    #         "tool_name": "update_todo_task",
    #         "arguments": {
    #             'task_id': 'd1dYQjQ5aVRaR0lyalVGcQ',
    #             'due_date': '2025-04-28T00:00:00Z',
    #             'priority': None,
    #             'days_offset': None,
    #             'tasklist_name': None,
    #             'completed': None
    #         }
    #     }
    # )
    # result = todo_tool.execute(update_msg)
    #
    # # Execute the tool with the constructed ToolMessage
    # todo_tool.execute(tool_message)
    #
    # print("🛠️ Update Due Date Result:", result)
    #
    # # === Step 4: Mark the task as completed ===
    # complete_msg = ToolMessage(
    #     tool_name="update_todo_task",
    #     tool_data={
    #         "tool_name": "update_todo_task",
    #         "arguments": {
    #             "task_id": task_id,
    #             "completed": True
    #         }
    #     }
    # )
    # result = todo_tool.execute(complete_msg)
    # print("✅ Mark Complete Result:", result)
    #

if __name__ == "__main__":
    main()
