"""
Clear all KG data (nodes, edges, classifications) but preserve taxonomy structure.

This will:
- Delete all nodes and edges
- Clear all node-taxonomy classifications
- Clear all review queues and suggestions
- KEEP the taxonomy hierarchy intact
"""

from sqlalchemy import text
from app.assistant.kg.db.knowledge_graph_db import get_session

print("🧹 CLEARING KG DATA (preserving taxonomy structure)...")
print("This will delete:")
print("  - All nodes and edges")
print("  - All node-taxonomy classifications") 
print("  - All review queues and suggestions")
print("  - KEEP taxonomy hierarchy intact")
print("\n⚠️  Press Ctrl+C within 3 seconds to cancel...")

import time
time.sleep(3)

print("\n🗑️  Clearing KG data...")

session = get_session()
engine = session.bind

def table_exists(conn, table_name):
    """Check if a table exists in the database."""
    result = conn.execute(text("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = :table_name
        )
    """), {"table_name": table_name})
    return result.scalar()

try:
    with engine.connect() as conn:
        # Clear in dependency order (child tables first)
        
        if table_exists(conn, "node_taxonomy_links"):
            print("1. Clearing node_taxonomy_links...")
            conn.execute(text("DELETE FROM node_taxonomy_links"))
            conn.commit()
        else:
            print("1. Skipping node_taxonomy_links (table doesn't exist)")
        
        if table_exists(conn, "node_taxonomy_review_queue"):
            print("2. Clearing node_taxonomy_review_queue...")
            conn.execute(text("DELETE FROM node_taxonomy_review_queue"))
            conn.commit()
        else:
            print("2. Skipping node_taxonomy_review_queue (table doesn't exist)")
        
        if table_exists(conn, "taxonomy_suggestions_review"):
            print("3. Clearing taxonomy_suggestions_review...")
            conn.execute(text("DELETE FROM taxonomy_suggestions_review"))
            conn.commit()
        else:
            print("3. Skipping taxonomy_suggestions_review (table doesn't exist)")
        
        if table_exists(conn, "taxonomy_suggestions"):
            print("4. Clearing taxonomy_suggestions...")
            conn.execute(text("DELETE FROM taxonomy_suggestions"))
            conn.commit()
        else:
            print("4. Skipping taxonomy_suggestions (table doesn't exist)")
        
        if table_exists(conn, "edges"):
            print("5. Clearing edges...")
            conn.execute(text("DELETE FROM edges"))
            conn.commit()
        else:
            print("5. Skipping edges (table doesn't exist)")
        
        if table_exists(conn, "nodes"):
            print("6. Clearing nodes...")
            conn.execute(text("DELETE FROM nodes"))
            conn.commit()
        else:
            print("6. Skipping nodes (table doesn't exist)")
        
        if table_exists(conn, "review_queue"):
            print("7. Clearing review_queue...")
            conn.execute(text("DELETE FROM review_queue"))
            conn.commit()
        else:
            print("7. Skipping review_queue (table doesn't exist)")
        
        print("\n✅ KG data cleared successfully!")
        print("📊 Taxonomy structure preserved - ready for new data!")

except Exception as e:
    print(f"\n❌ Error during cleanup: {e}")
    session.rollback()
    raise
finally:
    session.close()

print("\n🎯 Database is now clean but taxonomy structure remains intact!")
