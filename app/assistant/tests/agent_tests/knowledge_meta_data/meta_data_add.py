import json

import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message

def main():
    # Create the agent
    agent = DI.agent_factory.create_agent("knowledge_graph_add::meta_data_add")

    # Ensure the agent was created successfully
    if not agent:
        print("❌ Agent creation failed!")
        return

    # Example triple and context

    fact_extractor_data = {'nodes': [{'node_type': 'Entity', 'temp_id': 'entity_1', 'label': 'Jukka', 'aliases': [], 'category': 'Person', 'sentence': 'Jukka and Katy were married in 2003.'}, {'node_type': 'Entity', 'temp_id': 'entity_2', 'label': 'Katy', 'aliases': [], 'category': 'Person', 'sentence': 'Jukka and Katy were married in 2003.'}, {'node_type': 'Event', 'temp_id': 'event_1', 'label': 'Marriage of Jukka and Katy', 'aliases': [], 'category': None, 'sentence': 'Jukka and Katy were married in 2003.'}, {'node_type': 'EventNode', 'temp_id': 'event_1', 'label': 'Marriage event of Jukka and Katy in 2003', 'aliases': [], 'category': None, 'sentence': 'Jukka and Katy were married in 2003'}, ] }

    nodes = fact_extractor_data.get('nodes')


    sentence_window = "Jukka and Katy were married in 2003."

    node_data = {"nodes" : json.dumps(nodes),
                 "sentence_window": sentence_window,
                 "message_timestamp": "2025-09-18T10:30:00Z"  # Example message timestamp for testing
                 }

    msg = Message(agent_input=node_data)
    # Run the agent
    result = agent.action_handler(msg)
    print("\n--- Meta Data Agent Output ---")
    print(result)
    
    # Apply selective metadata enrichment based on node type rules
    print("\n--- Applying Selective Metadata Enrichment ---")
    
    if result and hasattr(result, 'data') and result.data:
        enriched_nodes = result.data.get("Nodes", [])
        print(f"Found {len(enriched_nodes)} enriched nodes from metadata agent")
        
        # Create a mapping of temp_id to enriched metadata
        enriched_metadata = {}
        for enriched_node in enriched_nodes:
            temp_id = enriched_node.get("temp_id")
            if temp_id:
                enriched_metadata[temp_id] = enriched_node
        
        # Apply selective metadata to original nodes
        for node in nodes:
            temp_id = node.get("temp_id")
            node_type = node.get("node_type", "").lower()
            
            print(f"\nProcessing node: {node['label']} ({node_type})")
            
            if temp_id in enriched_metadata:
                enriched = enriched_metadata[temp_id]
                print(f"  Found enriched metadata: {enriched}")
                
                # Apply metadata based on node type rules
                if node_type in ["eventnode", "statenode", "goalnode"]:
                    # These get start_date, end_date, valid_during
                    if enriched.get("start_date"):
                        node["start_date"] = enriched["start_date"]
                        print(f"  ✅ Added start_date: {enriched['start_date']}")
                    if enriched.get("end_date"):
                        node["end_date"] = enriched["end_date"]
                        print(f"  ✅ Added end_date: {enriched['end_date']}")
                    if enriched.get("valid_during"):
                        node["valid_during"] = enriched["valid_during"]
                        print(f"  ✅ Added valid_during: {enriched['valid_during']}")
                
                if node_type in ["statenode", "propertynode"]:
                    # These get semantic_label
                    if enriched.get("semantic_label"):
                        node["semantic_label"] = enriched["semantic_label"]
                        print(f"  ✅ Added semantic_label: {enriched['semantic_label']}")
                
                if node_type == "goalnode":
                    # GoalNodes get goal_status
                    if enriched.get("goal_status"):
                        node["goal_status"] = enriched["goal_status"]
                        print(f"  ✅ Added goal_status: {enriched['goal_status']}")
                
                # Entity nodes get nothing new (as per requirements)
                if node_type == "entity":
                    print(f"  ⏭️ Entity node - no metadata applied (as per requirements)")
            else:
                print(f"  ❌ No enriched metadata found for temp_id: {temp_id}")
    
    print("\n--- Final Enriched Nodes ---")
    for node in nodes:
        print(f"\nNode: {node['label']} ({node.get('node_type', 'unknown')})")
        print(f"  Original fields: {list(node.keys())}")
        
        # Show only the new metadata fields that were added
        metadata_fields = ["start_date", "end_date", "valid_during", "semantic_label", "goal_status"]
        added_fields = {k: v for k, v in node.items() if k in metadata_fields and v}
        if added_fields:
            print(f"  Added metadata: {added_fields}")
        else:
            print(f"  No metadata added")
    
    print("\n--- Complete Final Node List ---")
    print(json.dumps(nodes, indent=2))

if __name__ == "__main__":
    main()