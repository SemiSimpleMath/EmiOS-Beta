import json
import time

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def main(manager_type, task, info=None):
    logger.info("initialize_system() is running...")
    print("initialize system...")
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    DI.manager_registry.preload_all()
    elapsed_time = time.time() - preload_start
    logger.info(f"✅ Preloading completed in {elapsed_time:.2f} seconds.")
    print(f"✅ Preloading completed in {elapsed_time:.2f} seconds.")

    logger.info(f"\n🔄 Creating {manager_type}...")
    manager = factory.create_manager(manager_type)

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
    return result


if __name__ == "__main__":
    task = (
        "Plan a tool sequence for: capture monitor 2, describe it, capture monitor 3, "
        "describe it, After you have the descriptions from the monitors do you own analysis or best guess as what the"
        "user is doing, then write a snippet file to resources/dayflow_pipeline_outputs/resource_desktop_activity_recent.md."
    )

    info = json.dumps(
        {
            "plan_only": True,
            "emit_llm_trace": False,
            "max_steps": 1,
        }
    )

    main("multi_tool_manager", task, info)
