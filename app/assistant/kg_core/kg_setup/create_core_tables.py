"""
Create core KG tables (nodes, edges, ENUM) using SQLAlchemy models.

This must run BEFORE taxonomy tables since they have foreign keys to nodes.

Simply calls the __main__ logic from knowledge_graph_db.py.
"""

from app.assistant.kg.db.knowledge_graph_db import initialize_knowledge_graph_db

print("🔧 Creating core KG tables (nodes, edges, ENUM)...")

try:
    # This function already has all the logic to create tables properly
    initialize_knowledge_graph_db()
    
    print("\n✅ Successfully created core tables:")
    print("   ✅ node_type_enum (Entity, Event, State, Goal, Concept, Property)")
    print("   ✅ nodes table")
    print("   ✅ edges table")
    print("\n🎉 Core tables ready!")
    print("📊 Ready for taxonomy and standardization tables!")

except Exception as e:
    print(f"\n❌ Error creating core tables: {e}")
    import traceback
    traceback.print_exc()
    raise

