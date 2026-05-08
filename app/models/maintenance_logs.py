"""
Centralized Maintenance Tracking System
Tracks when various maintenance tasks were last run
"""

from datetime import datetime, timezone, timedelta
from sqlalchemy import (
    Column, String, DateTime, Integer, Float, Index, func
)
from app.assistant.utils.time_utils import AwareUtcDateTime

from app.models.base import Base, get_session, get_default_engine


class MaintenanceRunLog(Base):
    """
    Centralized table to track when various maintenance tasks were last run
    """
    __tablename__ = 'maintenance_run_log'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_name = Column(String(100), nullable=False)  # 'description_creation', 'entity_card_generation', etc.
    last_run_time = Column(AwareUtcDateTime, nullable=False)
    nodes_processed = Column(Integer, nullable=False)
    nodes_updated = Column(Integer, nullable=False)
    run_duration_seconds = Column(Float, nullable=True)
    created_at = Column(AwareUtcDateTime, server_default=func.now())
    
    __table_args__ = (
        Index('ix_maintenance_run_log_task_name', 'task_name'),
        Index('ix_maintenance_run_log_last_run_time', 'last_run_time'),
        Index('ix_maintenance_run_log_created_at', 'created_at'),
    )


# Database management functions
def check_maintenance_logs_db_exists():
    """Check if maintenance logs table exists"""
    from sqlalchemy import inspect
    engine = get_default_engine()
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    
    required_tables = ['maintenance_run_log']
    existing_required_tables = [table for table in required_tables if table in existing_tables]
    
    return {
        'exists': len(existing_required_tables) == len(required_tables),
        'existing_tables': existing_required_tables,
        'missing_tables': [table for table in required_tables if table not in existing_tables]
    }


def initialize_maintenance_logs_db():
    """Initialize maintenance logs table."""
    try:
        print("Creating maintenance logs table...")
        
        # Create tables directly
        engine = get_default_engine()
        Base.metadata.create_all(engine, checkfirst=True)
        
        print("Maintenance logs table initialized successfully.")
        
    except Exception as e:
        if "already exists" in str(e) or "DuplicateTable" in str(e):
            print("Maintenance logs table already exists. Skipping creation.")
        else:
            print(f"Error initializing maintenance logs table: {e}")
            raise


def drop_maintenance_logs_db():
    """Drop maintenance logs table."""
    engine = get_default_engine()
    Base.metadata.drop_all(engine, tables=[MaintenanceRunLog.__table__], checkfirst=True)
    print("Maintenance logs table dropped successfully.")


def reset_maintenance_logs_db():
    """Drop and recreate maintenance logs table."""
    print("Resetting maintenance logs database...")
    drop_maintenance_logs_db()
    initialize_maintenance_logs_db()
    print("Maintenance logs database reset completed.")


# Helper functions for maintenance operations
def get_last_maintenance_run_time(session, task_name):
    """
    Get the timestamp of the last run for a specific task
    """
    last_run = session.query(MaintenanceRunLog).filter(
        MaintenanceRunLog.task_name == task_name
    ).order_by(MaintenanceRunLog.last_run_time.desc()).first()
    return last_run.last_run_time if last_run else None


def log_maintenance_run(session, task_name, last_run_time, nodes_processed, nodes_updated, run_duration_seconds=None):
    """
    Log a maintenance run for a specific task
    """
    run_log = MaintenanceRunLog(
        task_name=task_name,
        last_run_time=last_run_time,
        nodes_processed=nodes_processed,
        nodes_updated=nodes_updated,
        run_duration_seconds=run_duration_seconds
    )
    session.add(run_log)
    session.commit()
    return run_log


def get_maintenance_run_history(session=None, task_name=None, limit=10):
    """
    Get recent maintenance run history, optionally filtered by task.

    If no session is provided, one is opened and closed internally.
    If a session is provided, the caller owns its lifecycle.
    """
    owned = session is None
    if owned:
        session = get_session()
    try:
        query = session.query(MaintenanceRunLog)
        if task_name:
            query = query.filter(MaintenanceRunLog.task_name == task_name)

        runs = query.order_by(MaintenanceRunLog.last_run_time.desc()).limit(limit).all()

        return [
            {
                "id": run.id,
                "task_name": run.task_name,
                "last_run_time": run.last_run_time,
                "nodes_processed": run.nodes_processed,
                "nodes_updated": run.nodes_updated,
                "run_duration_seconds": run.run_duration_seconds,
                "created_at": run.created_at,
            }
            for run in runs
        ]
    finally:
        if owned:
            session.close()


def initialize_with_yesterday_runs():
    """
    Initialize the maintenance logs with yesterday's timestamps for common tasks.
    """
    session = get_session()
    try:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)

        tasks = [
            {
                "task_name": "description_creation",
                "nodes_processed": 300,
                "nodes_updated": 300,
                "run_duration_seconds": 1800.0,
            },
            {
                "task_name": "entity_card_generation",
                "nodes_processed": 300,
                "nodes_updated": 250,
                "run_duration_seconds": 1200.0,
            },
        ]

        for task in tasks:
            existing = session.query(MaintenanceRunLog).filter(
                MaintenanceRunLog.task_name == task["task_name"]
            ).first()

            if not existing:
                log_maintenance_run(
                    session=session,
                    task_name=task["task_name"],
                    last_run_time=yesterday,
                    nodes_processed=task["nodes_processed"],
                    nodes_updated=task["nodes_updated"],
                    run_duration_seconds=task["run_duration_seconds"],
                )
                print(f"Initialized {task['task_name']} with yesterday's timestamp")
            else:
                print(f"{task['task_name']} already has run history, skipping initialization")
    finally:
        session.close()


def get_nodes_updated_since(session, since_timestamp):
    """
    Get nodes that have been updated since the given timestamp
    """
    from app.assistant.kg.db.knowledge_graph_db import Node
    return session.query(Node).filter(Node.updated_at > since_timestamp).all()
