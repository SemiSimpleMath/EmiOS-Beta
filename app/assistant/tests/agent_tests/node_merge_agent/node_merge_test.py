import json
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message

def main():
    agent = DI.agent_factory.create_agent("knowledge_graph_add::node_merger")
    if not agent:
        print("❌ Agent creation failed!")
        return

    # Yes example
    # # 1. Define the context dictionaries
    # new_node_data = {
    #     "label": "J. Smith",
    #     "type": "Person",
    #     "context_sentence": "J. Smith was named CEO of Innovate Inc.",
    #     "relationship": {
    #         "type": "is_ceo_of",
    #         "direction": "outgoing",
    #         "related_node_label": "Innovate Inc.",
    #         "related_node_type": "Company"
    #     }
    # }
    # existing_node_data = {
    #     "id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
    #     "label": "John Smith",
    #     "type": "person",
    #     "originating_sentence": "The founder, John Smith, started the company in 2021.",
    #     "edges_sample": [
    #         {
    #             "direction": "outgoing",
    #             "relationship_type": "founded",
    #             "related_node_label": "Innovate Inc.",
    #             "related_node_type": "Company",
    #             "sentence": "The founder, John Smith, started the company in 2021."
    #         }
    #     ]
    # }
    new_node_data = {
        "label": "dog",
        "type": "Animal",
        "context_sentence": "My family's dog, Sparky, loves to play fetch in the yard.",
        "relationship": {
            "type": "is_pet_of",
            "direction": "outgoing",
            "related_node_label": "Smith Family",
            "related_node_type": "Family"
        }
    }

    existing_node_data_1 = {
        "id": "f1e2d3c4-b5a6-7890-1234-567890abcdef",
        "label": "dog",
        "type": "Animal",
        "originating_sentence": "The school's therapy dog helps students relax before exams.",
        "edges_sample": [
            {
                "direction": "outgoing",
                "relationship_type": "works_at",
                "related_node_label": "Eastwood Elementary",
                "related_node_type": "School",
                "sentence": "The school's therapy dog helps students relax before exams."
            }
        ]
    }

    new_node_data = {
        "label": "Emi",
        "type": "AI Assistant",
        "context_sentence": "Emi is my personal AI assistant.",
        "relationship": {
            "type": "has_role",
            "direction": "outgoing",
            "related_node_label": "AI Assistant",
            "related_node_type": "Role"
        }
    }
    existing_node_data_2 = {
        "id": "c7a8b9d0-e1f2-3456-7890-abcdef123456",
        "label": "Emi",
        "type": "person",
        "originating_sentence": "Emi helpts me to organize my calendar and to-do lists.",
        "edges_sample": [
            {
                "direction": "outgoing",
                "relationship_type": "manages",
                "related_node_label": "Calendar",
                "related_node_type": "Tool",
                "sentence": "Emi keeps my calendar organized."
            },
            {
                "direction": "outgoing",
                "relationship_type": "manages",
                "related_node_label": "To-Do List",
                "related_node_type": "Tool",
                "sentence": "Emi also handles my to-do list."
            }
        ]
    }

    # new_node_data = {
    #     "label": "saxophone",
    #     "type": "Musical Instrument",
    #     "context_sentence": "Jukka owns a Yamaha-64 saxophone.",
    #     "relationship": {
    #         "type": "is_played_by",
    #         "direction": "incoming",
    #         "related_node_label": "Jukka",
    #         "related_node_type": "Person"
    #     }
    # }
    existing_node_data_3 = {
        "id": "e4d5f6a7-b8c9-0123-4567-89abcdef0123",
        "label": "saxophone",
        "type": "Musical Instrument",
        "originating_sentence": "Jukka owns a Selmer Mark IV saxophone.",
        "edges_sample": [
            {
                "direction": "incoming",
                "relationship_type": "is_owned_by",
                "related_node_label": "Jukka",
                "related_node_type": "Person",
                "sentence": "Jukka owns a saxophone."
            }
        ]
    }


    input_dict = {
        "new_node_data": json.dumps(new_node_data),
        "existing_node_data": json.dumps([existing_node_data_1, existing_node_data_2, existing_node_data_3])
    }

    print("--- Sending Input to Agent (Human-Readable) ---")
    # For our own logging, we can print the pretty version

    msg = Message(agent_input=input_dict)
    result = agent.action_handler(msg)

    print("\n--- Agent Output ---")
    if result and result.data:
        print(json.dumps(result.data, indent=2))
    else:
        print("❌ Agent did not return any data.")

if __name__ == "__main__":
    main()