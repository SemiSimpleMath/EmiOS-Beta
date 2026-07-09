import uuid
from sqlalchemy import create_engine, Column, Integer, Text, JSON, TIMESTAMP, func, String, Boolean, Float
from datetime import datetime, timezone
from sqlalchemy.types import TypeDecorator

from app.models.base import Base  # Import Base from base.py
from app.models.base import get_session

import json
from hashlib import sha256

from sqlalchemy import Column, String, JSON, DateTime, func

# Note: UUID type not needed for SQLite (using Text for IDs)


class UTCDateTime(TypeDecorator):
    """
    UTC-enforced datetime type for SQLite-backed SQLAlchemy models.
    """

    impl = TIMESTAMP(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("UTCDateTime requires timezone-aware datetime values on write.")
            return parsed.astimezone(timezone.utc)
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expected datetime or ISO string, got {type(value).__name__}.")
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires timezone-aware datetime values on write.")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        if not isinstance(value, datetime):
            raise TypeError(f"UTCDateTime expected datetime or ISO string from DB, got {type(value).__name__}.")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

# Unified ingestion log model
class UnifiedLog2026(Base):
    __tablename__ = "unified_log_2026"

    id = Column(Text, primary_key=True)
    timestamp = Column(UTCDateTime(), nullable=False)
    role = Column(Text)
    message = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    processed = Column(Boolean, default=False, nullable=False)

    # Routing and scope
    request_id = Column(Text, nullable=True)
    room_id = Column(Text, nullable=True)
    room_surface = Column(Text, nullable=True)
    room_context_id = Column(Text, nullable=True)
    direction = Column(Text, nullable=True)

    # Speaker identity
    speaker_id = Column(Text, nullable=True)
    speaker_name = Column(Text, nullable=True)
    speaker_role = Column(Text, nullable=True)
    speaker_external_id = Column(Text, nullable=True)

    # Transport identity
    transport_message_id = Column(Text, nullable=True)
    transport_from = Column(Text, nullable=True)
    transport_to = Column(Text, nullable=True)

    # Rich content
    content_type = Column(Text, nullable=True)
    media_items_json = Column(JSON, nullable=True)
    link_items_json = Column(JSON, nullable=True)

    # Raw structured payload for lossless replay
    metadata_json = Column(JSON, nullable=True)
    data_json = Column(JSON, nullable=True)

class InfoDatabase(Base):
    __tablename__ = 'info_database'

    id = Column(Text, primary_key=True)
    label = Column(String, nullable=False)
    info = Column(Text, nullable=False)
    timestamp = Column(TIMESTAMP(timezone=True), nullable=True)
    relevance_score = Column(Integer, nullable=True)
    source = Column(String, nullable=True)

class AgentActivityLog(Base):
    __tablename__ = 'agent_activity_log'

    id = Column(Text, primary_key=True, default=lambda: str(uuid.uuid4()))
    timestamp = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now())
    agent_name = Column(String, nullable=True)
    description = Column(String, nullable=False)
    parameters = Column(JSON, nullable=True)
    status = Column(String, nullable=False, default="pending")
    notes = Column(Text, nullable=True)

class RAGDatabase(Base):
    __tablename__ = 'rag_database'

    id = Column(Text, primary_key=True)
    document = Column(Text, nullable=False)
    embedding = Column(JSON, nullable=False)
    source = Column(String, nullable=True)
    scope = Column(String, nullable=False, default='chat')
    timestamp = Column(TIMESTAMP(timezone=True), nullable=False)
    processed = Column(Boolean, default=False, nullable=False)

class EventRepository(Base):
    __tablename__ = 'event_repository'

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String, nullable=False)
    data_type = Column(String, nullable=False)
    data = Column(JSON, nullable=False)
    data_hash = Column(String, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now())

class EmailCheckState(Base):
    __tablename__ = 'email_check_state'

    id = Column(Integer, primary_key=True, default=1)
    last_checked = Column(TIMESTAMP(timezone=True), default=func.now(), nullable=False)

    @staticmethod
    def compute_hash(event_data):
        """Compute a SHA-256 hash of the event data."""
        return sha256(json.dumps(event_data, sort_keys=True).encode()).hexdigest()


class ExtractedFact(Base):
    """
    Stores extracted facts from Switchboard agent.
    These are self-contained chunks of conversation that contain user preferences or feedback.
    """
    __tablename__ = 'extracted_facts'
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    category = Column(String, nullable=False)  # 'preference', 'state'
    summary = Column(Text, nullable=False)  # Extracted summary of the fact
    tags = Column(JSON, nullable=False)  # List of tags for routing (e.g., ['food', 'routine'])
    confidence = Column(Float, nullable=False)  # Confidence score (0.0-1.0)
    source_message_ids = Column(JSON, nullable=False)  # List of message IDs in the window
    window_end_id = Column(String, nullable=False)  # High water mark - last message ID in window
    processed = Column(Boolean, default=False, nullable=False)  # Whether Memory Manager has processed this
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class SwitchboardState(Base):
    """
    Tracks the state of SwitchboardRunner processing.
    Stores the last processed message ID to enable resumable processing.
    """
    __tablename__ = 'switchboard_state'
    
    id = Column(Integer, primary_key=True, default=1)  # Singleton row
    last_processed_message_id = Column(String, nullable=True)  # Last message ID processed
    last_run_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

def initialize_database(force_test_db=False):
    """
    Initialize all defined tables in the database.
    """
    # Use get_session to get the correct engine
    session = get_session(force_test_db=force_test_db)
    engine = session.bind
    
    Base.metadata.create_all(engine)
    session.close()

if __name__ == "__main__":
    # Force use test database
    initialize_database(force_test_db=True)
    print("\nDatabase initialized successfully in test database.")
