import time
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main(manager_type: str, task_file: str, info: str | None = None):
    logger.info("initialize_system() is running...")
    print("initialize system...")
    preload_start = time.time()
    manager_registry = DI.manager_registry
    manager_registry.preload_all()
    elapsed_time = time.time() - preload_start
    logger.info("✅ Preloading completed in %.2f seconds.", elapsed_time)
    print(f"✅ Preloading completed in {elapsed_time:.2f} seconds.")

    logger.info("🔄 Creating %s...", manager_type)
    manager = DI.multi_agent_manager_factory.create_manager(manager_type)
    request_message = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task="",
        information=info or "",
        data={"task_file": task_file},
    )
    result = manager.request_handler(request_message)
    print(result)
    return result


if __name__ == "__main__":
    main("emi_team_manager", "tasks/timesheet/task_spec.md")
