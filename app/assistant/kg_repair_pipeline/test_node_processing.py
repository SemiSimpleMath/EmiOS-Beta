"""
Test script for the Node Processing Tracking system.

Demonstrates how the system tracks node processing status, user interactions,
and scheduling.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime, timezone, timedelta
from app.assistant.kg_repair_pipeline.utils.node_processing_manager import NodeProcessingManager
from app.assistant.kg_repair_pipeline.data_models.node_processing_tracking import NodeProcessingStatus

def test_node_processing_tracking():
    """Test the node processing tracking system."""
    print("🧪 Testing Node Processing Tracking System")
    print("=" * 50)
    
    # Initialize processing manager
    manager = NodeProcessingManager()
    
    # Test 1: Create node processing status
    print("\n1️⃣ Creating node processing status...")
    node_id = "test_node_123"
    status = manager.create_node_status(
        node_id=node_id,
        node_label="Test Wedding Event",
        node_type="event",
        problem_description="Missing start_date and end_date",
        priority="high"
    )
    print(f"✅ Created status for node {node_id}: {status.status}")
    
    # Test 2: Update node status
    print("\n2️⃣ Updating node status...")
    success = manager.update_node_status(
        node_id=node_id,
        status="analyzing",
        analyzer_suggestion="Need to add wedding dates"
    )
    print(f"✅ Updated status: {success}")
    
    # Test 3: Record user response
    print("\n3️⃣ Recording user response...")
    success = manager.record_user_response(
        node_id=node_id,
        user_response="The wedding was on June 15, 2023 at St. Mary's Church",
        response_type="provide_data",
        provided_data={
            "start_date": "2023-06-15",
            "description": "wedding at St. Mary's Church"
        },
        confidence=0.9
    )
    print(f"✅ Recorded user response: {success}")
    
    # Test 4: Schedule node for later
    print("\n4️⃣ Scheduling node for later...")
    schedule_time = datetime.now(timezone.utc) + timedelta(hours=2)
    success = manager.schedule_node_for_later(
        node_id="test_node_456",
        schedule_time=schedule_time
    )
    print(f"✅ Scheduled node for later: {success}")
    
    # Test 5: Mark node as invalid
    print("\n5️⃣ Marking node as invalid...")
    success = manager.mark_node_as_invalid(
        node_id="test_node_789",
        reason="User confirmed this is not a problem"
    )
    print(f"✅ Marked node as invalid: {success}")
    
    # Test 6: Get nodes for processing
    print("\n6️⃣ Getting nodes for processing...")
    nodes = manager.get_nodes_for_processing(max_nodes=5)
    print(f"✅ Found {len(nodes)} nodes for processing")
    for node in nodes:
        print(f"  - {node.node_label}: {node.status} (priority: {node.priority})")
    
    # Test 7: Get processing statistics
    print("\n7️⃣ Getting processing statistics...")
    stats = manager.get_processing_statistics(days=7)
    print(f"✅ Statistics: {stats}")
    
    # Test 8: Get nodes to skip
    print("\n8️⃣ Getting nodes to skip...")
    skip_nodes = manager.get_nodes_to_skip()
    print(f"✅ Found {len(skip_nodes)} nodes to skip")
    
    print("\n🎉 Node Processing Tracking System Test Complete!")
    print("=" * 50)

def test_scheduling_system():
    """Test the scheduling system for nodes."""
    print("\n📅 Testing Scheduling System")
    print("=" * 30)
    
    manager = NodeProcessingManager()
    
    # Create some test nodes with different scheduling scenarios
    test_nodes = [
        {
            "node_id": "scheduled_1",
            "node_label": "Scheduled Node 1",
            "schedule_time": datetime.now(timezone.utc) + timedelta(minutes=5)
        },
        {
            "node_id": "scheduled_2", 
            "node_label": "Scheduled Node 2",
            "schedule_time": datetime.now(timezone.utc) + timedelta(hours=1)
        },
        {
            "node_id": "scheduled_3",
            "node_label": "Scheduled Node 3", 
            "schedule_time": datetime.now(timezone.utc) + timedelta(days=1)
        }
    ]
    
    # Create scheduled nodes
    for node_data in test_nodes:
        status = manager.create_node_status(
            node_id=node_data["node_id"],
            node_label=node_data["node_label"],
            problem_description="Test problem"
        )
        
        manager.schedule_node_for_later(
            node_id=node_data["node_id"],
            schedule_time=node_data["schedule_time"]
        )
        print(f"✅ Scheduled {node_data['node_label']} for {node_data['schedule_time']}")
    
    # Get scheduled nodes (should return nodes ready for review)
    print("\n📋 Getting scheduled nodes...")
    scheduled_nodes = manager.get_scheduled_nodes(max_nodes=10)
    print(f"✅ Found {len(scheduled_nodes)} scheduled nodes ready for review")
    
    for node in scheduled_nodes:
        print(f"  - {node.node_label}: scheduled for {node.next_review_at}")

if __name__ == "__main__":
    try:
        test_node_processing_tracking()
        test_scheduling_system()
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
