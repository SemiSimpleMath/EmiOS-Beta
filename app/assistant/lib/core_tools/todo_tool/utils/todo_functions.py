# app/assistant/lib/core_tools/todo_tools/utils/todo_functions.py


from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError

from app.assistant.lib.google_auth.account_ids import TASKS_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth.google_credentials import load_google_credentials
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_time_string, to_rfc3339_z

logger = get_logger(__name__)

# Define the scope for Google Tasks
SCOPES = ["https://www.googleapis.com/auth/tasks"]


class TaskError(Exception):
    """Custom exception for task operations."""
    pass


class GoogleTasksService:
    _service: Optional[Resource] = None

    @classmethod
    def initialize(cls):
        """Initializes the Google Tasks API service if it hasn’t been already."""
        if cls._service is None:
            creds = load_google_credentials(
                account_id=TASKS_GOOGLE_ACCOUNT_ID,
                required_scopes=SCOPES,
            )
            cls._service = cls._build_service(creds)
            logger.info("Google Tasks service initialized.")

    @classmethod
    def get_service(cls) -> Resource:
        """Returns the authenticated Google Tasks service, initializing if necessary."""
        if cls._service is None:
            logger.info("Google Tasks service is not initialized. Initializing now...")
            cls.initialize()
        return cls._service

    @classmethod
    def _build_service(cls, creds: Credentials) -> Resource:
        """Build a Google Tasks API client from shared OAuth credentials."""
        try:
            service = build("tasks", "v1", credentials=creds)
            logger.info("Authenticated Google Tasks service created successfully.")
            return service
        except Exception as e:
            logger.error(f"Failed to build Google Tasks service: {e}")
            raise TaskError(f"Service creation failed: {e}") from e


def get_unique_task_id(task: Dict[str, Any]) -> str:
    """
    Returns the unique identifier for a task.
    If the task is recurring, this returns its recurringTaskId;
    otherwise, it falls back to its regular id.
    """
    return task.get("recurringTaskId") or task.get("id") or task.get("task_id")


def add_task(
        tasklist_id: str,
        title: str,
        due_date: Optional[str] = None,
        priority: Optional[int] = 1,
        parent: Optional[str] = None
) -> str:
    """
    Adds a task to a specific task list on Google Tasks.

    Args:
        tasklist_id: ID of the task list where the task will be added.
        title: Title of the task.
        due_date: Optional due date (ISO 8601 string).
        priority: Optional priority level.
        parent: Optional parent task ID (if creating a subtask).

    Returns:
        The newly created task's ID.
    """
    service = GoogleTasksService.get_service()
    task_body = {'title': title}

    if due_date is not None:
        assert isinstance(due_date, str), "Expected RFC3339 string for due_date"
        task_body["due"] = due_date

    if parent:
        task_body['parent'] = parent

    if priority:
        existing_notes = task_body.get('notes', '')
        task_body['notes'] = f"Priority: {priority}"
        if existing_notes:
            task_body['notes'] = f"{existing_notes}\nPriority: {priority}"

    try:
        result = service.tasks().insert(tasklist=tasklist_id, body=task_body).execute()
        task_id = result.get('id')
        logger.info(f"Task '{result.get('title')}' added to task list ID '{tasklist_id}' with priority '{priority}'.")
        return task_id
    except HttpError as http_err:
        logger.error(f"HTTP error while adding task '{title}': {http_err}")
        raise TaskError(f"Failed to add task '{title}': {http_err}") from http_err
    except Exception as e:
        logger.error(f"Failed to add task '{title}': {e}")
        raise TaskError(f"Failed to add task '{title}': {e}") from e


def create_tasklist(title: str) -> str:
    """
    Creates a new task list.

    Args:
        title: Title of the new task list.

    Returns:
        ID of the created task list.
    """
    service = GoogleTasksService.get_service()

    tasklist_body = {'title': title}
    try:
        result = service.tasklists().insert(body=tasklist_body).execute()
        tasklist_id = result.get('id')
        logger.info(f"Task list '{title}' created with ID '{tasklist_id}'.")
        return tasklist_id
    except HttpError as http_err:
        logger.error(f"HTTP error while creating task list '{title}': {http_err}")
        raise TaskError(f"Failed to create task list '{title}': {http_err}") from http_err
    except Exception as e:
        logger.error(f"Failed to create task list '{title}': {e}")
        raise TaskError(f"Failed to create task list '{title}': {e}") from e


