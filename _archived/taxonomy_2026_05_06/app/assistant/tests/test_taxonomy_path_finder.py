"""
Test script for taxonomy_path_finder tool.
"""

import app.assistant.tests.test_setup # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolMessage


def test_taxonomy_path_finder():
    """Test the taxonomy path finder tool."""
    
    print("🧪 Testing Taxonomy Path Finder Tool")
    print("=" * 50)
    
    # Create tool instance using the tool registry
    tool_class = DI.tool_registry.get_tool_class("taxonomy_path_finder")
    tool = tool_class()
    
    # Test case 1: Find robot-related paths in entity taxonomy
    print("\n📋 Test 1: Finding robot-related paths in entity taxonomy")
    tool_message1 = ToolMessage(
        tool_name="taxonomy_path_finder",
        tool_data={
            "arguments": {
                "top_level_taxonomy": "state",
                "description": "Anything related to a degree from a university",
                "keywords": ["educational", "degree", "graduate"]
            }
        }
    )
    
    result1 = tool.execute(tool_message1)
    print(f"Result type: {result1.result_type}")
    print(f"Content: {result1.content}")
    
    # Test case 2: Find medical-related paths in entity taxonomy
    print("\n📋 Test 2: Finding medical-related paths in entity taxonomy")
    tool_message2 = ToolMessage(
        tool_name="taxonomy_path_finder",
        tool_data={
            "arguments": {
                "top_level_taxonomy": "entity",
                "description": "paths for medical professionals and healthcare",
                "keywords": ["medical", "health", "doctor", "nurse", "healthcare", "treatment"]
            }
        }
    )
    
    result2 = tool.execute(tool_message2)
    print(f"Result type: {result2.result_type}")
    print(f"Content: {result2.content}")
    
    # Test case 3: Invalid top-level taxonomy
    print("\n📋 Test 3: Invalid top-level taxonomy")
    tool_message3 = ToolMessage(
        tool_name="taxonomy_path_finder",
        tool_data={
            "arguments": {
                "top_level_taxonomy": "nonexistent",
                "description": "any paths",
                "keywords": ["test"]
            }
        }
    )
    
    result3 = tool.execute(tool_message3)
    print(f"Result type: {result3.result_type}")
    print(f"Content: {result3.content}")
    
    print("\n✅ Test completed!")


if __name__ == "__main__":
    test_taxonomy_path_finder()
