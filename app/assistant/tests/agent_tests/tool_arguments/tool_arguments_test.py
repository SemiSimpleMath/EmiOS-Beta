"""
Test for shared::tool_arguments agent
Tests the tool_arguments agent with kg_update_node tool
"""
import json

import app.assistant.tests.test_setup  # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message

def test_tool_arguments_agent():
    """Test the tool_arguments agent with kg_update_node"""
    print("🧪 Testing shared::tool_arguments agent with kg_update_node tool")
    a_i = """'{"node_id":"13582683-38c0-4e48-92d3-adb1e40f9233","updates":{"attributes":{"temporal_confidence":"low","temporal_notes":"No specific dates available; known only to be long-term."},"category":"environmental_exposure","start_date":null,"end_date":null}}'"""
    # Setup blackboard with required context for tool_arguments agent
    blackboard = Blackboard()
    
    # CRITICAL: selected_tool must be set for ToolArguments to work
    blackboard.update_state({
        "selected_tool": "kg_update_node",  # REQUIRED - ToolArguments reads this from blackboard
        "tool_description": "Update a knowledge graph node with new information including label, description, aliases, attributes, and temporal data",
        "tool_args": """node_id (str): ID of the node to update
label (str, optional): New label for the node
description (str, optional): New description for the node
aliases (list, optional): New aliases for the node
category (str, optional): New category for the node
attributes (dict, optional): New attributes for the node
start_date (str, optional): Start date in ISO format
end_date (str, optional): End date in ISO format
start_date_confidence (str, optional): Confidence level for start date ('inferred', 'actual', 'estimated')
end_date_confidence (str, optional): Confidence level for end date ('inferred', 'actual', 'estimated')
valid_during (str): Time period when the node is valid""",
        "action_input": json.dumps(a_i),
        "date_time": "2025-01-02T10:30:00Z"
    })


    # Create the tool_arguments agent
    agent = DI.agent_factory.create_agent("shared::tool_arguments", blackboard=blackboard)
    
    if not agent:
        print("❌ Agent creation failed!")
        return False
    
    print("✅ Agent created successfully")
    print(f"📋 Agent name: {agent.name}")
    print(f"📋 Agent class: {agent.__class__.__name__}")
    
    # Validate that selected_tool is set (CRITICAL for ToolArguments)
    selected_tool = blackboard.get_state_value('selected_tool')
    if not selected_tool:
        print("❌ CRITICAL ERROR: selected_tool is not set on blackboard!")
        print("   ToolArguments agent requires selected_tool to be set by a previous agent (like tool_selector)")
        return False
    
    print(f"✅ selected_tool is set: {selected_tool}")
    
    # Create a test message
    msg = Message(
        data_type="agent_activation",
        sender="test_sender",
        receiver="shared::tool_arguments",
        content="Generate arguments for kg_update_node tool",
        task="Update a knowledge graph node with new information"
    )
    
    print("\n🚀 Running tool_arguments agent...")
    print("📋 Blackboard state before execution:")
    print(f"  - selected_tool: {blackboard.get_state_value('selected_tool')}")
    print(f"  - action_input: {blackboard.get_state_value('action_input')}")
    
    # Run the agent
    try:
        result = agent.action_handler(msg)
        print("✅ Agent execution completed")
        
        # Check blackboard state after agent execution
        print("\n📊 Blackboard state after execution:")
        print(f"  - selected_tool: {blackboard.get_state_value('selected_tool')}")
        print(f"  - tool_arguments: {blackboard.get_state_value('tool_arguments')}")
        
        # Validate the output
        tool_arguments = blackboard.get_state_value('tool_arguments')
        if tool_arguments:
            print("✅ Tool arguments generated successfully!")
            print(f"📝 Generated arguments: {tool_arguments}")
            
            # The ToolArguments class stores the result directly as a dict
            if isinstance(tool_arguments, dict):
                print("✅ Arguments are in correct dict format")
                
                # Check if required fields are present based on kg_update_node schema
                if 'node_id' in tool_arguments:
                    print("✅ Required field 'node_id' is present")
                else:
                    print("❌ Required field 'node_id' is missing")
                    return False
                
                # Check for other expected fields based on the action_input
                expected_fields = ['node_id', 'category', 'attributes']
                found_fields = [field for field in expected_fields if field in tool_arguments]
                print(f"✅ Found expected fields: {found_fields}")
                
                # Check for specific values from the action_input
                if 'category' in tool_arguments and tool_arguments['category'] == 'environmental_exposure':
                    print("✅ Category field matches expected value")
                if 'attributes' in tool_arguments and 'temporal_confidence' in tool_arguments['attributes']:
                    print("✅ Attributes field contains temporal_confidence")
                
                return True
            else:
                print(f"❌ Arguments are not in dict format: {type(tool_arguments)}")
                return False
        else:
            print("❌ No tool arguments generated")
            return False
            
    except Exception as e:
        print(f"❌ Agent execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Main test function"""
    success = test_tool_arguments_agent()
    if success:
        print("\n🎉 Test passed!")
    else:
        print("\n💥 Test failed!")

if __name__ == "__main__":
    main()
