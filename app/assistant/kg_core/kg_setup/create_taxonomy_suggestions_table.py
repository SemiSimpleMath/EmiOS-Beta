"""
Create table to track taxonomy suggestions from the classifier agent.

This table captures when the agent suggests new taxonomy types that should be created.
Phase 2 (Researcher Agent) will process these suggestions to build out the taxonomy.
"""

import sys
from sqlalchemy import text
from app.models.base import get_session


def create_taxonomy_suggestions_table():
    """Create taxonomy_suggestions table."""
    print("🔧 Creating taxonomy_suggestions table...")
    
    session = get_session()
    engine = session.bind
    
    try:
        with engine.connect() as conn:
            # Check if table already exists
            from sqlalchemy import inspect
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            
            if 'taxonomy_suggestions' in tables:
                print("✅ 'taxonomy_suggestions' table already exists - nothing to do!")
                return
            
            # Create the table
            conn.execute(text("""
                CREATE TABLE taxonomy_suggestions (
                    id SERIAL PRIMARY KEY,
                    suggested_type VARCHAR(255) NOT NULL,
                    node_label VARCHAR(255) NOT NULL,
                    node_sentence TEXT,
                    node_type VARCHAR(50),
                    parent_candidate_id INTEGER REFERENCES taxonomy(id) ON DELETE SET NULL,
                    match_quality INTEGER,
                    count INTEGER DEFAULT 1,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """))
            conn.commit()
            print("✅ Created 'taxonomy_suggestions' table")
            
            # Create indexes
            print("📋 Creating indexes...")
            conn.execute(text("CREATE INDEX ix_taxonomy_suggestions_suggested_type ON taxonomy_suggestions(suggested_type)"))
            conn.execute(text("CREATE INDEX ix_taxonomy_suggestions_count ON taxonomy_suggestions(count DESC)"))
            conn.execute(text("CREATE INDEX ix_taxonomy_suggestions_created_at ON taxonomy_suggestions(created_at DESC)"))
            conn.commit()
            print("✅ Created indexes")
            
            print("\n🎉 Successfully created taxonomy_suggestions table!")
            print("\n💡 This table will track:")
            print("   • Suggested new taxonomy types from classifier agent")
            print("   • Context (label, sentence, node_type)")
            print("   • Frequency (count field increments when same suggestion appears)")
            print("   • Parent candidate (which existing type could be the parent)")
            print("\n🚀 Phase 2: Researcher agent will process these to build taxonomy hierarchy")

    except Exception as e:
        print(f"❌ Error creating taxonomy_suggestions table: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    create_taxonomy_suggestions_table()
