"""
Test for kg_describe_node tool

This test verifies that the kg_describe_node tool works correctly
by describing a node from the knowledge graph.
"""

import sys
import os
import uuid

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Import test setup to initialize services

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolMessage

def test_kg_describe_node():
    """Test the kg_describe_node tool."""
    print("🧪 Testing kg_describe_node tool...")
    
    try:
        # Create the tool instance
        tool_class = DI.tool_registry.get_tool_class("kg_describe_node")
        tool = tool_class()
        print(f"✅ Tool created: {type(tool)}")
        
        # Create a test message
        test_node_id = "8bfbaca9-9c8b-4f24-9b0e-fc5ab396bb00"
        
        tool_message = ToolMessage(
            tool_name="kg_describe_node",
            tool_data={
                "tool_name": "kg_describe_node",
                "arguments": {
                    "node_id": test_node_id,
                }
            },
            request_id=str(uuid.uuid4()),
            sender="test_suite",
            receiver="kg_describe_node"
        )
        
        print(f"📝 Tool message created: {tool_message.tool_name}")
        print(f"📝 Node ID: {test_node_id}")
        
        # Execute the tool
        print("🚀 Executing kg_describe_node...")
        result = tool.execute(tool_message)
        
        print(f"✅ Tool execution completed")
        print(f"📊 Result type: {result.result_type}")
        print(f"📊 Content length: {len(result.content) if result.content else 0}")
        
        if result.content:
            print(f"📝 FULL CONTENT:")
            print("=" * 80)
            print(result.content)
            print("=" * 80)
        
        # Check if the result is valid (kg_describe_node returns "node_description" on success)
        if result.result_type == "node_description" or result.result_type == "Success":
            print("✅ kg_describe_node test PASSED")
            return True
        else:
            print(f"❌ kg_describe_node test FAILED: {result.content}")
            return False
            
    except Exception as e:
        print(f"❌ kg_describe_node test FAILED with exception: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_kg_describe_node_with_real_node():
    """Test the kg_describe_node tool with a real node from the database."""
    print("🧪 Testing kg_describe_node with real node...")
    
    try:
        # Get a real node from the database
        from app.models.base import get_session
        from app.assistant.kg.db.knowledge_graph_db import Node
        
        session = get_session()
        real_node = session.query(Node).first()
        
        if not real_node:
            print("⚠️ No nodes found in database, skipping real node test")
            return True
        
        print(f"📝 Using real node: {real_node.label} (ID: {real_node.id})")
        
        # Create the tool instance
        tool_class = DI.tool_registry.get_tool_class("kg_describe_node")
        tool = tool_class()
        
        # Create a test message with real node ID
        tool_message = ToolMessage(
            tool_name="kg_describe_node",
            tool_data={
                "tool_name": "kg_describe_node",
                "arguments": {
                    "node_id": str(real_node.id),
                    "max_edges": 3,
                    "include_raw": False
                }
            },
            sender="test_suite",
            receiver="kg_describe_node"
        )
        
        # Execute the tool
        print("🚀 Executing kg_describe_node with real node...")
        result = tool.execute(tool_message)
        
        print(f"✅ Tool execution completed")
        print(f"📊 Result type: {result.result_type}")
        
        if result.content:
            print(f"📝 FULL CONTENT:")
            print("=" * 80)
            print(result.content)
            print("=" * 80)
        
        if result.result_type == "node_description" or result.result_type == "Success":
            print("✅ kg_describe_node real node test PASSED")
            return True
        else:
            print(f"❌ kg_describe_node real node test FAILED: {result.content}")
            return False
            
    except Exception as e:
        print(f"❌ kg_describe_node real node test FAILED with exception: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    """Run the kg_describe_node tests."""
    print("🚀 Starting kg_describe_node tests...")
    print("=" * 60)
    
    # Test 1: Basic tool functionality
    test1_passed = test_kg_describe_node()
    print()
    
    # Test 2: With real node
    test2_passed = test_kg_describe_node_with_real_node()
    print()
    
    # Summary
    print("=" * 60)
    print("📊 Test Summary:")
    print(f"  Basic test: {'✅ PASSED' if test1_passed else '❌ FAILED'}")
    print(f"  Real node test: {'✅ PASSED' if test2_passed else '❌ FAILED'}")
    
    if test1_passed and test2_passed:
        print("🎉 All kg_describe_node tests PASSED!")
        sys.exit(0)
    else:
        print("❌ Some kg_describe_node tests FAILED!")
        sys.exit(1)