def get_all_tasklists() -> List[Dict[str, Any]]:
    """
    Retrieves all task lists.

    Returns:
        A list of task list dictionaries.
    """
    service = GoogleTasksService.get_service()

    try:
        tasklists = []
        request = service.tasklists().list(maxResults=100)
        while request is not None:
            response = request.execute()
            tasklists.extend(response.get('items', []))
            request = service.tasklists().list_next(previous_request=request, previous_response=response)
        logger.debug(f"Retrieved {len(tasklists)} task lists.")
        return tasklists
    except HttpError as http_err:
        logger.error(f"HTTP error while retrieving task lists: {http_err}")
        raise TaskError(f"Failed to retrieve task lists: {http_err}") from http_err
    except Exception as e:
        logger.error(f"Failed to retrieve task lists: {e}")
        raise TaskError(f"Failed to retrieve task lists: {e}") from e


def find_tasklist_id_by_name(tasklist_name: str) -> Optional[str]:
    """
    Finds a task list ID by its human-readable name.

    Args:
        tasklist_name: Human-readable name of the task list.

    Returns:
        The task list ID if found, else None.
    """
    tasklists = get_all_tasklists()
    for tasklist in tasklists:
        if tasklist.get('title').lower() == tasklist_name.lower():
            print(tasklist)
            logger.info(f"Found task list '{tasklist_name}' with ID '{tasklist.get('id')}'.")
            return tasklist.get('id')

    logger.warning(f"Task list '{tasklist_name}' not found.")
    return None


