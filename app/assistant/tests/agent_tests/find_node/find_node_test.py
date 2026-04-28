import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message

def main():
    # Create the find_node agent
    agent = DI.agent_factory.create_agent("kg_team::find_node")

    fmt_input = {"task": "Please find up to 12 nodes of type State or Event that are missing temporal info (no start_date and no end_date). Return ids, labels, types, and any brief context to help the user recognize them."}
    msg = Message(agent_input=fmt_input)

    # Create message for the agent
    msg = Message(agent_input=fmt_input)
    # Run the agent
    result = agent.action_handler(msg)
    
    print(f"=== AGENT RESULT ===")
    print(f"Result: {result}")
    print(f"Result type: {type(result)}")
    
    if result and hasattr(result, 'data'):
        print(f"Result data: {result.data}")
    
    print(f"=== TEST COMPLETE ===")

if __name__ == "__main__":
    main()
