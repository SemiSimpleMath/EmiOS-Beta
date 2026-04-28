import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
from app.assistant.utils.pydantic_classes import Message
from app.assistant.agent_registry.agent_factory import AgentFactory



def main():
    # Create the agent
    agent = DI.agent_factory.create_agent("knowledge_graph_add::fact_extractor")
    if not agent:
        print("❌ Agent creation failed!")
        return


    #fmt_input = {"text": "['Jukka wants the Emi open-source community to refactor Emi's codebase as a first priority'., 'Jukka wants the Emi open-source community to strengthen Emi's codebase as a first priority.', 'Jukka wants the Emi open-source community to prioritize security.', 'Jukka wants the Emi open-source community to prioritize databases.', 'Jukka wants the Emi open-source community to prioritize multi-threading.']"}
    fmt_input = {"text": "['Jukka says that Phil needs help managing his iOS apps.']"}
    msg = Message(agent_input=fmt_input)
    # Run the agent
    agent.action_handler(msg)



if __name__ == "__main__":
    main()