def list_tasks_with_subtasks(tasklist_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Retrieves and organizes all tasks with their subtasks, including priority information.

    Args:
        tasklist_id: ID of the task list. If None, retrieves tasks from all task lists.

    Returns:
        A list of parent tasks, each containing a list of their subtasks with priority.
    """
    service = GoogleTasksService.get_service()
    tasks = []

    try:
        if tasklist_id:
            # Retrieve tasks from a specific task list
            logger.debug(f"Retrieving tasks from task list ID '{tasklist_id}'.")
            request = service.tasks().list(tasklist=tasklist_id, showCompleted=True, showHidden=True, maxResults=100)
            while request is not None:
                response = request.execute()
                tasks.extend(response.get('items', []))
                request = service.tasks().list_next(previous_request=request, previous_response=response)
            logger.debug(f"Retrieved {len(tasks)} tasks from task list ID '{tasklist_id}'.")
        else:
            # Retrieve tasks from all task lists
            tasklists = get_all_tasklists()
            tasklist_ids = [tl['id'] for tl in tasklists]
            logger.debug(f"Retrieving tasks from {len(tasklist_ids)} task lists.")
            for tl_id in tasklist_ids:
                logger.debug(f"Retrieving tasks from task list ID '{tl_id}'.")
                req = service.tasks().list(tasklist=tl_id, showCompleted=True, showHidden=True, maxResults=100)
                while req is not None:
                    resp = req.execute()
                    tasks.extend(resp.get('items', []))
                    req = service.tasks().list_next(previous_request=req, previous_response=resp)
                logger.debug(f"Retrieved {len(tasks)} tasks from task list ID '{tl_id}'.")

        if not tasks:
            logger.info('No tasks found.')
            return []

        # Separate parent tasks and subtasks
        parent_tasks = {task['id']: {**task, 'subtasks': []} for task in tasks if 'parent' not in task}
        sub_tasks = [task for task in tasks if 'parent' in task]

        for sub_task in sub_tasks:
            parent_id = sub_task.get('parent')
            if parent_id in parent_tasks:
                parent_tasks[parent_id]['subtasks'].append(sub_task)
                logger.debug(f"Added subtask '{sub_task['title']}' under parent ID '{parent_id}'.")
            else:
                # Handle subtasks without a valid parent
                parent_tasks.setdefault('orphan_subtasks', []).append(sub_task)
                logger.warning(f"Orphan subtask '{sub_task['title']}' found without a valid parent.")

        organized_tasks = list(parent_tasks.values())

        # Optionally, handle orphan subtasks separately
        if 'orphan_subtasks' in parent_tasks:
            logger.warning("\nSubtasks without a valid parent:")
            for st in parent_tasks['orphan_subtasks']:
                priority = extract_priority(st.get('notes', ''))
                logger.warning(f"  - {st['title']} (Due: {st.get('due', 'No due date')}, Priority: {priority})")

        logger.info("Tasks and their subtasks have been organized successfully.")
        return organized_tasks

    except HttpError as http_err:
        logger.error(f"HTTP error while listing tasks: {http_err}")
        raise TaskError(f"Failed to list tasks: {http_err}") from http_err
    except Exception as e:
        logger.error(f"Failed to list tasks: {e}")
        raise TaskError(f"Failed to list tasks: {e}") from e


def update_task(
        task_id: str,
        new_title: Optional[str] = None,
        new_due_date: Optional[datetime] = None,
        new_priority: Optional[str] = None,
        days_offset: Optional[int] = None,
        completed: Optional[bool] = None,  # ✅ Fix: Default to None
        description: Optional[str] = None,
) -> dict:
    """
    Updates a task's details, including title, due date, priority, completion status, and shifting due dates.

    Args:
        task_id: ID of the task to update.
        new_title: New title for the task (optional).
        new_due_date: New due date as a datetime object (optional).
        new_priority: New priority level for the task (e.g., "high", "normal", "low") (optional).
        days_offset: Number of days to shift the due date (can be positive or negative) (optional).
        completed: Boolean indicating if the task is completed (optional).

    Raises:
        TaskError: If no updates are provided or the task is not found.
    """
    service = GoogleTasksService.get_service()

    task_updates = {}
    tasklist_id, task = get_tasklist_and_task_by_task_id(task_id)

    if not task:
        logger.error(f"Task with ID '{task_id}' not found for updating.")
        raise TaskError(f"Task with ID '{task_id}' not found.")

    # ✅ Only update fields if they are provided
    if new_title is not None:
        task_updates['title'] = new_title
        logger.debug(f"Updating title to '{new_title}' for task ID '{task_id}'.")

    if new_due_date is not None:
        task_updates['due'] = new_due_date.isoformat().replace('+00:00', 'Z')

        logger.debug(f"Updating due date to '{task_updates['due']}' for task ID '{task_id}'.")

    if completed is not None:
        if completed:
            task_updates['status'] = 'completed'
            task_updates['completed'] = to_rfc3339_z(datetime.now(timezone.utc).replace(microsecond=0))
            logger.debug(f"Marking task ID '{task_id}' as completed at {task_updates['completed']}.")
        else:
            task_updates['status'] = 'needsAction'

    if description is not None:
        task_updates["notes"] = description
        logger.debug(f"Updating notes for task ID '{task_id}'.")

    if days_offset is not None:
        current_due_str = task.get('due')
        if current_due_str:
            try:
                # Parse RFC3339 with 'Z' correctly
                current_due = parse_time_string(current_due_str)  # Already handles Z and sets tzinfo
                new_due = current_due + timedelta(days=days_offset)
                task_updates['due'] = to_rfc3339_z(new_due)
                logger.debug(
                    f"Shifting due date by {days_offset} days to '{task_updates['due']}' for task ID '{task_id}'.")
            except ValueError as ve:
                logger.error(f"Invalid due date format for task ID '{task_id}': {current_due_str}")
                raise TaskError(f"Invalid due date format: {current_due_str}") from ve
        else:
            logger.warning(f"Task with ID '{task_id}' does not have a due date to shift. Skipping offset change.")


    if new_priority is not None:
        existing_notes = task.get('notes', '')
        notes_lines = existing_notes.split('\n') if existing_notes else []
        notes_lines = [line for line in notes_lines if not line.startswith("Priority:")]
        notes_lines.append(f"Priority: {new_priority}")
        updated_notes = '\n'.join(notes_lines)
        task_updates['notes'] = updated_notes
        logger.debug(f"Updating priority to '{new_priority}' for task ID '{task_id}'.")

    if not task_updates:
        logger.error("No updates provided for the task.")
        raise TaskError("No updates provided.")

    try:
        updated_task = service.tasks().patch(
            tasklist=tasklist_id,
            task=task_id,
            body=task_updates
        ).execute()

        logger.info(f"Task '{updated_task.get('title')}' with ID '{task_id}' has been updated successfully.")
        return updated_task
    except HttpError as http_err:
        logger.error(f"HTTP error updating task ID '{task_id}': {http_err}")
        raise TaskError(f"Failed to update task ID '{task_id}': {http_err}") from http_err
    except Exception as e:
        logger.error(f"Error updating task ID '{task_id}': {e}")
        raise TaskError(f"Failed to update task ID '{task_id}': {e}") from e


def get_tasklist_and_task_by_task_id(task_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Finds the task list ID and task object that contains the given task ID.

    Args:
        task_id: ID of the task to search for.

    Returns:
        Tuple (tasklist_id, task) if found, else None.
    """
    service = GoogleTasksService.get_service()

    try:
        tasklists = service.tasklists().list().execute().get("items", [])
    except HttpError as http_err:
        logger.error(f"HTTP error retrieving task lists: {http_err}")
        return None
    except Exception as e:
        logger.error(f"Error retrieving task lists: {e}")
        return None

    for tasklist in tasklists:
        tasklist_id = tasklist["id"]  # Keep as tasklist_id
        try:
            task = service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
            logger.debug(f"Found task '{task.get('title')}' in task list ID '{tasklist_id}'.")
            return tasklist_id, task  # Now returning correct naming convention
        except HttpError as http_err:
            if http_err.resp.status == 404:
                continue  # Task not found in this list, try the next one
            logger.error(f"HTTP error retrieving task '{task_id}' from task list '{tasklist_id}': {http_err}")

    logger.warning(f"Task ID '{task_id}' not found in any task list.")
    return None  # Task not found


def delete_task(task_id: str, tasklist_id: Optional[str] = None) -> None:
    """
    Deletes a task from a task list. If the tasklist_id is not provided, it will search through all task lists.

    Args:
        task_id: ID of the task to delete.
        tasklist_id: ID of the task list. If None, it searches for the task across all task lists.

    Raises:
        TaskError: If the task or tasklist is not found, or deletion fails.
    """
    service = GoogleTasksService.get_service()

    if tasklist_id is None:
        logger.info(f"Tasklist ID not provided. Searching for task ID '{task_id}' in all task lists...")
        result = get_tasklist_and_task_by_task_id(task_id)

        if not result:
            logger.error(f"Task ID '{task_id}' not found in any task list.")
            raise TaskError(f"Task ID '{task_id}' not found in any task list.")

        tasklist_id, _ = result  # Now safe to unpack

    try:
        service.tasks().delete(tasklist=tasklist_id, task=task_id).execute()
        logger.info(f"Task ID '{task_id}' has been deleted successfully from task list ID '{tasklist_id}'.")
    except HttpError as http_err:
        if http_err.resp.status == 404:
            logger.warning(f"Task ID '{task_id}' not found in task list ID '{tasklist_id}'.")
            raise TaskError(f"Task ID '{task_id}' not found.") from http_err
        logger.error(f"HTTP error deleting task ID '{task_id}': {http_err}")
        raise TaskError(f"Failed to delete task ID '{task_id}': {http_err}") from http_err
    except Exception as e:
        logger.error(f"Error deleting task ID '{task_id}': {e}")
        raise TaskError(f"Failed to delete task ID '{task_id}': {e}") from e


def extract_priority(notes: str) -> str:
    """
    Extracts the priority level from the notes field.

    Args:
        notes: The notes string from a task.

    Returns:
        The priority level if found, else 'normal'.
    """
    for line in notes.split('\n'):
        if line.startswith("Priority:"):
            return line.replace("Priority:", "").strip()
    return "normal"


def search_task_by_name(tasklist_id: str, name: str) -> List[Dict[str, Any]]:
    """
    Searches tasks in a task list by name.

    Args:
        tasklist_id: ID of the task list.
        name: Name or partial name of the task to search.

    Returns:
        A list of tasks matching the name.
    """
    tasks = list_tasks_with_subtasks(tasklist_id)
    matched_tasks = [task for task in tasks if name.lower() in task['title'].lower()]
    logger.info(f"Found {len(matched_tasks)} task(s) matching '{name}' in tasklist ID '{tasklist_id}'.")
    return matched_tasks


def filter_tasks_by_date_range(tasks: List[Dict[str, Any]],
        start_date: Optional[datetime],
        end_date: Optional[datetime]
) -> List[Dict[str, Any]]:
    """
    Filters tasks within a date range.

    Args:
        tasklist_id: ID of the task list.
        start_date: Start of the date range (inclusive).
        end_date: End of the date range (inclusive).

    Returns:
        A list of tasks within the specified date range.
    """

    filtered_tasks: List[Dict[str, Any]] = []

    # Ensure start_date and end_date are timezone-aware (convert to UTC if needed)
    if start_date and start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    if end_date and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)

    print("start_date:", start_date)
    print("end_date", end_date)

    for task in tasks:
        raw_due = task.get("due")
        if not raw_due:
            filtered_tasks.append(task)
            continue

        try:
            task_due_date = parse_time_string(raw_due)
        except Exception as e:
            logger.debug(f"Skipping unparsable due date '{raw_due}': {e}")
            continue

        if not isinstance(task_due_date, datetime):
            # skip non-datetime results
            continue

        if task_due_date.tzinfo is None:
            task_due_date = task_due_date.replace(tzinfo=timezone.utc)

        # Apply date filtering
        if (not start_date or task_due_date >= start_date) and \
                (not end_date or task_due_date <= end_date):
            filtered_tasks.append(task)

    logger.info(f"Filtered tasks count: {len(filtered_tasks)} within the date range.")
    return filtered_tasks


