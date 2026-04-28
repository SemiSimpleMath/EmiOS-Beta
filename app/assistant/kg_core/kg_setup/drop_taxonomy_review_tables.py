"""
Drop taxonomy review queue tables.

DANGER: This will delete all pending review data!
"""

import sys
from sqlalchemy import text
from app.models.base import get_session


def drop_taxonomy_review_tables():
    """Drop taxonomy review queue tables."""
    print("⚠️  WARNING: This will drop taxonomy review queue tables!")
    print("   All pending suggestions and reviews will be lost.")
    
    response = input("\n❓ Are you sure? Type 'yes' to continue: ")
    if response.lower() != 'yes':
        print("❌ Aborted")
        return
    
    print("\n🔧 Dropping taxonomy review queue tables...")
    
    session = get_session()
    engine = session.bind
    
    try:
        with engine.connect() as conn:
            from sqlalchemy import inspect
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            
            # Drop node_taxonomy_review_queue first (has FK to nodes)
            if 'node_taxonomy_review_queue' in tables:
                print("📋 Dropping 'node_taxonomy_review_queue' table...")
                conn.execute(text("DROP TABLE IF EXISTS node_taxonomy_review_queue CASCADE"))
                conn.commit()
                print("✅ Dropped 'node_taxonomy_review_queue'")
            else:
                print("✅ 'node_taxonomy_review_queue' doesn't exist - nothing to drop")
            
            # Drop taxonomy_suggestions_review
            if 'taxonomy_suggestions_review' in tables:
                print("📋 Dropping 'taxonomy_suggestions_review' table...")
                conn.execute(text("DROP TABLE IF EXISTS taxonomy_suggestions_review CASCADE"))
                conn.commit()
                print("✅ Dropped 'taxonomy_suggestions_review'")
            else:
                print("✅ 'taxonomy_suggestions_review' doesn't exist - nothing to drop")
            
            print("\n🎉 Successfully dropped taxonomy review queue tables!")

    except Exception as e:
        print(f"❌ Error dropping tables: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    drop_taxonomy_review_tables()

