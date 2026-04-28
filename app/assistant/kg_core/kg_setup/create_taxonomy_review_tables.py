"""
Create taxonomy review queue tables for the proposer-critic workflow.

This creates two tables:
1. taxonomy_suggestions_review - New taxonomy type suggestions from critic agent
2. node_taxonomy_review_queue - Low-confidence node classifications needing review
"""

import sys
from sqlalchemy import text
from app.models.base import get_session


def create_taxonomy_review_tables():
    """Create taxonomy review queue tables."""
    print("🔧 Creating taxonomy review queue tables...")
    
    session = get_session()
    engine = session.bind
    
    try:
        with engine.connect() as conn:
            from sqlalchemy import inspect
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            
            # ================================================================
            # TABLE 1: taxonomy_suggestions_review
            # ================================================================
            if 'taxonomy_suggestions_review' in tables:
                print("✅ 'taxonomy_suggestions_review' table already exists - skipping")
            else:
                print("📋 Creating 'taxonomy_suggestions_review' table...")
                conn.execute(text("""
                    CREATE TABLE taxonomy_suggestions_review (
                        id SERIAL PRIMARY KEY,
                        parent_path TEXT NOT NULL,
                        suggested_label VARCHAR(255) NOT NULL,
                        description TEXT,
                        reasoning TEXT,
                        example_nodes JSONB,
                        confidence FLOAT,
                        status VARCHAR(50) DEFAULT 'pending' NOT NULL,
                        reviewed_by VARCHAR(255),
                        reviewed_at TIMESTAMP WITH TIME ZONE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """))
                conn.commit()
                print("✅ Created 'taxonomy_suggestions_review' table")
                
                # Create indexes
                print("   Creating indexes...")
                conn.execute(text("""
                    CREATE INDEX ix_taxonomy_suggestions_review_parent_path 
                    ON taxonomy_suggestions_review(parent_path)
                """))
                conn.execute(text("""
                    CREATE INDEX ix_taxonomy_suggestions_review_suggested_label 
                    ON taxonomy_suggestions_review(suggested_label)
                """))
                conn.execute(text("""
                    CREATE INDEX ix_taxonomy_suggestions_review_status 
                    ON taxonomy_suggestions_review(status)
                """))
                conn.commit()
                print("   ✅ Created indexes")
            
            # ================================================================
            # TABLE 2: node_taxonomy_review_queue
            # ================================================================
            if 'node_taxonomy_review_queue' in tables:
                print("✅ 'node_taxonomy_review_queue' table already exists - skipping")
            else:
                print("📋 Creating 'node_taxonomy_review_queue' table...")
                conn.execute(text("""
                    CREATE TABLE node_taxonomy_review_queue (
                        id SERIAL PRIMARY KEY,
                        node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        proposed_path TEXT,
                        validated_path TEXT,
                        action VARCHAR(50) NOT NULL,
                        confidence FLOAT,
                        reasoning TEXT,
                        status VARCHAR(50) DEFAULT 'pending' NOT NULL,
                        reviewed_by VARCHAR(255),
                        reviewed_at TIMESTAMP WITH TIME ZONE,
                        final_taxonomy_path TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """))
                conn.commit()
                print("✅ Created 'node_taxonomy_review_queue' table")
                
                # Create indexes
                print("   Creating indexes...")
                conn.execute(text("""
                    CREATE INDEX ix_node_taxonomy_review_queue_node_id 
                    ON node_taxonomy_review_queue(node_id)
                """))
                conn.execute(text("""
                    CREATE INDEX ix_node_taxonomy_review_queue_action 
                    ON node_taxonomy_review_queue(action)
                """))
                conn.execute(text("""
                    CREATE INDEX ix_node_taxonomy_review_queue_status 
                    ON node_taxonomy_review_queue(status)
                """))
                conn.commit()
                print("   ✅ Created indexes")
            
            print("\n🎉 Successfully created taxonomy review queue tables!")
            print("\n💡 These tables support the proposer-critic workflow:")
            print("\n📋 taxonomy_suggestions_review:")
            print("   • Captures VALIDATE_INSERT actions from critic")
            print("   • New taxonomy types waiting for human approval")
            print("   • Example nodes showing why the type is needed")
            print("\n📋 node_taxonomy_review_queue:")
            print("   • Captures low-confidence classifications (< 0.85)")
            print("   • CORRECT_PATH and REJECT actions from critic")
            print("   • Allows human review before finalizing classification")
            print("\n🌐 View in web UI: http://localhost:5002")

    except Exception as e:
        print(f"❌ Error creating taxonomy review tables: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    create_taxonomy_review_tables()

