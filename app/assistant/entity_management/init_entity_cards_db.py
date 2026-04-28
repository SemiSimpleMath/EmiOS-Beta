"""
Initialize Entity Cards Database
Creates the entity cards tables in the database
"""

import os
# We'll use force_test_db=True in get_session() instead

from app.assistant.entity_management.entity_cards import (
    initialize_entity_cards_db, 
    get_entity_card_stats, 
    check_entity_cards_db_exists
)
from app.models.base import get_session
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main():
    """Initialize the entity cards database"""
    print("Initializing Entity Cards Database...")
    
    # Debug: Show which database we're using
    from app.models.base import get_database_uri
    print(f"Using database: {get_database_uri()}")
    print()
    
    try:
        # Check current status
        status = check_entity_cards_db_exists()
        print(f"Current database status:")
        print(f"  Tables exist: {status['exists']}")
        print(f"  Existing tables: {status['existing_tables']}")
        print(f"  Missing tables: {status['missing_tables']}")
        print()
        
        # Initialize tables
        initialize_entity_cards_db(force_test_db=True)
        
        # Show final stats
        session = get_session(force_test_db=True)
        try:
            stats = get_entity_card_stats(session)
            
            print("✅ Entity Cards Database ready!")
            print(f"📊 Database Stats:")
            print(f"  Total cards: {stats['total_cards']}")
            print(f"  Active cards: {stats['active_cards']}")
            print(f"  Total usage: {stats['total_usage']}")
            
            if stats['type_stats']:
                print(f"  Type distribution:")
                for type_stat in stats['type_stats']:
                    print(f"    {type_stat['entity_type']}: {type_stat['count']} cards")
            else:
                print("  No entity cards yet - run the pipeline to create some!")
                
        except Exception as e:
            print(f"⚠️  Could not get stats (tables may be empty): {e}")
            print("This is normal for newly created tables.")
        finally:
            session.close()
        
    except Exception as e:
        logger.error(f"Error initializing entity cards database: {e}")
        print(f"❌ Error: {e}")
        print("\nIf you need to reset the database, use:")
        print("from app.models.entity_cards import reset_entity_cards_db")
        print("reset_entity_cards_db()")


if __name__ == "__main__":
    main()
