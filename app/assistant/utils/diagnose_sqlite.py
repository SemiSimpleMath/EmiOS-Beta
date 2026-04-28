"""
SQLite Database Diagnostic Script

Run this to verify WAL mode and busy_timeout are properly configured.

There are TWO database access patterns in this app:
1. Plain SQLAlchemy (get_session from app.models.base) - used by most components
2. Flask-SQLAlchemy (db.session from db_instance) - used by scheduler

Both should have WAL mode and busy_timeout=30000ms configured.
"""

def diagnose_sqlite():
    """Check SQLite configuration and connection state."""
    from app.models.base import get_session
    
    print("=" * 60)
    print("SQLite Database Diagnostics")
    print("=" * 60)
    
    # Check Plain SQLAlchemy (get_session)
    print("\n>>> Checking Plain SQLAlchemy (get_session) <<<")
    session = get_session()
    
    try:
        # Check journal mode
        result = session.execute("PRAGMA journal_mode").fetchone()
        journal_mode = result[0] if result else "UNKNOWN"
        print(f"\n1. Journal Mode: {journal_mode}")
        if journal_mode.lower() != "wal":
            print("   ❌ WARNING: Not in WAL mode! Concurrent reads may block writes.")
        else:
            print("   ✅ WAL mode is enabled (good for concurrency)")
        
        # Check busy_timeout
        result = session.execute("PRAGMA busy_timeout").fetchone()
        busy_timeout = result[0] if result else 0
        print(f"\n2. Busy Timeout: {busy_timeout}ms")
        if busy_timeout < 1000:
            print("   ❌ WARNING: Busy timeout is too low! Should be at least 30000ms.")
            print("   This explains why 'database is locked' errors occur immediately!")
        else:
            print(f"   ✅ Will wait up to {busy_timeout/1000:.1f} seconds for locks")
        
        # Check synchronous mode
        result = session.execute("PRAGMA synchronous").fetchone()
        sync_mode = result[0] if result else "UNKNOWN"
        sync_names = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}
        print(f"\n3. Synchronous Mode: {sync_names.get(sync_mode, sync_mode)}")
        
        # Check WAL checkpoint
        result = session.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if result:
            blocked, log_size, checkpointed = result
            print(f"\n4. WAL Checkpoint Status:")
            print(f"   - Log size: {log_size} pages")
            print(f"   - Checkpointed: {checkpointed} pages")
            if blocked:
                print("   ⚠️ Checkpoint was blocked (active transactions)")
        
        # Check for open connections/transactions
        # Note: SQLite doesn't have a direct way to check this
        print(f"\n5. Database File Check:")
        import os
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), 'emi.db')
        
        if os.path.exists(db_path):
            print(f"   - Main DB: {db_path} ({os.path.getsize(db_path) / 1024 / 1024:.2f} MB)")
        
        wal_path = db_path + "-wal"
        if os.path.exists(wal_path):
            wal_size = os.path.getsize(wal_path)
            print(f"   - WAL file: {wal_path} ({wal_size / 1024:.2f} KB)")
            if wal_size > 10 * 1024 * 1024:  # > 10MB
                print("   ⚠️ WAL file is large. Consider running PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            print("   ❌ No WAL file found - WAL mode may not be active!")
        
        shm_path = db_path + "-shm"
        if os.path.exists(shm_path):
            print(f"   - SHM file: {shm_path} (exists)")
        else:
            print("   ❌ No SHM file found - WAL mode may not be active!")
        
        print(f"\n" + "=" * 60)
        
        # Test write
        print("\n6. Testing write operation...")
        from sqlalchemy import text
        try:
            session.execute(text("CREATE TABLE IF NOT EXISTS _diag_test (id INTEGER PRIMARY KEY)"))
            session.execute(text("INSERT OR REPLACE INTO _diag_test (id) VALUES (1)"))
            session.commit()
            print("   ✅ Write test passed")
            
            # Cleanup
            session.execute(text("DROP TABLE IF EXISTS _diag_test"))
            session.commit()
        except Exception as e:
            print(f"   ❌ Write test failed: {e}")
            session.rollback()
        
        print("\n" + "=" * 60)
        
        if busy_timeout < 1000:
            print("\n⚠️  CRITICAL: The busy_timeout is not set properly!")
            print("   This is likely why you're seeing immediate 'database is locked' errors.")
            print("   The fix in base.py should resolve this on the next application restart.")
        
        plain_sqlalchemy_result = {
            "journal_mode": journal_mode,
            "busy_timeout": busy_timeout,
            "wal_file_exists": os.path.exists(wal_path) if 'wal_path' in dir() else False
        }
        
    finally:
        session.close()
    
    # Check Flask-SQLAlchemy (db.session) - used by scheduler
    print("\n" + "=" * 60)
    print("\n>>> Checking Flask-SQLAlchemy (db.session - scheduler) <<<")
    
    flask_result = {"available": False}
    try:
        from flask import current_app
        from app.assistant.database.db_instance import db
        
        # This will only work if we're in a Flask app context
        flask_session = db.session
        
        result = flask_session.execute(db.text("PRAGMA journal_mode")).fetchone()
        flask_journal_mode = result[0] if result else "UNKNOWN"
        print(f"\n1. Journal Mode: {flask_journal_mode}")
        if flask_journal_mode.lower() != "wal":
            print("   ❌ WARNING: Flask-SQLAlchemy NOT in WAL mode!")
        else:
            print("   ✅ WAL mode is enabled")
        
        result = flask_session.execute(db.text("PRAGMA busy_timeout")).fetchone()
        flask_busy_timeout = result[0] if result else 0
        print(f"\n2. Busy Timeout: {flask_busy_timeout}ms")
        if flask_busy_timeout < 1000:
            print("   ❌ WARNING: Flask-SQLAlchemy busy_timeout is too low!")
            print("   This means SCHEDULER operations will fail immediately on lock!")
        else:
            print(f"   ✅ Will wait up to {flask_busy_timeout/1000:.1f} seconds for locks")
        
        flask_result = {
            "available": True,
            "journal_mode": flask_journal_mode,
            "busy_timeout": flask_busy_timeout
        }
        
    except RuntimeError as e:
        if "application context" in str(e).lower():
            print("\n⚠️  Cannot check Flask-SQLAlchemy outside of Flask app context.")
            print("   Run this diagnostic from within the Flask application.")
        else:
            print(f"\n❌ Error checking Flask-SQLAlchemy: {e}")
    except Exception as e:
        print(f"\n❌ Error checking Flask-SQLAlchemy: {e}")
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    all_good = True
    
    if plain_sqlalchemy_result.get("busy_timeout", 0) < 1000:
        print("❌ Plain SQLAlchemy: busy_timeout NOT configured!")
        all_good = False
    else:
        print("✅ Plain SQLAlchemy: busy_timeout = 30000ms")
    
    if flask_result.get("available"):
        if flask_result.get("busy_timeout", 0) < 1000:
            print("❌ Flask-SQLAlchemy (scheduler): busy_timeout NOT configured!")
            all_good = False
        else:
            print("✅ Flask-SQLAlchemy (scheduler): busy_timeout = 30000ms")
    else:
        print("⚠️  Flask-SQLAlchemy: Could not check (not in app context)")
    
    if all_good:
        print("\n✅ All database connections properly configured!")
    else:
        print("\n❌ Some connections missing busy_timeout - restart the app to apply fixes")
    
    return plain_sqlalchemy_result
        

if __name__ == "__main__":
    diagnose_sqlite()

