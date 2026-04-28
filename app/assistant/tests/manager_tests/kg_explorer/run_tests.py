#!/usr/bin/env python3
"""
KG Explorer Tool Tests - Test the KG Explorer tool directly
"""

import sys
import os

# Add the project root to the path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, project_root)

from app.assistant.lib.tools.kg_explorer.kg_explorer import KGExplorerTool

def test_kg_explorer_tool():
    """Test the KG Explorer tool directly"""
    print("🧪 Testing KG Explorer Tool...")
    
    tool = KGExplorerTool()
    
    # Test overview
    print("\n📊 Testing KG Overview...")
    result = tool.execute({
        'query_type': 'overview',
        'parameters': {}
    })
    print(f"Overview Result: {result.content}")
    
    # Test missing dates
    print("\n📅 Testing Missing Dates Analysis...")
    result = tool.execute({
        'query_type': 'missing_dates',
        'parameters': {'limit': 10}
    })
    print(f"Missing Dates Result: {result.content}")
    
    # Test orphaned nodes
    print("\n🔗 Testing Orphaned Nodes Analysis...")
    result = tool.execute({
        'query_type': 'orphaned_nodes',
        'parameters': {'limit': 10}
    })
    print(f"Orphaned Nodes Result: {result.content}")
    
    # Test data quality
    print("\n🔍 Testing Data Quality Assessment...")
    result = tool.execute({
        'query_type': 'data_quality',
        'parameters': {}
    })
    print(f"Data Quality Result: {result.content}")

if __name__ == "__main__":
    print("🚀 Starting KG Explorer Tool Tests...")
    
    # Test the tool directly
    test_kg_explorer_tool()
    
    print("\n✅ KG Explorer tool tests completed!")
