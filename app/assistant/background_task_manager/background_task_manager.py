# background_task_manager.py
"""
Background Task Manager
=======================

Runs periodic tasks independently of the browser/UI.
These tasks do not require user interaction and should run even when no browser tab is open.

Tasks managed (current defaults):
- Watchdog (60s): restart any tasks that stop unexpectedly
- Ticket maintenance (5 min): expire old tickets (>2h), wake snoozed tickets
- Routine runner (60s): scheduled routines from routines.json

Data fetch routines (email, calendar, weather, news, tasks, scheduler) are
managed by RoutineManager via configs/routines.json (fetch_* entries).
"""

import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.error_logging import log_critical_error
from app.assistant.runtime import start_monitored_thread

logger = get_logger(__name__)


TaskFn = Callable[[], None]


class BackgroundTask:
    """
    A single background task with its own thread.

    Key behavior:
    - Uses a stop Event for interruptible waiting (fast shutdown).
    - Maintains thread and counters under a lock for safe status reads.
    - Does not drop the thread handle unless the thread is actually dead.
    - Logs exceptions with tracebacks via logger.exception.
    """

    def __init__(
            self,
            name: str,
            func: TaskFn,
            interval_seconds: int,
            run_immediately: bool = False,
            initial_delay_seconds: int = 0,
    ):
        self.name = name
        self.func = func
        self.interval_seconds = int(interval_seconds)
        self.run_immediately = bool(run_immediately)
        self.initial_delay_seconds = int(initial_delay_seconds)

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._last_run: Optional[datetime] = None
        self._run_count = 0
        self._error_count = 0
        self._running_intent = False  # "Should be running" signal, not thread liveness

    def start(self) -> None:
        with self._lock:
            if self._running_intent and self._thread and self._thread.is_alive():
                logger.debug("Task '%s' already running", self.name)
                return

            # If intent was true but thread died, we are restarting cleanly.
            self._running_intent = True
            self._stop_event.clear()

            self._thread = start_monitored_thread(
                owner="background_task_manager",
                name=f"bg-{self.name}",
                target=self._loop,
                daemon=True,
                kind="background_task",
                metadata={"task_name": self.name},
            )

        logger.info(
            "Background task '%s' started (interval=%ss, run_immediately=%s, initial_delay=%ss)",
            self.name,
            self.interval_seconds,
            self.run_immediately,
            self.initial_delay_seconds,
        )

    def stop(self, join_timeout_seconds: float = 2.0) -> None:
        # Signal stop first (interrupts waiting)
        with self._lock:
            self._running_intent = False
            self._stop_event.set()
            thread = self._thread

        if thread is None:
            logger.info("Background task '%s' stopped (was not running)", self.name)
            return

        thread.join(timeout=join_timeout_seconds)

        # Only clear the thread handle if it actually exited
        with self._lock:
            if self._thread and not self._thread.is_alive():
                self._thread = None

        logger.info(
            "Background task '%s' stop requested (joined=%s)",
            self.name,
            (thread is not None and not thread.is_alive()),
        )

    def restart(self) -> None:
        self.stop()
        self.start()

    def is_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def should_be_running(self) -> bool:
        with self._lock:
            return self._running_intent

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            thread_alive = self._thread.is_alive() if self._thread else False
            last_run = self._last_run.isoformat() if self._last_run else None
            return {
                "name": self.name,
                "should_be_running": self._running_intent,
                "thread_alive": thread_alive,
                "interval_seconds": self.interval_seconds,
                "run_immediately": self.run_immediately,
                "initial_delay_seconds": self.initial_delay_seconds,
                "last_run": last_run,
                "run_count": self._run_count,
                "error_count": self._error_count,
            }

    def _loop(self) -> None:
        # Optional initial delay
        if self.initial_delay_seconds > 0:
            if self._stop_event.wait(timeout=self.initial_delay_seconds):
                return

        # Optional immediate run on start
        if self.run_immediately and not self._stop_event.is_set():
            self._execute()

        # Fixed-rate loop, using Event.wait for interruptible sleep
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=self.interval_seconds):
                break
            self._execute()

    def _execute(self) -> None:
        from app.services.scheduler_heartbeat import record_tick
        try:
            self.func()
            now = datetime.now(timezone.utc)
            with self._lock:
                self._last_run = now
                self._run_count += 1
            record_tick(f"bgtask:{self.name}", ok=True)
        except Exception as e:
            with self._lock:
                self._error_count += 1
            logger.error("Background task '%s' failed during execution", self.name)
            logger.debug("background task '' failed during execution %s exception details", self.name, exc_info=True)
            # No-silent-death (R3): every background task's tick liveness is visible on the health surface.
            record_tick(f"bgtask:{self.name}", ok=False, error=e)


