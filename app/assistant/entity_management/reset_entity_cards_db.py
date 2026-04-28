"""
Reset Entity Cards Database
Drops and recreates all entity cards tables
"""

from app.assistant.entity_management.entity_cards import reset_entity_cards_db, check_entity_cards_db_exists
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main():
    """Reset the entity cards database"""
    print("Resetting Entity Cards Database...")
    
    try:
        # Check current status
        status = check_entity_cards_db_exists()
        print(f"Current database status:")
        print(f"  Tables exist: {status['exists']}")
        print(f"  Existing tables: {status['existing_tables']}")
        print()
        
        if not status['exists']:
            print("No entity cards tables found. Nothing to reset.")
            return
        
        # Confirm reset
        print("⚠️  This will DELETE ALL entity cards data!")
        response = input("Are you sure you want to continue? (yes/no): ")
        
        if response.lower() not in ['yes', 'y']:
            print("Reset cancelled.")
            return
        
        # Reset database
        reset_entity_cards_db()
        
        # Verify reset
        status_after = check_entity_cards_db_exists()
        if status_after['exists']:
            print("✅ Entity Cards Database reset successfully!")
        else:
            print("❌ Reset failed - tables still missing")
            
    except Exception as e:
        logger.error(f"Error resetting entity cards database: {e}")
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()
