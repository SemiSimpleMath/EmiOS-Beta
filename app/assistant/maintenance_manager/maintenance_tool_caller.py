from datetime import datetime, timezone, timedelta
import threading
from typing import Dict
import os

from app.assistant.lib.tools.get_email.tool_forms.tool_forms import get_email_args, get_email_arguments
from app.assistant.lib.tools.get_news.tool_forms.tool_forms import get_news_args, get_news_arguments
from app.assistant.lib.tools.get_calendar_events.tool_forms.tool_forms import get_calendar_events_args, get_calendar_events_arguments
from app.assistant.lib.tools.get_todo_tasks.tool_forms.tool_forms import get_todo_tasks_args, get_todo_tasks_arguments
from app.assistant.lib.tools.get_scheduler_events.tool_forms.tool_forms import get_scheduler_events_args, get_scheduler_events_arguments
from app.assistant.lib.tools.get_weather.tool_forms.tool_forms import get_weather_args, get_weather_arguments
from app.assistant.utils.pydantic_classes import ToolMessage

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.google_auth.account_ids import GMAIL_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth.oauth_account_store import get_google_oauth_account_store
from app.assistant.utils.logging_config import get_maintenance_logger
from app.assistant.utils.time_utils import get_local_time
from app.assistant.user_settings_manager.user_settings import can_run_feature
from app.assistant.runtime import MonitoredThreadPoolExecutor

logger = get_maintenance_logger(__name__)

DEFAULT_MIN_INTERVALS = {
    'get_email': 5,       # Check email frequently
    'get_todo_tasks': 31, # Check todos less frequently
    'get_news': 23,       # Check news less frequently
    'get_weather': 11,    # Prime
    'get_scheduler_events': 3,  # Check scheduler frequently
    'get_calendar_events': 27,   # Check calendar less frequently
}

