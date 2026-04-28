import json
import time

import app.assistant.tests.test_setup  # noqa: F401  (side-effect: initialize DI)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def main(manager_type: str, payload: dict):
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
        # MultiToolAgent (planner) will parse this JSON payload from task/information.
        task=json.dumps(payload),
        information="multi_tool_manager_test",
    )
    result = manager.request_handler(request_message)
    print(result)
    return result


if __name__ == "__main__":
    # Safe default payload: only run a local discovery tool.
    payload = {
        "sequence": [
            {
                "id": "find_describe_tool",
                "tool_name": "find_tool",
                "arguments": {"query": "describe image", "limit": 5},
            }
        ],
        "allowed_tools": ["find_tool"],
        "auto_discover_tools": True,
        "max_steps": 8,
        "max_parallel": 2,
    }
    main("multi_tool_test_manager", payload)
