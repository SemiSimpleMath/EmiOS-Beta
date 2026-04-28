import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
from app.assistant.utils.pydantic_classes import Message
from app.assistant.agent_registry.agent_factory import AgentFactory


def main():

    # Create the agent
    agent = DI.agent_factory.create_agent("knowledge_graph_add::formatter")

    # Ensure the agent was created successfully
    if not agent:
        print("❌ Agent creation failed!")
        return
    input_text="Jukka's favorite movie is The Good, the Bad and the Ugly."
    fmt_input = {"text": input_text, "original_message": "Jukka's favorite movie is The Good, the Bad and the Ugly."}
    msg = Message(agent_input=fmt_input)
    # Run the agent
    agent.action_handler(msg)



if __name__ == "__main__":
    main()