class MaintenanceToolCaller:
    def __init__(self, min_intervals: Dict[str, int] = DEFAULT_MIN_INTERVALS):
        # Resolve tool_registry directly from DI
        self.tool_registry = DI.tool_registry
        self.min_intervals = min_intervals
        self.tool_timers: Dict[str, datetime] = {}
        self.tools_running: Dict[str, bool] = {}  # Track which tools are currently running
        self.tools_in_order = [
            'get_email',
            'get_news',
            'get_calendar_events',
            'get_todo_tasks',
            'get_scheduler_events',
            'get_weather',
        ]

        self.next_tool_index = 0
        self._lock = threading.RLock()
        workers = int(os.environ.get("EMI_MAINTENANCE_TOOL_WORKERS", "12"))
        self._executor = MonitoredThreadPoolExecutor(
            name="maintenance-tools",
            owner="maintenance_tool_caller",
            max_workers=max(1, workers),
            metadata={"component": "maintenance_tool_caller"},
        )

    def call_next_tool_if_ready(self):
        now = datetime.now(timezone.utc)
        local_time = get_local_time()
        timestamp_str = local_time.strftime("%m/%d/%Y %I:%M %p")
        
        total_tools = len(self.tools_in_order)
        attempts = 0

        while attempts < total_tools:
            tool = self.tools_in_order[self.next_tool_index]
            is_eligible, reason = self._is_tool_eligible(tool, now)
            
            if is_eligible:
                # Log that we're running the tool
                logger.info(f"▶️  Running {tool} at {timestamp_str} - Reason: {reason}")
                
                # Dynamically get the trigger method (e.g., trigger_get_email)
                trigger_method = getattr(self, f"trigger_{tool}", None)
                if trigger_method:
                    try:
                        # Mark tool as running before triggering
                        with self._lock:
                            self.tools_running[tool] = True
                        trigger_method()
                        self.tool_timers[tool] = now
                        logger.info(f"✅ Completed {tool} at {timestamp_str}")
                    except Exception as e:
                        # Always update timer even if trigger fails, to prevent rapid retries
                        self.tool_timers[tool] = now
                        with self._lock:
                            self.tools_running[tool] = False  # Clear running flag on error
                        logger.error(f"❌ Error running {tool} at {timestamp_str}: {e}", exc_info=True)
                    break
                else:
                    logger.error(f"No trigger method defined for tool: {tool}")
            else:
                # Log that we're NOT running the tool and why
                logger.info(f"⏸️  Not running {tool} at {timestamp_str} - Reason: {reason}")
            
            self.next_tool_index = (self.next_tool_index + 1) % total_tools
            attempts += 1

    def _is_tool_eligible(self, tool: str, current_time: datetime) -> tuple[bool, str]:
        """
        Check if a tool is eligible to run
        
        Returns:
            tuple: (is_eligible: bool, reason: str)
        """
        # Check if tool is already running
        with self._lock:
            if self.tools_running.get(tool, False):
                return False, "Already running"
        
        # Map tool names to feature names
        tool_feature_map = {
            'get_email': 'email',
            'get_calendar_events': 'calendar',
            'get_todo_tasks': 'tasks',
            'get_weather': 'weather',
            'get_news': 'news',
            'get_scheduler_events': 'scheduler'
        }
        
        # Check if feature is enabled and has required API keys
        feature_name = tool_feature_map.get(tool)
        if feature_name:
            if not can_run_feature(feature_name):
                return False, "Feature disabled or missing API key"
            
            # Check if quiet mode is active for this feature
            from app.assistant.user_settings_manager.user_settings import get_settings_manager
            settings_manager = get_settings_manager()
            if settings_manager.is_quiet_mode_active(feature_name):
                return False, "Quiet hours active"
        
        # If tool has never been called, it's immediately eligible (startup staggering)
        last_called = self.tool_timers.get(tool)
        if last_called is None:
            return True, "First run"
        
        # Standard interval-based scheduling for tools that have run at least once
        min_interval = self.min_intervals.get(tool, 10)
        elapsed = (current_time - last_called).total_seconds() / 60.0
        
        if elapsed >= min_interval:
            return True, f"Interval met ({elapsed:.1f} min >= {min_interval} min)"
        else:
            return False, f"Interval not met ({elapsed:.1f} min < {min_interval} min)"

    def _run_tool_async(self, tool_name: str, tool_func, *args, **kwargs):
        """Run tool asynchronously and clear running flag when done"""
        def wrapped_tool():
            try:
                tool_func(*args, **kwargs)
            except Exception as e:
                logger.error(f"❌ Error in async tool '{tool_name}': {e}", exc_info=True)
            finally:
                # Always clear running flag when tool completes (success or failure)
                with self._lock:
                    self.tools_running[tool_name] = False
                logger.debug(f"Tool '{tool_name}' completed, cleared running flag")

        self._executor.submit(wrapped_tool)

    def trigger_get_email(self):
        logger.info("Triggering get_email tool.")

        now_utc = datetime.now(timezone.utc)
        UNREAD_LOOKBACK = timedelta(hours=10)

        # Scan a fixed 10-hour lookback for unread inbox mail.
        # The is:unread filter prevents re-ingesting already-processed messages.
        start_date_str = (now_utc - UNREAD_LOOKBACK).replace(microsecond=0).isoformat()
        end_date_str = now_utc.replace(microsecond=0).isoformat()

        logger.info(
            f"📧 Email search window (UTC): start_date={start_date_str}, "
            f"end_date={end_date_str}"
        )

        account_ids = self._resolve_email_poll_account_ids()
        logger.info("📧 Polling Gmail for %d account(s): %s", len(account_ids), account_ids)

        def tool_call():
            tool_entry = self.tool_registry.get_tool("get_email")
            tool_class = tool_entry["tool_class"]
            for account_id in account_ids:
                args_dict = get_email_args(
                    start_date=start_date_str,
                    end_date=end_date_str,
                    unseen=True,
                    repo_update=True,
                    account_id=account_id,
                ).model_dump()

                email_args_model = get_email_arguments(
                    tool_name="get_email",
                    arguments=args_dict,
                )

                tool_message = ToolMessage(
                    tool_name="get_email",
                    tool_data=email_args_model.model_dump(),
                )

                tool_instance = tool_class()
                tool_instance.execute(tool_message)

        self._run_tool_async('get_email', tool_call)

    def _resolve_email_poll_account_ids(self) -> list[str]:
        configured_gmail_account = str(GMAIL_GOOGLE_ACCOUNT_ID or "").strip()
        if not configured_gmail_account:
            raise ValueError("GMAIL_GOOGLE_ACCOUNT_ID resolved to empty value.")

        store = get_google_oauth_account_store()
        rows = store.list_accounts()
        active_gmail_accounts: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not bool(row.get("is_active", False)):
                continue
            scopes = row.get("granted_scopes")
            scopes_list = scopes if isinstance(scopes, list) else []
            has_gmail_scope = any(
                isinstance(scope, str) and scope.startswith("https://www.googleapis.com/auth/gmail.")
                for scope in scopes_list
            )
            if not has_gmail_scope:
                continue
            account_id = str(row.get("account_id") or "").strip()
            if account_id:
                active_gmail_accounts.append(account_id)

        ordered_accounts = [configured_gmail_account, *active_gmail_accounts]
        deduped: list[str] = []
        seen: set[str] = set()
        for account_id in ordered_accounts:
            if account_id in seen:
                continue
            seen.add(account_id)
            deduped.append(account_id)

        return deduped


    def trigger_get_news(self):
        logger.info("Triggering get_news tool.")
        args_dict = get_news_args(feed_urls=[]).model_dump()
        news_args_model = get_news_arguments(tool_name="get_news", arguments=args_dict)
        tool_message = ToolMessage(
            tool_name="get_news",
            tool_data=news_args_model.model_dump()
        )

        def tool_call():
            tool_entry = self.tool_registry.get_tool('get_news')
            tool_class = tool_entry["tool_class"]
            tool_instance = tool_class()
            tool_instance.execute(tool_message)

        self._run_tool_async('get_news', tool_call)

    def trigger_get_calendar_events(self):
        """
        Fetch calendar events with a stable 7 day window anchored to midnight.
        Window: Today 00:00:00 to (Today + 7 days) 23:59:59.
        """
        local_now = get_local_time()
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

        start_date = local_midnight.isoformat()
        end_date = (local_midnight + timedelta(days=7, seconds=-1)).isoformat()

        args_dict = get_calendar_events_args(
            start_date=start_date,
            end_date=end_date,
            calendar_names=[
                "primary",
                "birthday",
                "holidays in united states",
                "family",
                "work",
                "food",
            ],
            single_events=True,
            repo_update=True,  # only scheduler sets this
        ).model_dump()

        calendar_args_model = get_calendar_events_arguments(
            tool_name="get_calendar_events",
            arguments=args_dict,
        )

        tool_message = ToolMessage(
            tool_name="get_calendar_events",
            tool_data=calendar_args_model.model_dump(),
        )

        def tool_call():
            tool_entry = self.tool_registry.get_tool("get_calendar_events")
            tool_class = tool_entry["tool_class"]
            tool_instance = tool_class()
            tool_instance.execute(tool_message)

        self._run_tool_async('get_calendar_events', tool_call)




    def trigger_get_todo_tasks(self):
        logger.info("Triggering get_todo_tasks tool.")
        args_dict = get_todo_tasks_args(
            start_date=None,
            end_date=None,
            include_completed=False,
            repo_update=True,  # Maintenance tool writes to repo
        ).model_dump()
        todo_args_model = get_todo_tasks_arguments(tool_name='get_todo_tasks', arguments=args_dict)
        tool_message = ToolMessage(
            tool_name='get_todo_tasks',
            tool_data=todo_args_model.model_dump()
        )


        def tool_call():
            tool_entry = self.tool_registry.get_tool('get_todo_tasks')
            tool_class = tool_entry["tool_class"]
            tool_instance = tool_class()
            tool_instance.execute(tool_message)

        self._run_tool_async('get_todo_tasks', tool_call)

    def trigger_get_scheduler_events(self):
        logger.info("Triggering get_scheduler_events tool.")

        args_model = get_scheduler_events_args(start_date="", end_date="")
        scheduler_args_model = get_scheduler_events_arguments(
            tool_name='get_scheduler_events',
            arguments=args_model
        )
        tool_message = ToolMessage(
            tool_name='get_scheduler_events',
            tool_data=scheduler_args_model.model_dump()
        )


        def tool_call():
            tool_entry = self.tool_registry.get_tool('get_scheduler_events')
            tool_class = tool_entry["tool_class"]
            tool_instance = tool_class()
            tool_instance.execute(tool_message)

        self._run_tool_async('get_scheduler_events', tool_call)

    def trigger_get_weather(self):
        logger.info("Triggering get_weather tool.")
        args_dict = get_weather_args(city="Irvine", forecast_type="current").model_dump()
        weather_args_model = get_weather_arguments(tool_name="get_weather", arguments=args_dict)
        tool_message = ToolMessage(
            tool_name="get_weather",
            tool_data=weather_args_model.model_dump()
        )

        def tool_call():
            tool_entry = self.tool_registry.get_tool('get_weather')
            tool_class = tool_entry["tool_class"]
            tool_instance = tool_class()
            tool_instance.execute(tool_message)

        self._run_tool_async('get_weather', tool_call)