def retrieve_all_tasks_across_lists() -> List[Dict[str, Any]]:
    """
    Retrieves all tasks across all task lists.

    Returns:
        A list of all task dictionaries from all task lists.
    """
    service = GoogleTasksService.get_service()

    all_tasks = []
    try:
        tasklists = get_all_tasklists()
        logger.debug(f"Retrieving tasks from {len(tasklists)} task lists.")
        for tasklist in tasklists:
            tasklist_id = tasklist['id']
            tasklist_title = tasklist['title']
            logger.debug(f"Retrieving tasks from task list '{tasklist_title}' (ID: {tasklist_id}).")
            tasks = list_tasks_with_subtasks(tasklist_id=tasklist_id)
            for task in tasks:
                task['tasklist_title'] = tasklist_title  # Add task list title for reference
            all_tasks.extend(tasks)
        logger.info(f"Retrieved a total of {len(all_tasks)} tasks across all task lists.")
        return all_tasks
    except TaskError as e:
        logger.error(f"Failed to retrieve all tasks: {e}")
        raise TaskError(f"Failed to retrieve all tasks: {e}") from e


if __name__ == "__main__":
    import sys

    logger.info("Running main commands for testing...")

    try:
        GoogleTasksService.get_service()

        # Test finding or creating "Packing List"
        packing_list_id = find_tasklist_id_by_name("Packing List")
        if not packing_list_id:
            logger.info("Packing List not found. Creating one...")
            packing_list_id = create_tasklist("Packing List")
        else:
            logger.info(f"Using existing Packing List with ID: {packing_list_id}")

        # Add a test task
        logger.info("Adding a new task to Packing List...")
        task_snacks_id = add_task(
            tasklist_id=packing_list_id,
            title="Pack snacks",
            due_date=datetime.now() + timedelta(days=1),
            priority="medium"
        )
        logger.info("Added task: 'Pack snacks' to Packing List.")

        # Verify task exists
        logger.info("Fetching all task lists...")
        tasklists = get_all_tasklists()
        logger.info(f"Retrieved {len(tasklists)} task lists.")

        for tasklist in tasklists:
            logger.info(f"Task List: {tasklist['title']} (ID: {tasklist['id']})")

    except Exception as e:
        logger.error(f"An error occurred during testing: {e}")
        sys.exit(1)
