#!/usr/bin/env python3
"""
Test to verify taxonomy_team_manager tool schema works with 'task' field.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.assistant.lib.tools.taxonomy_team_manager.tool_forms.tool_forms import taxonomy_team_manager_args

def test_taxonomy_team_manager_schema():
    """Test that the taxonomy_team_manager schema accepts 'task' field."""
    print("🧪 Testing Taxonomy Team Manager Schema")
    print("=" * 50)
    
    try:
        # Test creating args with 'task' field
        args = taxonomy_team_manager_args(
            task="Merge duplicate 'email_address' and 'email' categories",
            information="Found two categories that represent the same concept: 'email_address' (ID: 123) and 'email' (ID: 456). They should be merged."
        )
        
        print(f"✅ Successfully created args with task: {args.task}")
        print(f"✅ Information: {args.information}")
        
        # Test that the schema validates correctly
        print(f"✅ Schema validation passed")
        
        # Test that we can access the fields
        assert args.task == "Merge duplicate 'email_address' and 'email' categories"
        assert "Found two categories" in args.information
        
        print("🎉 All schema tests passed! taxonomy_team_manager uses 'task' field correctly.")
        
    except Exception as e:
        print(f"❌ Schema test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True

if __name__ == "__main__":
    success = test_taxonomy_team_manager_schema()
    sys.exit(0 if success else 1)