class BackgroundTaskManager:
    """
    Manages all background tasks that run independently of the UI.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.tasks: Dict[str, BackgroundTask] = {}
        self._started = False

        self._register_default_tasks()

    def _register_default_tasks(self) -> None:
        # Data fetch routines (email, calendar, weather, etc.) are now managed
        # by RoutineManager via configs/routines.json — see fetch_* entries.

        # 6. Watchdog (1 minute)
        self.register_task(
            name="watchdog",
            func=self._run_watchdog,
            interval_seconds=60,
            run_immediately=False,
            initial_delay_seconds=60,
        )

        # 7. Ticket maintenance (5 minutes)
        # Expire tickets older than 2 hours
        self.register_task(
            name="ticket_maintenance",
            func=self._run_ticket_maintenance,
            interval_seconds=5 * 60,
            run_immediately=False,
            initial_delay_seconds=30,
        )

        # 8. Routine runner (60 seconds)
        # Executes scheduled routines (batch jobs / tool runs).
        self.register_task(
            name="routine_runner",
            func=self._run_routine_cycle,
            interval_seconds=60,
            run_immediately=False,
            initial_delay_seconds=45,
        )

    def register_task(
            self,
            name: str,
            func: TaskFn,
            interval_seconds: int,
            run_immediately: bool = False,
            initial_delay_seconds: int = 0,
    ) -> None:
        with self._lock:
            if name in self.tasks:
                logger.warning("Task '%s' already registered, replacing", name)
                self.tasks[name].stop()

            self.tasks[name] = BackgroundTask(
                name=name,
                func=func,
                interval_seconds=interval_seconds,
                run_immediately=run_immediately,
                initial_delay_seconds=initial_delay_seconds,
            )

    def start_all(self) -> None:
        with self._lock:
            if self._started:
                logger.warning("BackgroundTaskManager already started")
                return
            self._started = True

        logger.info("Starting background tasks (count=%s)", len(self.tasks))
        for task in self.tasks.values():
            task.start()

    def stop_all(self) -> None:
        logger.info("Stopping all background tasks...")
        for task in self.tasks.values():
            task.stop()

        # Wait for any in-flight routine-manager worker threads to finish.
        try:
            from app.assistant.routine_manager import get_routine_manager
            get_routine_manager().shutdown()
        except Exception:
            logger.debug("RoutineManager shutdown skipped (not initialized)")

        with self._lock:
            self._started = False

        logger.info("All background tasks stopped")

    def start_task(self, name: str) -> None:
        task = self.tasks.get(name)
        if not task:
            logger.error("Task '%s' not registered", name)
            return
        task.start()

    def stop_task(self, name: str) -> None:
        task = self.tasks.get(name)
        if not task:
            logger.error("Task '%s' not registered", name)
            return
        task.stop()

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            started = self._started
        return {
            "started": started,
            "task_count": len(self.tasks),
            "tasks": {name: task.get_status() for name, task in self.tasks.items()},
        }

    # =========================================================================
    # Watchdog & Housekeeping
    # =========================================================================

    def _run_watchdog(self) -> None:
        # Restart any task that is intended to be running but is not alive.
        dead = []
        with self._lock:
            snapshot = list(self.tasks.items())
        for name, task in snapshot:
            if name == "watchdog":
                continue

            if task.should_be_running() and not task.is_alive():
                dead.append(name)

        if not dead:
            return

        logger.error("Watchdog detected dead tasks: %s", dead)
        with self._lock:
            for name in dead:
                try:
                    task = self.tasks.get(name)
                    if task is None:
                        continue
                    logger.warning("Watchdog restarting task: %s", name)
                    task.start()
                except Exception:
                    logger.error("Watchdog failed to restart task: %s", name)
                    logger.debug("watchdog failed to restart task:  %s exception details", name, exc_info=True)

    def _run_routine_cycle(self) -> None:
        """
        Trigger the routine manager refresh loop.
        """
        try:
            from app.assistant.utils.subsystem_flags import is_subsystem_enabled
            if not is_subsystem_enabled("routine_manager"):
                return
            from app.assistant.routine_manager import get_routine_manager
            manager = get_routine_manager()
            manager.refresh()
            logger.debug("Routine cycle complete")
        except Exception as e:
            log_critical_error(
                message="Routine cycle failed",
                exception=e,
                context="BackgroundTaskManager._run_routine_cycle",
                include_traceback=True,
            )

    def _run_ticket_maintenance(self) -> None:
        """
        Expire tickets older than 2 hours.
        Also wakes up snoozed tickets whose snooze time has passed.
        """
        try:
            from app.assistant.ticket_manager import get_ticket_manager
            
            ticket_manager = get_ticket_manager()
            
            # Expire old tickets (2 hour max age)
            expired = ticket_manager.expire_old_tickets()
            if expired > 0:
                logger.info(f"Ticket maintenance: expired {expired} old tickets")
            
            # Handle snoozed tickets whose snooze time has passed
            # Policy: expire them so orchestrator can create fresh tickets with current context
            snoozed_ready = ticket_manager.get_snoozed_tickets_ready()
            for ticket in snoozed_ready:
                ticket_manager.mark_expired(
                    ticket.ticket_id,
                    reason=f"Snooze expired after {ticket.snooze_count} snooze(s) - orchestrator will re-evaluate"
                )
            if snoozed_ready:
                logger.info(f"Ticket maintenance: expired {len(snoozed_ready)} snoozed tickets")

        except Exception as e:
            log_critical_error(
                message="Ticket maintenance failed",
                exception=e,
                context="BackgroundTaskManager._run_ticket_maintenance",
                include_traceback=True,
            )


# Singleton instance
_background_task_manager: Optional[BackgroundTaskManager] = None


def get_background_task_manager() -> BackgroundTaskManager:
    global _background_task_manager
    if _background_task_manager is None:
        _background_task_manager = BackgroundTaskManager()
    return _background_task_manager


def start_background_tasks() -> BackgroundTaskManager:
    manager = get_background_task_manager()
    manager.start_all()
    return manager


def stop_background_tasks() -> None:
    global _background_task_manager
    if _background_task_manager:
        _background_task_manager.stop_all()
