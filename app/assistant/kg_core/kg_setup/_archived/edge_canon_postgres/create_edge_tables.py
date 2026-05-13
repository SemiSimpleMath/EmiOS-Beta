"""
Create edge_canon and edge_alias tables with simplified schema:
- edge_type (not relationship_type_snake)
- edge_type_embedding VECTOR(384) (not embedding JSONB)
"""

from app.assistant.kg.db.knowledge_graph_db import get_session
from sqlalchemy import text

def main():
    print("🔧 Creating edge_canon and edge_alias tables with new schema...")
    
    session = get_session()
    engine = session.bind
    
    print(f"🔍 Database: {engine.url}")
    
    try:
        with engine.connect() as conn:
            # Create edge_canon table
            print("📝 Creating edge_canon table...")
            conn.execute(text("""
                CREATE TABLE edge_canon (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    domain_type VARCHAR NOT NULL,
                    range_type VARCHAR NOT NULL,
                    edge_type VARCHAR NOT NULL,
                    edge_type_embedding VECTOR(384),
                    is_symmetric BOOLEAN DEFAULT FALSE,
                    status VARCHAR DEFAULT 'active',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    created_by VARCHAR
                )
            """))
            conn.commit()
            print("✅ Created edge_canon table")
            
            # Create indexes on edge_canon
            print("📝 Creating indexes on edge_canon...")
            conn.execute(text("CREATE INDEX ix_edge_canon_domain_type ON edge_canon(domain_type)"))
            conn.execute(text("CREATE INDEX ix_edge_canon_range_type ON edge_canon(range_type)"))
            conn.execute(text("CREATE INDEX ix_edge_canon_edge_type ON edge_canon(edge_type)"))
            conn.execute(text("CREATE INDEX ix_edge_canon_status ON edge_canon(status)"))
            conn.commit()
            print("✅ Created indexes on edge_canon")
            
            # Create edge_alias table
            print("📝 Creating edge_alias table...")
            conn.execute(text("""
                CREATE TABLE edge_alias (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    canon_id UUID NOT NULL REFERENCES edge_canon(id) ON DELETE CASCADE,
                    raw_text VARCHAR NOT NULL,
                    domain_type VARCHAR NOT NULL,
                    range_type VARCHAR NOT NULL,
                    method VARCHAR,
                    confidence FLOAT,
                    provenance JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """))
            conn.commit()
            print("✅ Created edge_alias table")
            
            # Create indexes on edge_alias
            print("📝 Creating indexes on edge_alias...")
            conn.execute(text("CREATE INDEX ix_edge_alias_canon_id ON edge_alias(canon_id)"))
            conn.execute(text("CREATE INDEX ix_edge_alias_raw_text ON edge_alias(raw_text)"))
            conn.execute(text("CREATE INDEX ix_edge_alias_domain_type ON edge_alias(domain_type)"))
            conn.execute(text("CREATE INDEX ix_edge_alias_range_type ON edge_alias(range_type)"))
            conn.commit()
            print("✅ Created indexes on edge_alias")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        session.rollback()
        raise
    finally:
        session.close()
    
    print("🎉 Tables created successfully!")
    print("\n📋 Next step: Run seed_edge_types.py")

if __name__ == "__main__":
    main()

