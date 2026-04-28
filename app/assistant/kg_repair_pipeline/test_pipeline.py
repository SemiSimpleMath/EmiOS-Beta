"""
Test script for the KG Repair Pipeline

This script demonstrates how to use the pipeline to repair knowledge graph issues.
"""

import sys
import os
from datetime import datetime, timezone

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pipeline_orchestrator import KGPipelineOrchestrator
from data_models.problematic_node import ProblematicNode
from data_models.user_response import UserResponse, ResponseType
from utils.logging_config import get_logger

logger = get_logger(__name__)

def test_pipeline():
    """Test the KG repair pipeline with mock data."""
    
    print("🚀 Starting KG Repair Pipeline Test")
    print("=" * 50)
    
    try:
        # Create pipeline orchestrator
        pipeline = KGPipelineOrchestrator()
        
        # Mock KG info for testing
        kg_info = {
            "total_nodes": 1000,
            "total_edges": 2500,
            "last_analyzed": datetime.now(timezone.utc).isoformat()
        }
        
        print(f"📊 Input KG Info: {kg_info}")
        print()
        
        # Run the pipeline
        print("🔄 Running pipeline...")
        result = pipeline.run_pipeline(kg_info)
        
        # Display results
        print("📋 Pipeline Results:")
        print(f"  Pipeline ID: {result.pipeline_id}")
        print(f"  Final Stage: {result.current_stage}")
        print(f"  Total Nodes Identified: {result.total_nodes_identified}")
        print(f"  Nodes Validated: {result.nodes_validated}")
        print(f"  Nodes Questioned: {result.nodes_questioned}")
        print(f"  Nodes Resolved: {result.nodes_resolved}")
        print(f"  Nodes Skipped: {result.nodes_skipped}")
        
        if result.errors:
            print(f"  Errors: {result.errors}")
            
        print()
        print("✅ Pipeline test completed!")
        
        return result
        
    except Exception as e:
        print(f"❌ Pipeline test failed: {e}")
        logger.error(f"Pipeline test failed: {e}")
        return None

def test_individual_stages():
    """Test individual pipeline stages."""
    
    print("\n🧪 Testing Individual Stages")
    print("=" * 30)
    
    try:
        from stages.analyzer import KGAnalyzer
        from stages.critic import KGCritic
        from stages.questioner import KGQuestioner
        from stages.implementer import KGImplementer
        
        # Test Analyzer
        print("🔍 Testing Analyzer...")
        analyzer = KGAnalyzer()
        analysis_result = analyzer.analyze_kg({"test": True})
        print(f"  Analysis Result: {analysis_result}")
        
        # Test Critic
        print("🎯 Testing Critic...")
        critic = KGCritic()
        test_node = ProblematicNode(
            node_id="test_node_123",
            problem_description="Missing start_date and end_date for wedding event"
        )
        critique_result = critic.critique_node(test_node)
        print(f"  Critique Result: {critique_result}")
        
        # Test Questioner (would need actual user interaction)
        print("❓ Testing Questioner...")
        questioner = KGQuestioner()
        print("  Questioner created (requires user interaction to test fully)")
        
        # Test Implementer
        print("🔧 Testing Implementer...")
        implementer = KGImplementer()
        test_node.user_data = {"start_date": "2023-06-15", "end_date": "2023-06-15"}
        implementation_result = implementer.implement_fixes(test_node)
        print(f"  Implementation Result: {implementation_result}")
        
        print("✅ Individual stage tests completed!")
        
    except Exception as e:
        print(f"❌ Individual stage test failed: {e}")
        logger.error(f"Individual stage test failed: {e}")

def create_mock_problematic_nodes():
    """Create mock problematic nodes for testing."""
    
    mock_nodes = [
        ProblematicNode(
            node_id="node_001",
            problem_description="Missing start_date and end_date for wedding event",
            node_type="event",
            priority="high"
        ),
        ProblematicNode(
            node_id="node_002", 
            problem_description="Orphaned node with no connections to other entities",
            node_type="person",
            priority="medium"
        ),
        ProblematicNode(
            node_id="node_003",
            problem_description="Missing description and confidence score",
            node_type="location",
            priority="low"
        )
    ]
    
    return mock_nodes

if __name__ == "__main__":
    print("🧪 KG Repair Pipeline Test Suite")
    print("=" * 40)
    
    # Test individual stages first
    test_individual_stages()
    
    # Test full pipeline
    test_pipeline()
    
    print("\n🎉 All tests completed!")
