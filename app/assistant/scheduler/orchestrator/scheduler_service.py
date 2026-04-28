
from app.assistant.scheduler.scheduler.executor import EventExecutor
from app.assistant.scheduler.scheduler.timing_engine import TimingEngine
from app.assistant.scheduler.scheduler.event_scheduler import EventScheduler
from app.assistant.scheduler.storage.event_storage import EventStorage

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class SchedulerService:
    """
    High-level scheduler orchestrator. Initializes and wires up
    the event storage, timing engine, executor, and scheduler interface.
    """

    def __init__(self, app):
        self.app = app

        # Core components
        self.event_storage = EventStorage()              # Singleton
        self.event_executor = EventExecutor()            # Stateless
        self.timing_engine = TimingEngine(
            event_storage=self.event_storage,
            event_executor=self.event_executor,
            app=self.app
        )
        self.event_scheduler = EventScheduler(
            event_storage=self.event_storage,
            timing_engine=self.timing_engine,
            app=self.app
        )

    def start(self):
        logger.info("Starting SchedulerService...")
        self.timing_engine.start()

    def stop(self):
        logger.info("Stopping SchedulerService...")
        self.timing_engine.shutdown()

    def get_event_scheduler(self):
        return self.event_scheduler
