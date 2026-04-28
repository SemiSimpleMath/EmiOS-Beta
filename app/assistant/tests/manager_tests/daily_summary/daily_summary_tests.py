import time
import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

def main(manager_type, task, info=None):
    logger.info("initialize_system() is running...")
    print("inintialize system...")
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    manager_registry = DI.manager_registry
    manager_registry.preload_all()

    preload_end = time.time()
    elapsed_time = preload_end - preload_start  # Compute time difference

    logger.info(f"✅ Preloading completed in {elapsed_time:.2f} seconds.")
    print(f"✅ Preloading completed in {elapsed_time:.2f} seconds.")

    # Step 2: Create the manager
    logger.info(f"\n🔄 Creating {manager_type}...")
    manager = factory.create_manager(manager_type)

    request_message = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",  # This kicks off the agent loop
        content="",
        task=task,
        information=info,
    )
    result = manager.request_handler(request_message)

    print(result)


if __name__ == "__main__":
    task = "Prepare daily summary"
    main('daily_summary_manager', task)
