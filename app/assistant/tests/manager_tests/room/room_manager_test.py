import time

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main(manager_type="room_manager", room_id="1234", task="Say hello and briefly introduce yourself."):
    logger.info("initialize_system() is running...")
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    manager_registry = DI.manager_registry
    manager_registry.preload_all()
    preload_end = time.time()
    elapsed_time = preload_end - preload_start

    logger.info("Preloading completed in %.2f seconds.", elapsed_time)
    print(f"Preloading completed in {elapsed_time:.2f} seconds.")

    logger.info("Creating %s...", manager_type)
    manager = factory.create_manager(manager_type)

    request_message = Message(
        data_type="agent_activation",
        sender="User",
        receiver=None,
        content="",
        task=task,
        information="Room-manager smoke test with local room resource injection.",
        data={
            "room_id": room_id,
        },
    )

    result = manager.request_handler(request_message)
    print(result)


if __name__ == "__main__":
    main()
