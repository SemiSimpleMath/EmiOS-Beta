from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session


def main():
    # Create the agent
    agent = DI.agent_factory.create_agent("knowledge_graph_add::entity_resolver")

    text = """I got my phd at ucla.  I might go visit there today."""

    msg_text = {
        "text": text,
        "original_message_timestamp": "2024-01-15T10:30:00Z"
    }
    msg = Message(agent_input=msg_text)
    result = agent.action_handler(msg)
    print(f"=== ENTITY RESOLVER OUTPUT ===\n{result}\n")
    
    # Print just the resolved sentences
    if result and hasattr(result, 'data') and result.data:
        print("=== RESOLVED SENTENCES ===")
        for sentence in result.data.get('resolved_sentences', []):
            print(f"Original: {sentence.get('original_sentence', '')}")
            print(f"Resolved: {sentence.get('resolved_sentence', '')}")
            print(f"Reasoning: {sentence.get('reasoning', '')}")
            print("---")

    if result and hasattr(result, 'data') and result.data:
        print("=== RESOLVED SENTENCES ===")
        for sentence in result.data.get('resolved_sentences', []):
            print(f"Resolved: {sentence.get('resolved_sentence', '')}")
            print("---")


if __name__ == "__main__":
    import app.assistant.tests.test_setup # This is just run for the import
    main()
