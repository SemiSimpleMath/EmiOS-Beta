# base.py
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
import threading

# Module-level cache for engines and sessionmakers (singleton pattern)
# Keyed by database URI to support both test and dev databases
_engines = {}
_sessionmakers = {}
_engines_lock = threading.Lock()
_pragma_listener_registered = False


def _set_sqlite_pragma(dbapi_conn, connection_record):
    """
    Set SQLite pragmas for ALL connections.
    This is registered as a module-level listener to ensure it applies to every connection.
    """
    cursor = dbapi_conn.cursor()
    # Ensure SQLite enforces FK constraints (required for ondelete='CASCADE' to work).
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
    cursor.execute("PRAGMA synchronous=NORMAL")  # Balance between safety and performance
    cursor.close()

# Database setup with SQLite
def get_database_uri():
    """Get database URI - using SQLite for this version"""
    # Allow env override (used by some maintenance scripts and tests)
    if os.environ.get('USE_TEST_DB') == 'true':
        explicit = os.environ.get('TEST_DATABASE_URI_EMI')
        # This repo is the SQLite variant; only honor explicit URIs if they are SQLite.
        if explicit and str(explicit).startswith("sqlite://"):
            return explicit
        test_db_name = os.environ.get('TEST_DB_NAME', 'test_emidb')
        filename = test_db_name if test_db_name.endswith('.db') else f"{test_db_name}.db"
    else:
        explicit = os.environ.get('DEV_DATABASE_URI_EMI')
        # This repo is the SQLite variant; only honor explicit URIs if they are SQLite.
        if explicit and str(explicit).startswith("sqlite://"):
            return explicit
        filename = 'emi.db'

    # SQLite db lives under the writable data dir (EMI_DATA_DIR), which defaults to
    # the repo root in dev — so this is byte-for-byte the legacy project-root path
    # when EMI_DATA_DIR is unset. Local import keeps this low-level module cycle-free.
    from app.assistant.utils.path_utils import get_data_dir
    # Convert to forward slashes for SQLite URI (required on all platforms)
    db_path = str(get_data_dir() / filename).replace('\\', '/')
    return f'sqlite:///{db_path}'

def get_session(force_test_db=False):
    """
    Single source of truth for database sessions.
    Works for both Flask and standalone components.
    Always uses the correct database based on USE_TEST_DB environment variable.
    
    Uses a singleton engine pattern to reuse connection pools and prevent
    connection exhaustion.
    
    Thread Safety:
    - The engine is thread-safe and shared across all threads
    - The connection pool (QueuePool) is thread-safe
    - Each call creates a NEW session - sessions are NOT thread-safe
    - Each thread must use its own session instance
    
    Args:
        force_test_db (bool): If True, force use test database regardless of environment variable
    """
    global _pragma_listener_registered
    
    # Force test database if requested — use local override, not env mutation.
    if force_test_db:
        _saved_use_test = os.environ.get('USE_TEST_DB')
        _saved_db_name = os.environ.get('TEST_DB_NAME')
        os.environ['USE_TEST_DB'] = 'true'
        os.environ.setdefault('TEST_DB_NAME', 'test_emidb')

    database_uri = get_database_uri()

    if force_test_db:
        # Restore env to prevent contaminating production DB routing.
        if _saved_use_test is None:
            os.environ.pop('USE_TEST_DB', None)
        else:
            os.environ['USE_TEST_DB'] = _saved_use_test
        if _saved_db_name is None:
            os.environ.pop('TEST_DB_NAME', None)
        else:
            os.environ['TEST_DB_NAME'] = _saved_db_name
    
    # Get or create engine for this database URI (singleton pattern)
    # Thread-safe: lock protects dictionary access during engine creation
    with _engines_lock:
        # Register the pragma listener ONCE at module level for ALL engines
        # This ensures pragmas are set for every new connection
        if not _pragma_listener_registered:
            event.listen(Engine, "connect", _set_sqlite_pragma)
            _pragma_listener_registered = True
        
        if database_uri not in _engines:
            # Create engine with connection pooling
            # pool_size: number of connections to maintain
            # max_overflow: additional connections that can be created on demand
            # pool_recycle: close connections after this many seconds (prevent stale connections)
            _engines[database_uri] = create_engine(
                database_uri,
                echo=False,
                pool_size=10,           # Maintain 10 connections in pool
                max_overflow=20,       # Allow up to 20 additional connections
                pool_recycle=3600,     # Recycle connections after 1 hour
                pool_pre_ping=True,    # Verify connections before using
                connect_args={
                    'timeout': 30,     # Wait up to 30 seconds for lock to release
                    'check_same_thread': False  # Allow connections across threads
                }
            )
            _sessionmakers[database_uri] = sessionmaker(bind=_engines[database_uri])
        
        # Get the sessionmaker (thread-safe to call from multiple threads)
        session_maker = _sessionmakers[database_uri]
    
    # Create a new session for this thread (sessions are NOT thread-safe)
    # Each thread gets its own session instance
    return session_maker()

def get_current_engine():
    """Get the current engine based on environment configuration"""
    global _pragma_listener_registered
    
    database_uri = get_database_uri()
    with _engines_lock:
        # Register the pragma listener ONCE at module level for ALL engines
        if not _pragma_listener_registered:
            event.listen(Engine, "connect", _set_sqlite_pragma)
            _pragma_listener_registered = True
        
        if database_uri not in _engines:
            _engines[database_uri] = create_engine(
                database_uri,
                echo=False,
                pool_size=10,
                max_overflow=20,
                pool_recycle=3600,
                pool_pre_ping=True,
                connect_args={
                    'timeout': 30,
                    'check_same_thread': False
                }
            )
            
            # IMPORTANT: Also create sessionmaker so get_session() works later
            _sessionmakers[database_uri] = sessionmaker(bind=_engines[database_uri])
        return _engines[database_uri]

# Base declarative base (this is safe to create at import time)
Base = declarative_base()

def get_default_engine():
    """Get the default engine based on current environment configuration"""
    return get_current_engine()  # Reuse the singleton pattern
