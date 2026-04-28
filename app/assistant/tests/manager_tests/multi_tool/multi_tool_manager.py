import json
import os
import time

import app.assistant.tests.test_setup  # noqa: F401  (side-effect: initialize DI)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def main(manager_type, task, info=None):
    logger.info("initialize_system() is running...")
    print("initialize system...")
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    manager_registry = DI.manager_registry
    manager_registry.preload_all()
    preload_end = time.time()
    elapsed_time = preload_end - preload_start

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
    try:
        final_state_result = manager.blackboard.get_state_value("result")
        print("final_blackboard_result=", final_state_result)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    # Test-only policy overrides to let screenshot tools run during daytime.
    os.environ["EMI_SCREENSHOTS_ALLOW_WORK_HOURS"] = "1"
    os.environ["EMI_SCREENSHOTS_ALLOW_AFK"] = "1"
    os.environ["EMI_SCREENSHOTS_ALLOW_UNKNOWN_AFK"] = "1"
    os.environ["EMI_SCREENSHOTS_ALLOW_WHEN_TOGGLE_OFF"] = "1"

    task = (
        "Produce a short, high-signal activity log entry from screenshots of monitor 2 and 3. "
        "Use exact tool names from the tool contracts. Build a DAG with depends_on where appropriate. "
        "Capture monitor 2 and describe it, then capture monitor 3 and describe it. "
        "Then you have to pause to analyze the results, then once you have figured out your analysis of what you think the user is doing."
        "Then write the summary to resources/dayflow_pipeline_outputs/resource_desktop_activity_recent.md "
        "with timestamp, both monitor recaps, and a final call."
    )

    # task = (
    #     "Find out Leonardo Di Caprio's current girlfriends age."
    # )

    # MultiToolAgent will parse this JSON from `information`.
    information_payload = json.dumps(
        {
            "allowed_tools": [
                "set_screen_capture_enabled",
                "capture_monitor_screenshot",
                "vision_image_describe",
                "write_text_file",
                "find_tool",
                "web_manager",
            ],
            "auto_discover_tools": True,
            "max_parallel": 2,
            "initial_context": {"source": "manager_test_multi_tool"},
        }
    )

    main("multi_tool_manager", task, information_payload)
