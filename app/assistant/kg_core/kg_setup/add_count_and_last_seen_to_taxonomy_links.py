"""
Migration: Add count and last_seen columns to node_taxonomy_links table.

This enables tracking how often each taxonomy classification occurs and when
it was last seen, supporting:
- Progressive classification (counts accumulate over time)
- Multi-dimensional tagging (nodes can have multiple types)
- Emergence of truth (most common classification rises to top)
- Temporal tracking (see when classifications were last confirmed)
"""

from sqlalchemy import text
from app.assistant.kg.db.knowledge_graph_db import get_session

print("🔧 Adding count and last_seen to node_taxonomy_links table...")

session = get_session()
engine = session.bind

try:
    with engine.connect() as conn:
        # Check if columns already exist
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = inspector.get_columns('node_taxonomy_links')
        column_names = [col['name'] for col in columns]
        
        # Add count column (default 1)
        if 'count' not in column_names:
            conn.execute(text("""
                ALTER TABLE node_taxonomy_links 
                ADD COLUMN count INTEGER DEFAULT 1 NOT NULL
            """))
            conn.commit()
            print("✅ Added 'count' column")
        else:
            print("✅ 'count' column already exists")
        
        # Add last_seen column (timestamp)
        if 'last_seen' not in column_names:
            conn.execute(text("""
                ALTER TABLE node_taxonomy_links 
                ADD COLUMN last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            """))
            conn.commit()
            print("✅ Added 'last_seen' column")
        else:
            print("✅ 'last_seen' column already exists")
        
        # Create index on count for sorting by frequency
        indexes = inspector.get_indexes('node_taxonomy_links')
        index_names = [idx['name'] for idx in indexes]
        
        if 'ix_node_taxonomy_links_count' not in index_names:
            conn.execute(text("""
                CREATE INDEX ix_node_taxonomy_links_count 
                ON node_taxonomy_links(count DESC)
            """))
            conn.commit()
            print("✅ Created index on count")
        else:
            print("✅ Index on count already exists")
        
        # Create index on last_seen for temporal queries
        if 'ix_node_taxonomy_links_last_seen' not in index_names:
            conn.execute(text("""
                CREATE INDEX ix_node_taxonomy_links_last_seen 
                ON node_taxonomy_links(last_seen DESC)
            """))
            conn.commit()
            print("✅ Created index on last_seen")
        else:
            print("✅ Index on last_seen already exists")

except Exception as e:
    print(f"❌ Migration failed: {e}")
    session.rollback()
    raise
finally:
    session.close()

print("🎉 Migration complete!")

