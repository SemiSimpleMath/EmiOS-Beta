from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class EventExecutor:
    """
    Executes a fired event by publishing it to 'scheduler_fired:{event_type}'.
    """

    def __init__(self):
        self.logger = logger

    def execute(self, event):
        event_type = getattr(event, "event_type", None)
        if not event_type:
            self.logger.error("Triggered event missing 'event_type'. Cannot route.")
            return

        topic = f"{event_type}"
        self.logger.info(f"Event fired. Publishing to: scheduler_event_{topic}")
        self.logger.debug("Event to publish: %s", event)

        event_msg = Message(
            sender="scheduler",
            receiver="emi_reminder_handler",
            content="reminder event",
            data = event.model_dump(),
            event_topic="scheduler_event_" + topic
        )

        DI.event_hub.publish(event_msg)
