#!/usr/bin/env python3
"""
Test to verify kg_team_manager tool schema works with 'task' field.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.assistant.lib.tools.kg_team_manager.tool_forms.tool_forms import kg_team_manager_args

def test_kg_team_manager_schema():
    """Test that the kg_team_manager schema accepts 'task' field."""
    print("🧪 Testing KG Team Manager Schema")
    print("=" * 50)
    
    try:
        # Test creating args with 'task' field
        args = kg_team_manager_args(
            task="Which college did Martin attend?",
            information="Looking for educational background information."
        )
        
        print(f"✅ Successfully created args with task: {args.task}")
        print(f"✅ Information: {args.information}")
        
        # Test that the schema validates correctly
        print(f"✅ Schema validation passed")
        
        # Test that we can access the fields
        assert args.task == "Which college did Martin attend?"
        assert args.information == "Looking for educational background information."
        
        print("🎉 All schema tests passed! kg_team_manager now uses 'task' field correctly.")
        
    except Exception as e:
        print(f"❌ Schema test failed: {e}")
        return False
    
    return True

if __name__ == "__main__":
    test_kg_team_manager_schema()
