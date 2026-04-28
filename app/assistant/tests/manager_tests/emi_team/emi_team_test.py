import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../..")))

import time
import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message, ScopeContext
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
        receiver="Delegator",
        content=task,
        task=task,
        information=info,
        scope_context=ScopeContext(
            scope_id="scope::test::emi_team_smoke",
            owner_id="jukka",
            actor_id="emi_team_smoke_test",
            surface="test",
        ),
        data={"visible_tools": ["ask_kg", "ask_user"]},
    )
    result = manager.request_handler(request_message)

    print(result)


if __name__ == "__main__":
    #task = "Find out the distance between 1 Agate Irvine CA and Manhattan NY"
    #task = "Find out btc price."
    task = "What school did Jukka go to?"
    main('emi_team_manager', task)
