"""
Drop all KG-related tables in correct dependency order.

CAREFUL: This destroys all graph data!
"""

from sqlalchemy import text
from app.assistant.kg.db.knowledge_graph_db import get_session

print("⚠️  WARNING: This will delete ALL knowledge graph data!")
print("⚠️  Press Ctrl+C within 5 seconds to cancel...")

import time
time.sleep(5)

print("\n🗑️  Dropping all KG tables...")

session = get_session()
engine = session.bind

try:
    with engine.connect() as conn:
        # Drop in reverse dependency order
        
        print("1. Dropping edges...")
        conn.execute(text("DROP TABLE IF EXISTS edges CASCADE"))
        conn.commit()
        
        print("2. Dropping node_taxonomy_links...")
        conn.execute(text("DROP TABLE IF EXISTS node_taxonomy_links CASCADE"))
        conn.commit()
        
        print("3. Dropping node_taxonomy_review_queue...")
        conn.execute(text("DROP TABLE IF EXISTS node_taxonomy_review_queue CASCADE"))
        conn.commit()
        
        print("4. Dropping taxonomy_suggestions_review...")
        conn.execute(text("DROP TABLE IF EXISTS taxonomy_suggestions_review CASCADE"))
        conn.commit()
        
        print("5. Dropping taxonomy_suggestions...")
        conn.execute(text("DROP TABLE IF EXISTS taxonomy_suggestions CASCADE"))
        conn.commit()
        
        print("6. Dropping nodes...")
        conn.execute(text("DROP TABLE IF EXISTS nodes CASCADE"))
        conn.commit()
        
        print("7. Dropping taxonomy...")
        conn.execute(text("DROP TABLE IF EXISTS taxonomy CASCADE"))
        conn.commit()
        
        print("8. Dropping edge_alias...")
        conn.execute(text("DROP TABLE IF EXISTS edge_alias CASCADE"))
        conn.commit()
        
        print("9. Dropping edge_canon...")
        conn.execute(text("DROP TABLE IF EXISTS edge_canon CASCADE"))
        conn.commit()
        
        print("10. Dropping label_alias...")
        conn.execute(text("DROP TABLE IF EXISTS label_alias CASCADE"))
        conn.commit()
        
        print("11. Dropping label_canon...")
        conn.execute(text("DROP TABLE IF EXISTS label_canon CASCADE"))
        conn.commit()
        
        print("12. Dropping review_queue...")
        conn.execute(text("DROP TABLE IF EXISTS review_queue CASCADE"))
        conn.commit()
        
        print("\n✅ All KG tables dropped successfully!")

except Exception as e:
    print(f"\n❌ Error during drop: {e}")
    session.rollback()
    raise
finally:
    session.close()

print("\n📊 Database is now clean - ready for table creation!")

