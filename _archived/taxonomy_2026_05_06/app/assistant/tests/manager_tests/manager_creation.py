
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

    task = """     {
      "category_ids": [
        707,
        739,
        716,
        717,
        731,
        710,
        724,
        697,
        752,
        753,
        725
      ],
      "labels": [
        "reference_agent",
        "board_game",
        "digital_entity",
        "template_variable",
        "messaging_service",
        "phone_number",
        "room",
        "video_game",
        "personal_assistant",
        "software_project",
        "shelf"
      ],
      "paths": [
        "entity > ai_agent > reference_agent",
        "entity > artifact > creative_work > game > board_game",
        "entity > artifact > digital_entity",
        "entity > artifact > digital_entity > template_variable",
        "entity > artifact > product > service > messaging_service",
        "entity > contact_point > phone_number",
        "entity > location > room",
        "entity > artifact > creative_work > video_game",
        "entity > ai_agent > assistant > personal_assistant",
        "entity > artifact > digital_entity > software_project",
        "entity > artifact > product > physical_product > furniture > shelf"
      ],
      "problem": "Missing or unhelpful descriptions on important categories reduce clarity and consistency.",
      "actions": [
        "update_description(707, \"An AI agent specialized in retrieving, synthesizing, and citing information from reference sources.\" )",
        "update_description(739, \"A tabletop game played on a board, typically using pieces or cards and governed by a set of rules.\" )",
        "update_description(716, \"A man-made digital artifact or construct (e.g., software, platforms, online communities, and other virtual entities).\" )",
        "update_description(717, \"A placeholder symbol used in templates or code, replaced with concrete values during rendering or execution.\" )",
        "update_description(731, \"A service that enables sending or receiving electronic messages (e.g., email, SMS, chat).\" )",
        "update_description(710, \"A numerical sequence assigned to a telephone line for making and receiving calls or messages.\" )",
        "update_description(724, \"An enclosed space within a building, typically separated by walls, floor, and ceiling.\" )",
        "update_description(697, \"An electronic game played on computers, consoles, or mobile devices.\" )",
        "update_description(752, \"An AI assistant tailored to support an individual user with tasks, scheduling, and information retrieval.\" )",
        "update_description(753, \"An organized effort to plan, develop, and maintain software, encompassing code, documentation, and processes.\" )",
        "update_description(725, \"A flat furniture surface used for storing or displaying items, often mounted on a wall or part of a shelving unit.\" )"
      ],
      "confidence": 0.9
    }
    """
    main('taxonomy_team_manager', task)
