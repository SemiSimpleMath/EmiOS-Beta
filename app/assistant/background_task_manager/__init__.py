# Background Task Manager
# Runs periodic tasks independently of the UI

from app.assistant.background_task_manager.background_task_manager import (
    BackgroundTaskManager,
    BackgroundTask,
    get_background_task_manager,
    start_background_tasks,
    stop_background_tasks
)

__all__ = [
    'BackgroundTaskManager',
    'BackgroundTask', 
    'get_background_task_manager',
    'start_background_tasks',
    'stop_background_tasks'
]

