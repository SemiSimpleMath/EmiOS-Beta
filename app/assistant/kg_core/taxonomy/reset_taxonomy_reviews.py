"""
Reset Taxonomy Review Tables

Clears all taxonomy review/curation data while preserving the taxonomy structure itself.

Tables cleared:
- node_taxonomy_links (all node classifications)
- taxonomy_review_history (review actions)
- Any other review-related tables

Tables preserved:
- taxonomy (the actual taxonomy hierarchy)
"""

import sys
from sqlalchemy import text
from app.assistant.tests.test_setup import initialize_services

def reset_taxonomy_reviews():
    """Clear all taxonomy review and classification data"""
    
    print("🚀 Initializing services...")
    initialize_services()
    
    from app.models.base import get_session
    
    session = get_session()
    
    try:
        print("\n" + "="*80)
        print("⚠️  WARNING: This will delete:")
        print("  - All node-to-taxonomy classifications (node_taxonomy_links)")
        print("  - All pending taxonomy suggestions (taxonomy_suggestions_review)")
        print("  - All pending node reviews (node_taxonomy_review_queue)")
        print("\n✅ This will preserve:")
        print("  - The taxonomy structure (taxonomy table)")
        print("  - All nodes and edges in the knowledge graph")
        print("="*80)
        
        response = input("\n❓ Are you sure you want to proceed? (yes/no): ")
        if response.lower() != 'yes':
            print("❌ Aborted.")
            return
        
        print("\n🗑️  Clearing taxonomy review data...")
        
        # Get counts before deletion
        node_taxonomy_count = session.execute(
            text("SELECT COUNT(*) FROM node_taxonomy_links")
        ).scalar()
        
        suggestions_count = session.execute(
            text("SELECT COUNT(*) FROM taxonomy_suggestions_review")
        ).scalar()
        
        review_queue_count = session.execute(
            text("SELECT COUNT(*) FROM node_taxonomy_review_queue")
        ).scalar()
        
        print(f"\n📊 Current counts:")
        print(f"  - node_taxonomy_links: {node_taxonomy_count}")
        print(f"  - taxonomy_suggestions_review: {suggestions_count}")
        print(f"  - node_taxonomy_review_queue: {review_queue_count}")
        
        # Delete node classifications
        print("\n🗑️  Deleting node_taxonomy_links...")
        session.execute(text("DELETE FROM node_taxonomy_links"))
        
        # Delete pending suggestions
        print("🗑️  Deleting taxonomy_suggestions_review...")
        session.execute(text("DELETE FROM taxonomy_suggestions_review"))
        
        # Delete pending reviews
        print("🗑️  Deleting node_taxonomy_review_queue...")
        session.execute(text("DELETE FROM node_taxonomy_review_queue"))
        
        # Commit the changes
        session.commit()
        
        print("\n✅ Successfully cleared taxonomy review data!")
        print(f"  - Deleted {node_taxonomy_count} node classifications")
        print(f"  - Deleted {suggestions_count} pending suggestions")
        print(f"  - Deleted {review_queue_count} pending reviews")
        
        # Verify taxonomy structure is intact
        taxonomy_count = session.execute(
            text("SELECT COUNT(*) FROM taxonomy")
        ).scalar()
        print(f"\n✅ Taxonomy structure preserved: {taxonomy_count} categories")
        
    except Exception as e:
        session.rollback()
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        session.close()
        print("\n✅ Session closed")


if __name__ == "__main__":
    reset_taxonomy_reviews()

