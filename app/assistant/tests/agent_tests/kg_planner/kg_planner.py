from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


def main():
    # Create the agent
    agent = DI.agent_factory.create_agent("kg_team::planner")
    if not agent:
        print("❌ Agent creation failed!")
        return

    # Example triple and context
    test_data = {
        "question": "Who is Katy married to?"
    }
    msg = Message(agent_input=test_data)
    # Run the agent
    result = agent.action_handler(msg)
    print("\n--- kg_team::planner Agent Output ---")
    print(result)


if __name__ == "__main__":
    main()