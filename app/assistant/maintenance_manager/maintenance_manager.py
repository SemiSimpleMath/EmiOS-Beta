from datetime import datetime, timedelta, timezone
import threading
from app.assistant.maintenance_manager.daily_summary_scheduler import DailySummaryScheduler
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_maintenance_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.user_settings_manager.user_settings import can_run_feature
from app.assistant.runtime import start_monitored_thread

logger = get_maintenance_logger(__name__)


class MaintenanceManager:
    def __init__(self, name: str = "maintenance_manager"):

        self.name = name
        self.event_hub = DI.event_hub

        # Dictionaries for rate limiting publish events
        self.last_publish_times = {}
        self.publish_intervals = {}

        # Initialize daily summary scheduler for idle mode processing
        self.daily_summary_scheduler = DailySummaryScheduler()


        # Thread safety for maintenance operations
        self.processing_lock = threading.RLock()

    def should_publish(self, event_name: str) -> bool:
        now = datetime.now(timezone.utc)
        last_time = self.last_publish_times.get(event_name, datetime.min.replace(tzinfo=timezone.utc))
        interval = self.publish_intervals.get(event_name, timedelta(seconds=30))
        if now - last_time >= interval:
            self.last_publish_times[event_name] = now
            return True
        return False

    def should_run_daily_summary(self) -> bool:
        """
        Check if daily summary should run (once per day, respects quiet_mode settings).
        
        Returns:
            bool: True if daily summary should run, False otherwise
        """
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        today = now.date()
        today_str = today.strftime("%Y-%m-%d")

        # Check if we've already run today (in-memory check)
        last_run_date = getattr(self, 'last_daily_summary_date', None)
        if last_run_date == today:
            logger.debug(f"⏸️ Daily summary skipped - already ran today")
            return False

        # Check if daily summary file already exists for today
        from app.assistant.maintenance_manager.daily_summary_storage import DailySummaryStorage
        storage = DailySummaryStorage()

        if storage.get_daily_summary(today_str) is not None:
            # File already exists, mark as run for today
            self.last_daily_summary_date = today
            logger.debug(f"⏸️ Daily summary skipped - file already exists for {today_str}")
            return False

        # Should run - mark the date
        logger.info(f"✅ Daily summary eligible to run for {today_str}")
        self.last_daily_summary_date = today
        return True

    def _is_tool_quiet_hours(self) -> bool:
        """
        Check if we're in quiet hours for tool calls using universal_hours from quiet_mode config.
        Returns True if tools should be disabled.
        """
        from app.assistant.user_settings_manager.user_settings import get_settings_manager
        settings_manager = get_settings_manager()
        
        # Check if global quiet mode is enabled
        if not settings_manager.get("quiet_mode.enabled", False):
            return False
        
        # Use universal_hours from quiet_mode config
        universal_hours = settings_manager.get("quiet_mode.universal_hours", {})
        start_str = universal_hours.get("start", "23:00")
        end_str = universal_hours.get("end", "07:00")
        
        try:
            from zoneinfo import ZoneInfo
            now_local = datetime.now(ZoneInfo("America/Los_Angeles"))
            current_time = now_local.time()
            
            start_time = datetime.strptime(start_str, "%H:%M").time()
            end_time = datetime.strptime(end_str, "%H:%M").time()
            
            # Handle overnight ranges (e.g., 23:00 to 07:00)
            if start_time > end_time:
                # Overnight: quiet if current >= start OR current < end
                return current_time >= start_time or current_time < end_time
            else:
                # Same day: quiet if current >= start AND current < end
                return start_time <= current_time < end_time
        except Exception as e:
            logger.warning(f"Error parsing quiet hours: {e}, using default behavior")
            return False

    def _is_feature_in_quiet_hours(self, feature: str) -> bool:
        """
        Check if a feature is currently in quiet hours based on user settings.
        
        Args:
            feature: Feature name (e.g., 'email', 'calendar', 'tasks')
            
        Returns:
            True if the feature should be disabled due to quiet hours.
        """
        from app.assistant.user_settings_manager.user_settings import get_settings_manager
        settings_manager = get_settings_manager()
        return settings_manager.is_quiet_mode_active(feature)

    # =========================================================================
    # Async Wrappers for Long-Running Tasks
    # These run in background threads to avoid blocking the EventHub
    # =========================================================================

    def _run_location_tracking_async(self):
        """Run location tracking in background thread."""
        self.location_tracking_running = True
        start_monitored_thread(
            owner="maintenance_manager",
            name="maintenance-location-tracking",
            target=self._location_tracking_worker,
            daemon=True,
            kind="background_worker",
            metadata={"component": "maintenance_manager", "worker": "location_tracking"},
        )
        logger.info("📍 Started location tracking in background thread")

    def _location_tracking_worker(self):
        """Background worker for location tracking."""
        try:
            logger.info("📍 Location tracking background worker started")
            self.run_location_tracking()
        except Exception as e:
            logger.error(f"❌ Error in location tracking worker: {e}", exc_info=True)
        finally:
            self.location_tracking_running = False
            logger.info("📍 Location tracking background worker finished")

    def setup_daily_summary_schedule(self):
        """
        Set up the daily summary to run at 7am every day.
        """
        try:
            # Get the scheduler service
            scheduler_service = DI.scheduler
            if not scheduler_service:
                logger.warning("Scheduler service not available. Daily summary scheduling skipped.")
                return

            event_scheduler = scheduler_service.get_event_scheduler()

            # Create daily summary event (7am every day)
            from app.assistant.scheduler.pydantic_types.base_event_data import BaseEventData

            # Calculate next 7am
            now = datetime.now(timezone.utc)
            next_7am = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if now.hour >= 7:
                next_7am += timedelta(days=1)

            daily_summary_event = BaseEventData(
                event_id="daily_summary_7am",
                event_type="interval",
                interval=24 * 60 * 60,  # 24 hours in seconds
                start_date=next_7am.isoformat(),
                end_date=None,  # Run indefinitely
                event_payload={
                    "task": "daily_summary",
                    "description": "Generate daily summary at 7am"
                }
            )

            # Create the event
            result = event_scheduler.create_event(daily_summary_event)
            logger.info(f"Daily summary schedule setup: {result}")

        except Exception as e:
            logger.error(f"Failed to setup daily summary schedule: {e}")

    def run_daily_summary(self):
        """
        Execute the daily summary generation in a background thread.
        Called by idle mode handler between 6am-5pm, once per day.
        
        Runs in a separate thread to avoid blocking the EventHub worker,
        which would delay socket emissions and other events.
        """
        # Check if already running
        if getattr(self, 'daily_summary_running', False):
            logger.info("⏸️ Daily summary already running in background - skipping")
            return {"success": False, "error": "Already running"}
        
        # Mark as running and spawn thread
        self.daily_summary_running = True
        start_monitored_thread(
            owner="maintenance_manager",
            name="maintenance-daily-summary",
            target=self._daily_summary_worker,
            daemon=True,
            kind="background_worker",
            metadata={"component": "maintenance_manager", "worker": "daily_summary"},
        )
        logger.info("📋 Started daily summary in background thread")
        return {"success": True, "message": "Started in background"}
    
    def _daily_summary_worker(self):
        """Background worker for daily summary processing."""
        try:
            logger.info("🕖 Daily summary background worker started")
            result = self.daily_summary_scheduler.run_daily_summary()

            if result.get("success"):
                logger.info("✅ Daily summary completed successfully")
            else:
                logger.error(f"❌ Daily summary failed: {result.get('error')}")

        except Exception as e:
            logger.error(f"❌ Error in daily summary execution: {e}")
        finally:
            self.daily_summary_running = False
            logger.info("📋 Daily summary background worker finished")

    def run_location_tracking(self):
        """
        Build/refresh the location timeline based on calendar events.
        
        This runs periodically to predict where the user will be throughout
        the day based on calendar events with locations. Other agents can
        query: "Where will user be in 2 hours?"
        """
        logger.info("run_location_tracking() starting...")
        try:
            from app.assistant.location_manager.location_manager import get_location_manager
            
            location_manager = get_location_manager()
            
            # Rebuild timeline from calendar and infer gaps
            current = location_manager.refresh()
            
            # Log summary
            summary = location_manager.get_location_summary(hours_ahead=12)
            logger.info(f"Location timeline refreshed:\n{summary}")
            
        except Exception as e:
            logger.error(f"Error in location tracking: {e}", exc_info=True)

    def run_kg_explorer(self):
        """
        Run the Knowledge Graph Explorer to analyze the KG and generate insights.
        """
        try:
            logger.info("🔍 Starting KG Explorer analysis...")

            # Create and run the KG Explorer manager
            kg_explorer_manager = DI.multi_agent_manager_factory.create_manager("kg_explorer_manager")

            # Set up exploration parameters
            exploration_input = {
                "exploration_scope": "full",
                "focus_areas": ["missing_dates", "orphaned_nodes", "data_quality", "weak_connections"]
            }

            # Run the exploration
            result = DI.manager_invoker.invoke(
                kg_explorer_manager,
                Message(
                    task=f"Explore and analyze the knowledge graph with focus on: {exploration_input['focus_areas']}",
                    scope_context=build_system_scope_context(
                        owner_id="system_maintenance",
                        actor_id="maintenance_manager",
                        surface="maintenance",
                        scope_id=f"scope::maintenance::kg_explorer::{datetime.now(timezone.utc).isoformat()}",
                    ),
                ),
            )

            if result:
                logger.info(f"✅ KG Explorer completed: {result.content}")

                # Save exploration results (optional - could save to a file or database)
                self._save_exploration_results(result)

                return {
                    'exploration_result': result.content,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }
            else:
                logger.warning("⚠️ KG Explorer returned no results")
                return {
                    'status': 'no_results',
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }

        except Exception as e:
            logger.error(f"❌ Error in KG Explorer: {e}")
            return {
                'error': str(e),
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

    def _save_exploration_results(self, result):
        """
        Save KG exploration results to a file for review.
        """
        try:
            from app.assistant.maintenance_manager.daily_summary_storage import DailySummaryStorage

            # Create a timestamped filename
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"kg_exploration_{timestamp}.json"

            # Save to the daily_summaries directory
            import os
            summaries_dir = os.path.join(os.path.dirname(__file__), "..", "..", "daily_summaries")
            os.makedirs(summaries_dir, exist_ok=True)

            filepath = os.path.join(summaries_dir, filename)

            exploration_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "analysis": result.content,
                "type": "kg_exploration"
            }

            with open(filepath, 'w', encoding='utf-8') as f:
                import json
                json.dump(exploration_data, f, indent=2, ensure_ascii=False)

            logger.info(f"📁 KG exploration results saved to: {filepath}")

        except Exception as e:
            logger.error(f"❌ Error saving exploration results: {e}")

