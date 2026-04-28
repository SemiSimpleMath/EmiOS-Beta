#!/usr/bin/env python3
"""
Clean up duplicate pending reviews for the same node.

When the same node has multiple pending reviews, keep only the most recent one
and mark the others as 'superseded'.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import NodeTaxonomyReviewQueue
from sqlalchemy import func
from datetime import datetime


def cleanup_duplicate_reviews():
    """Find and clean up duplicate pending reviews for the same node."""
    session = get_session()
    
    try:
        print("🔍 Finding duplicate pending reviews...")
        
        # Find nodes with multiple pending reviews
        duplicates = session.query(
            NodeTaxonomyReviewQueue.node_id,
            func.count(NodeTaxonomyReviewQueue.id).label('count')
        ).filter(
            NodeTaxonomyReviewQueue.status == 'pending'
        ).group_by(
            NodeTaxonomyReviewQueue.node_id
        ).having(
            func.count(NodeTaxonomyReviewQueue.id) > 1
        ).all()
        
        if not duplicates:
            print("✅ No duplicate reviews found!")
            return
        
        print(f"📊 Found {len(duplicates)} nodes with multiple pending reviews")
        
        total_updated = 0
        
        for node_id, count in duplicates:
            print(f"\n  Node {node_id}: {count} pending reviews")
            
            # Get all pending reviews for this node, ordered by created_at desc
            reviews = session.query(NodeTaxonomyReviewQueue).filter(
                NodeTaxonomyReviewQueue.node_id == node_id,
                NodeTaxonomyReviewQueue.status == 'pending'
            ).order_by(
                NodeTaxonomyReviewQueue.created_at.desc()
            ).all()
            
            # Keep the first (most recent), mark others as superseded
            for i, review in enumerate(reviews):
                if i == 0:
                    print(f"    ✅ Keeping review {review.id} (most recent)")
                else:
                    review.status = 'superseded'
                    review.reviewed_at = datetime.utcnow()
                    print(f"    ❌ Marking review {review.id} as superseded")
                    total_updated += 1
        
        session.commit()
        print(f"\n✅ Cleanup complete! Marked {total_updated} duplicate reviews as superseded")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error during cleanup: {str(e)}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    cleanup_duplicate_reviews()

