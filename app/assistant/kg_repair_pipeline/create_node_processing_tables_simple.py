"""
Simple script to create Node Processing Tracking tables.
"""

import os
import sys

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from app.models.base import get_session, Base

def create_tables():
    """Create the node processing tracking tables."""
    print("🔧 Creating Node Processing Tracking Tables...")
    
    try:
        # Get database session
        session = get_session()
        engine = session.bind
        
        print(f"🔍 Database: {engine.url}")
        
        # Import only the models we need to create
        # Avoid importing KG models that use VECTOR type (requires pgvector extension)
        
        # Import core models (these are safe to import)
        from app.assistant.database.db_handler import UnifiedLog2026, InfoDatabase, AgentActivityLog, RAGDatabase, EventRepository, EmailCheckState
        from app.assistant.database.processed_entity_log import ProcessedEntityLog
        from app.models.maintenance_logs import MaintenanceRunLog
        from app.models.duplicate_node_tracking import DuplicateNodeTracking
        from app.models.node_analysis_tracking import NodeAnalysisTracking
        
        # Import our new models
        from app.assistant.kg_repair_pipeline.data_models.node_processing_tracking import (
            NodeProcessingStatus, 
            NodeProcessingBatch, 
            NodeProcessingStatistics
        )
        
        # Drop existing tables if they exist (to handle schema changes)
        print("🔄 Dropping existing tables if they exist...")
        with engine.connect() as conn:
            from sqlalchemy import text
            
            tables_to_drop = [
                'node_processing_status',
                'node_processing_batches', 
                'node_processing_statistics'
            ]
            
            for table_name in tables_to_drop:
                try:
                    conn.execute(text(f"DROP TABLE IF EXISTS {table_name} CASCADE;"))
                    print(f"  ✅ Dropped {table_name} if it existed")
                except Exception as e:
                    print(f"  ⚠️  Could not drop {table_name}: {e}")
            
            conn.commit()
        
        # Create all tables
        print("🔄 Creating tables...")
        Base.metadata.create_all(engine, checkfirst=True)
        
        print("✅ Tables created successfully!")
        
        # Verify tables were created
        print("\n🔍 Verifying table creation...")
        with engine.connect() as conn:
            from sqlalchemy import text
            tables_to_check = [
                'node_processing_status',
                'node_processing_batches', 
                'node_processing_statistics'
            ]
            
            for table_name in tables_to_check:
                result = conn.execute(text(f"""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'public' 
                        AND table_name = '{table_name}'
                    );
                """))
                
                if result.fetchone()[0]:
                    print(f"  ✅ {table_name} table exists")
                else:
                    print(f"  ❌ {table_name} table missing")
        
        session.close()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    create_tables()
