#!/usr/bin/env python3
"""
Script to fix orphaned edges in the knowledge graph database
"""

import os
import sys

# Add the parent directory to the path to import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.assistant.kg.db.knowledge_graph_db import Node, Edge
from app.models.base import engine
from sqlalchemy.orm import Session

def analyze_orphaned_edges():
    """Analyze orphaned edges in the database"""
    print("🔍 Analyzing orphaned edges...")
    print("=" * 50)
    
    with Session(engine) as session:
        # Get all nodes and edges
        nodes = session.query(Node).all()
        edges = session.query(Edge).all()
        
        print(f"📊 Total nodes: {len(nodes)}")
        print(f"🔗 Total edges: {len(edges)}")
        
        # Get node IDs
        node_ids = {str(node.id) for node in nodes}
        
        # Find orphaned edges
        orphaned_edges = []
        valid_edges = []
        
        for edge in edges:
            source_id = str(edge.source_id)
            target_id = str(edge.target_id)
            
            if source_id not in node_ids or target_id not in node_ids:
                orphaned_edges.append(edge)
            else:
                valid_edges.append(edge)
        
        print(f"✅ Valid edges: {len(valid_edges)}")
        print(f"⚠️ Orphaned edges: {len(orphaned_edges)}")
        
        if orphaned_edges:
            print("\n🔍 Orphaned edge details:")
            for edge in orphaned_edges:
                source_exists = str(edge.source_id) in node_ids
                target_exists = str(edge.target_id) in node_ids
                print(f"  - {edge.source_id} -> {edge.target_id} ({edge.relationship_type})")
                print(f"    Source exists: {source_exists}, Target exists: {target_exists}")
        
        return orphaned_edges, valid_edges

def delete_orphaned_edges():
    """Delete all orphaned edges from the database"""
    print("🗑️ Deleting orphaned edges...")
    
    with Session(engine) as session:
        try:
            # Get all node IDs
            node_ids = {str(node.id) for node in session.query(Node).all()}
            
            # Find and delete orphaned edges
            orphaned_edges = session.query(Edge).filter(
                ~(Edge.source_id.in_(node_ids)) | ~(Edge.target_id.in_(node_ids))
            ).all()
            
            orphaned_count = len(orphaned_edges)
            
            for edge in orphaned_edges:
                session.delete(edge)
            
            session.commit()
            
            print(f"✅ Deleted {orphaned_count} orphaned edges")
            
            # Verify
            remaining_edges = session.query(Edge).count()
            print(f"📊 Remaining edges: {remaining_edges}")
            
        except Exception as e:
            session.rollback()
            print(f"❌ Error deleting orphaned edges: {e}")

def reconnect_edges_to_demo_data():
    """Reconnect orphaned edges to existing nodes (for demo purposes)"""
    print("🔗 Reconnecting orphaned edges...")
    
    with Session(engine) as session:
        try:
            # Get existing nodes
            nodes = session.query(Node).all()
            if not nodes:
                print("❌ No nodes found to reconnect to")
                return
            
            # Get orphaned edges
            node_ids = {str(node.id) for node in nodes}
            orphaned_edges = session.query(Edge).filter(
                ~(Edge.source_id.in_(node_ids)) | ~(Edge.target_id.in_(node_ids))
            ).all()
            
            if not orphaned_edges:
                print("✅ No orphaned edges found")
                return
            
            print(f"🔍 Found {len(orphaned_edges)} orphaned edges to reconnect")
            
            # Get first two nodes to reconnect to
            if len(nodes) >= 2:
                node1 = nodes[0]
                node2 = nodes[1]
                
                reconnected_count = 0
                for edge in orphaned_edges:
                    # Reconnect to existing nodes
                    edge.source_id = node1.id
                    edge.target_id = node2.id
                    reconnected_count += 1
                
                session.commit()
                print(f"✅ Reconnected {reconnected_count} edges to {node1.label} -> {node2.label}")
            else:
                print("❌ Need at least 2 nodes to reconnect edges")
                
        except Exception as e:
            session.rollback()
            print(f"❌ Error reconnecting edges: {e}")


def main():
    """Main function"""
    print("🔧 Edge Fix Tool")
    print("=" * 50)
    
    while True:
        print("\nOptions:")
        print("1. Analyze orphaned edges")
        print("2. Delete orphaned edges")
        print("3. Reconnect edges to existing nodes")
        print("4. Exit")
        
        choice = input("\nEnter your choice (1-5): ").strip()
        
        if choice == "1":
            analyze_orphaned_edges()
        elif choice == "2":
            if input("Are you sure you want to delete orphaned edges? (y/N): ").lower() == 'y':
                delete_orphaned_edges()
        elif choice == "3":
            if input("Reconnect orphaned edges to existing nodes? (y/N): ").lower() == 'y':
                reconnect_edges_to_demo_data()

        elif choice == "4":
            print("👋 Goodbye!")
            break
        else:
            print("❌ Invalid choice. Please try again.")

if __name__ == "__main__":
    main() 