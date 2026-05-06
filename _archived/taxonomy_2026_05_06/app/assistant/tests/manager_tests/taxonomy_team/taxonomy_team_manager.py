import time
import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

def main(manager_type, task, info=None):
    logger.info("initialize_system() is running...")
    print("initialize system...")
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
    # Example: Real issue from taxonomy_integrity_validator (formatted by taxonomy_integrity_pipeline)
    # This is what the pipeline outputs after running:
    #   python app/assistant/kg_core/taxonomy/taxonomy_integrity_pipeline.py
    
    task = "Duplicate machine/robot subtrees exist under both 'artifact' and 'product > physical_product'. This splits identical concepts across two branches..."
    
    info = """
PROBLEM:
Duplicate machine/robot subtrees exist under both 'artifact' and 'product > physical_product'. This splits identical concepts across two branches and duplicates 'robot' and 'competition_robot'.

AFFECTED CATEGORIES:

  [CATEGORY] 'machine' (DUPLICATE - 2 instances):
    - ID: 746
      Path: entity > artifact > machine
      Parent: artifact
      Description: (no description)
    - ID: 492
      Path: entity > artifact > product > physical_product > machine
      Parent: physical_product
      Description: (no description)

  [CATEGORY] 'robot' (DUPLICATE - 2 instances):
    - ID: 747
      Path: entity > artifact > machine > robot
      Parent: machine
      Description: (no description)
    - ID: 493
      Path: entity > artifact > product > physical_product > machine > robot
      Parent: machine
      Description: (no description)

  [CATEGORY] 'competition_robot' (DUPLICATE - 2 instances):
    - ID: 748
      Path: entity > artifact > machine > robot > competition_robot
      Parent: robot
      Description: (no description)
    - ID: 494
      Path: entity > artifact > product > physical_product > machine > robot > competition_robot
      Parent: robot
      Description: (no description)

  [CATEGORY] 'combat_robot' (ID: 799)
    Path: entity > artifact > machine > robot > competition_robot > combat_robot
    Parent: competition_robot
    Description: (no description)

ACTIONS TO TAKE (IN ORDER):
  1. merge_categories(746, 492)
     => Merge 'machine' (ID 746) at 'entity > artifact > machine' INTO 'machine' (ID 492) at 'entity > artifact > product > physical_product > machine'
  2. merge_categories(747, 493)
     => Merge 'robot' (ID 747) at 'entity > artifact > machine > robot' INTO 'robot' (ID 493) at 'entity > artifact > product > physical_product > machine > robot'
  3. merge_categories(748, 494)
     => Merge 'competition_robot' (ID 748) at 'entity > artifact > machine > robot > competition_robot' INTO 'competition_robot' (ID 494) at 'entity > artifact > product > physical_product > machine > robot > competition_robot'

CONFIDENCE: 93.0%

NOTE: All category IDs, paths, and parent relationships are provided above.
The team manager should verify each action before execution.
"""
    
    main('taxonomy_team_manager', task, info)

