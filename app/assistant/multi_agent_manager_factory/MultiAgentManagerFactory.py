import time

import copy

from app.assistant.ServiceLocator.service_locator import DI

from app.assistant.utils.dynamic_import import load_class_from_prefix
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class ManagerFactory:
    def __init__(self, registry, tool_registry, agent_registry):
        self.registry = registry
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry

    def create(self, manager_type: str, name: str = None):
        config = self.registry.get(manager_type)
        if not config:
            raise ValueError(f"No config found for manager type '{manager_type}'")

        class_name = config.get("class_name")
        if not class_name:
            raise ValueError(f"Missing 'class_name' in config for '{manager_type}'")

        manager_class = load_class_from_prefix("app.assistant.manager_classes", class_name)

        allowed_raw = config.get("tools", {}).get("allowed_tools")
        if isinstance(allowed_raw, str):
            allowed = {allowed_raw.strip()} if allowed_raw.strip() else set()
        elif isinstance(allowed_raw, list):
            allowed = {str(x).strip() for x in allowed_raw if isinstance(x, str) and str(x).strip()}
        else:
            allowed = set()
        excluded = set(config.get("tools", {}).get("except_tools", []))

        if not allowed:
            logger.warning(f"Manager '{manager_type}' has no allowed tools configured.")

        allow_all = "all" in allowed
        if allow_all:
            allowed = set(self.tool_registry.list_tools())

        filtered_tools = self.tool_registry.filter_tools(allowed - excluded)



        agent_registry_copy = copy.deepcopy(self.agent_registry)

        name = name or manager_type
        instance = manager_class(name, config, filtered_tools, agent_registry_copy)
        return instance


class MultiAgentManagerFactory:
    """
    Legacy-compatible factory and registry wrapper.
    Internally uses ManagerRegistry + ManagerFactory.
    """

    def __init__(self):
        self._manager_registry = DI.manager_registry
        self._manager_factory = ManagerFactory(
            registry=self._manager_registry,
            tool_registry=DI.tool_registry,
            agent_registry=DI.agent_registry
        )

    def create_manager(self, manager_type: str, name=None):
        if manager_type not in self._manager_registry.list_available():
            logger.error(f"❌ Manager type '{manager_type}' not found in registry")
            raise ValueError(f"❌ Manager type '{manager_type}' not found in registry")

        return self._manager_factory.create(manager_type, name)



def main():
    """
    Main entry point to demonstrate the creation of multiple MultiAgentManager instances.
    """
    import app.assistant.tests.test_setup
    # Step 1: Preload resources
    print("\nPreloading resources...")
    preload_start = time.time()
    DI.manager_registry.preload_all()
    preload_end = time.time()
    print(f"Preloading resources took {preload_end - preload_start:.2f} seconds.")

    # Step 2: Initialize objects
 # Step 3: Create managers
    print("\nCreating first manager...")
    multi_agent_manager_factory = DI.multi_agent_manager_factory
    manager1 = multi_agent_manager_factory.create_manager("emi_team_manager")

    print("\nCreating second manager...")
    manager2 = multi_agent_manager_factory.create_manager("emi_team_manager")

    # Step 4: Verify managers
    print("\nVerifying managers...")

    print("\nObject Instances and IDs:")
    print(f"Manager 1: {manager1.name}, ID: {id(manager1)}")
    print(f"Manager 2: {manager2.name}, ID: {id(manager2)}")

    print("\nAgent Registry IDs:")
    print(f"Manager 1 AgentRegistry ID: {id(manager1.agent_registry)}")
    print(f"Manager 2 AgentRegistry ID: {id(manager2.agent_registry)}")

    print("\nTool Registry IDs:")
    print(f"Manager 1 ToolRegistry ID: {id(manager1.tool_registry)}")
    print(f"Manager 2 ToolRegistry ID: {id(manager2.tool_registry)}")

    print("\nBlackboard IDs:")
    print(f"Manager 1 Blackboard ID: {id(manager1.blackboard)}")
    print(f"Manager 2 Blackboard ID: {id(manager2.blackboard)}")

    print("\nInitialization complete!")

    # Print agent instances inside each registry
    print(f"\nAgent Instances in Manager: {manager1.name}")

    #print(manager1.agent_registry.get_all_agents())
    print(f"\nAgent Instances in Manager: {manager2.name}")
    #print(manager2.agent_registry.get_all_agents())

    print("\nChecking agent identity between managers...")

    agents1 = manager1.agent_registry.get_all_agents()
    agents2 = manager2.agent_registry.get_all_agents()

    # Check for matching names
    common_names = set(agents1.keys()) & set(agents2.keys())
    for name in sorted(common_names):
        a1 = agents1[name]
        a2 = agents2[name]
        same = a1 is a2
        print(f"  Agent '{name}': same object? {same} (IDs: {id(a1)} vs {id(a2)})")



if __name__ == "__main__":
    main()


