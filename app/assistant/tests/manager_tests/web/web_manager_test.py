import time

import app.assistant.tests.test_setup  # noqa: F401 - side-effect import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def main(task: str, info: str | None = None) -> None:
    logger.info("initialize_system() is running...")
    print("initialize system...")
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    manager_registry = DI.manager_registry
    manager_registry.preload_all()
    elapsed_time = time.time() - preload_start
    logger.info("✅ Preloading completed in %.2f seconds.", elapsed_time)
    print(f"✅ Preloading completed in {elapsed_time:.2f} seconds.")

    logger.info("🔄 Creating web_manager...")
    manager = factory.create_manager("web_manager")

    request_message = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=task,
        information=info,
    )
    result = manager.request_handler(request_message)
    print(result)


if __name__ == "__main__":
    task = (
        "Find out a 5 mountain bike options where to buy and what is highest rated."
    )
    info = ""
    main(task, info)
