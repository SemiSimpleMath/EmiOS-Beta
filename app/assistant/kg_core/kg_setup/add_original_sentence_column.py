"""
Migration: Add 'original_sentence' column to nodes table.

This column stores the sentence that first created each node as immutable provenance data.
Critical for distinguishing State nodes with the same label but different contexts.

Usage:
    python add_original_sentence_column.py
"""

import app.assistant.tests.test_setup  # Initialize everything

from app.models.base import get_session
from sqlalchemy import text

print("🔧 Adding 'original_sentence' column to nodes table...")

session = get_session()
engine = session.bind

print(f"🔍 Database: {engine.url}")

try:
    # Check if column already exists
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'nodes' 
            AND column_name = 'original_sentence';
        """))
        
        exists = result.fetchone()
        
        if exists:
            print("✅ 'original_sentence' column already exists - nothing to do!")
        else:
            # Add the column
            conn.execute(text("""
                ALTER TABLE nodes 
                ADD COLUMN original_sentence TEXT NULL;
            """))
            
            # Create index for efficient searching
            conn.execute(text("""
                CREATE INDEX ix_nodes_original_sentence 
                ON nodes (original_sentence);
            """))
            
            conn.commit()
            print("✅ Successfully added 'original_sentence' column!")
            print("✅ Created index 'ix_nodes_original_sentence'!")
            print("\n📊 Column Details:")
            print("  - Name: original_sentence")
            print("  - Type: TEXT")
            print("  - Nullable: True")
            print("  - Purpose: Immutable provenance - stores the sentence that first created this node")
            print("\n💡 This field is especially important for State nodes to distinguish")
            print("   different instances with the same label (e.g., multiple 'Ownership' states)")
            
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
finally:
    session.close()

print("\n✅ Migration complete!")

