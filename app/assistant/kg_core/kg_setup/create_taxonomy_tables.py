"""
Migration script to create taxonomy tables.

This implements a clean taxonomy system where:
- taxonomy stores hierarchical TYPE concepts only (animal, dog, ownership)
- node_taxonomy_links creates many-to-many classification between nodes and types
- Instances (Scout, Buddy) are nodes, NOT taxonomy entries
- Progressive classification: nodes can start unclassified, gain taxonomy over time

Design principles:
- Pure adjacency list (id + parent_id)
- No materialized path (use recursive CTEs)
- Taxonomy = stable type hierarchy
- Nodes = instances that reference taxonomy

Run this after creating the main knowledge graph tables.
"""

import sys
from sqlalchemy import text
from app.models.base import get_session


def create_taxonomy_tables():
    """Create taxonomy and node_taxonomy_links tables."""
    print("🔧 Creating taxonomy tables (clean design)...")
    
    session = get_session()
    engine = session.bind
    
    print(f"🔍 Database: {engine.url}")
    
    try:
        with engine.connect() as conn:
            # Check if tables already exist
            result = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name IN ('taxonomy', 'node_taxonomy_links')
            """))
            existing_tables = [row[0] for row in result]
            
            if 'taxonomy' in existing_tables and 'node_taxonomy_links' in existing_tables:
                print("✅ Taxonomy tables already exist - nothing to do!")
                return
            
            # Create taxonomy table
            if 'taxonomy' not in existing_tables:
                print("📋 Creating 'taxonomy' table...")
                conn.execute(text("""
                    CREATE TABLE taxonomy (
                        id SERIAL PRIMARY KEY,
                        label VARCHAR(255) NOT NULL,
                        parent_id INTEGER REFERENCES taxonomy(id) ON DELETE RESTRICT,
                        description TEXT,
                        label_embedding VECTOR(384),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                """))
                conn.commit()
                print("✅ Created 'taxonomy' table")
                
                # Create indexes
                print("📋 Creating indexes for 'taxonomy'...")
                conn.execute(text("CREATE INDEX ix_taxonomy_parent_id ON taxonomy(parent_id)"))
                conn.execute(text("CREATE INDEX ix_taxonomy_label ON taxonomy(label)"))
                conn.commit()
                print("✅ Created indexes for 'taxonomy'")
            
            # Create node_taxonomy_links table
            if 'node_taxonomy_links' not in existing_tables:
                print("📋 Creating 'node_taxonomy_links' table...")
                conn.execute(text("""
                    CREATE TABLE node_taxonomy_links (
                        node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                        taxonomy_id INTEGER NOT NULL REFERENCES taxonomy(id) ON DELETE RESTRICT,
                        confidence FLOAT DEFAULT 1.0 NOT NULL,
                        source VARCHAR(100),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        PRIMARY KEY (node_id, taxonomy_id)
                    )
                """))
                conn.commit()
                print("✅ Created 'node_taxonomy_links' table")
                
                # Create indexes
                print("📋 Creating indexes for 'node_taxonomy_links'...")
                conn.execute(text("CREATE INDEX ix_node_taxonomy_links_taxonomy_id ON node_taxonomy_links(taxonomy_id)"))
                conn.execute(text("CREATE INDEX ix_node_taxonomy_links_node_id ON node_taxonomy_links(node_id)"))
                conn.execute(text("CREATE INDEX ix_node_taxonomy_links_confidence ON node_taxonomy_links(confidence)"))
                conn.commit()
                print("✅ Created indexes for 'node_taxonomy_links'")
            
            print("\n🎉 Taxonomy tables created successfully!")
            print("\n📊 Table structure:")
            print("   taxonomy: Type hierarchy (animal→dog, state→ownership)")
            print("   node_taxonomy_links: Many-to-many node classification")
            print("\n✨ Key features:")
            print("   • Types only (no instances in taxonomy)")
            print("   • Pure adjacency list (recursive CTEs for queries)")
            print("   • Progressive classification (nodes gain taxonomy over time)")
            print("   • Multi-dimensional tagging (nodes can have multiple taxonomies)")

    except Exception as e:
        print(f"❌ Error creating taxonomy tables: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    create_taxonomy_tables()
