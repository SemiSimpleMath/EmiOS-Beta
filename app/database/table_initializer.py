"""
Centralized table initialization by feature group
Each function imports only its related models and creates tables

Table Groups:
- Core: Always created (mandatory)
- Entity Cards: Always created (core feature)
- News: Always created (small overhead)
- Knowledge Graph: Only if enabled (requires ChromaDB)
"""
from app.models.base import Base, get_current_engine
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def initialize_core_tables():
    """
    Create mandatory tables for core functionality
    Always runs on startup
    
    Tables (7):
    - unified_log, agent_activity_log, info_database
    - rag_database, event_repository, email_check_state
    - processed_entity_log
    """
    logger.info("Initializing core tables...")
    
    # Import core models so they register with Base.metadata before create_all
    from app.assistant.database.db_handler import (  # noqa: F401
        UnifiedLog2026, InfoDatabase, AgentActivityLog,
        RAGDatabase, EventRepository, EmailCheckState,
        ExtractedFact, SwitchboardState,
    )
    from app.assistant.database.processed_entity_log import ProcessedEntityLog  # noqa: F401
    from app.assistant.database.kg_node_verdict import KGNodeVerdict  # noqa: F401
    from app.assistant.ticket_manager.ticket import Ticket  # noqa: F401

    engine = get_current_engine()
    Base.metadata.create_all(engine, checkfirst=True)
    logger.info("✅ Core tables initialized")


def initialize_music_tables():
    """
    Create music/DJ tables (core feature)
    Always runs on startup
    
    Tables (6):
    - played_songs
    - music_tracks_spotify
    - music_genre_stats
    - music_genre_weights
    - music_artist_weights
    - music_track_weights
    """
    logger.info("Initializing music tables...")
    
    # NOTE: These models use Flask-SQLAlchemy's metadata (db.Model), not Base.metadata.
    # Create them explicitly to avoid relying on incidental imports.
    from app.assistant.database.db_instance import db
    from app.models.played_songs import PlayedSong
    from app.models.music_tracks_spotify import SpotifyMusicTrack
    from app.models.music_genre_stats import MusicGenreStats
    from app.models.music_weights import MusicGenreWeight, MusicArtistWeight, MusicTrackWeight

    db.Model.metadata.create_all(
        db.engine,
        tables=[
            PlayedSong.__table__,
            SpotifyMusicTrack.__table__,
            MusicGenreStats.__table__,
            MusicGenreWeight.__table__,
            MusicArtistWeight.__table__,
            MusicTrackWeight.__table__,
        ],
        checkfirst=True,
    )
    logger.info("✅ Music tables initialized (6 tables)")


def initialize_entity_card_tables():
    """Create v2 entity card tables.

    Tables (4):
    - entity_card_v2, entity_card_section, entity_card_bullet,
      entity_card_bullet_source_node
    """
    logger.info("Initializing entity card v2 tables...")

    # Importing the module registers all four v2 tables with Base.metadata
    # via their class definitions.
    from app.assistant.entity_management import entity_card_v2  # noqa: F401

    engine = get_current_engine()
    Base.metadata.create_all(engine, checkfirst=True)
    logger.info("✅ Entity card v2 tables initialized (4 tables)")


def initialize_news_tables():
    """
    Create News tables
    Always runs on startup (small overhead)
    
    Tables (5):
    - news_categories, news_labels, news_scores
    - news_articles, news_feedbacks
    """
    logger.info("Initializing news tables...")
    
    try:
        from app.assistant.news.news_schema import (
            NewsCategory, NewsLabel, NewsScore, 
            NewsArticle, NewsFeedback
        )
        
        engine = get_current_engine()
        Base.metadata.create_all(engine, checkfirst=True)
        logger.info("✅ News tables initialized (5 tables)")
    except ImportError as e:
        logger.warning(f"⚠️ News models not found, skipping news tables: {e}")


def initialize_kg_tables():
    """
    Create Knowledge Graph tables (optional feature)
    Only run when KG feature is enabled
    
    ⚠️ REQUIRES: ChromaDB installation
    
    Tables:
    KG Core:
    - kg_node_metadata, kg_edge_metadata, message_source_mapping
    
    Taxonomy:
    - taxonomy, node_taxonomy_links, taxonomy_suggestions
    - taxonomy_suggestions_review, node_taxonomy_review_queue
    """
    logger.info("Initializing Knowledge Graph tables...")
    
    from app.assistant.kg.db.knowledge_graph_db_sqlite import (  # noqa: F401
        Node, Edge, MessageSourceMapping,
    )
    from app.assistant.kg_core.taxonomy.models import (  # noqa: F401
        Taxonomy, NodeTaxonomyLink, TaxonomySuggestion,
        TaxonomySuggestions, NodeTaxonomyReviewQueue,
    )

    engine = get_current_engine()
    Base.metadata.create_all(engine, checkfirst=True)
    logger.info("✅ Knowledge Graph tables initialized")


def initialize_kg_pipeline_tables():
    """
    Create KG Pipeline evidence-trail tables used by the live ingestion path.

    The legacy kg_chat_* staging tables (projection / conversation_window /
    conversation_window_item / parsed_sentence) were retired and migrated
    into the unified kg_window / kg_window_message / kg_resolved_message
    schema. Only the append-only evidence trail tables remain registered
    here — kg_node_evidence and kg_edge_evidence are still actively
    written and queried.
    """
    logger.info("Initializing KG Pipeline tables...")

    from app.assistant.database.kg_chat_projection import (  # noqa: F401
        KGNodeEvidence, KGEdgeEvidence,
    )

    engine = get_current_engine()
    Base.metadata.create_all(engine, checkfirst=True)
    logger.info("✅ KG Pipeline tables initialized")


def initialize_always_on_tables():
    """
    Initialize tables that are always created on startup
    
    Total: 20 tables
    - Core: 9 tables
    - Entity Cards: 5 tables
    - News: 5 tables
    - Music: 1 table
    """
    logger.info("🚀 Initializing always-on database tables...")
    
    initialize_core_tables()
    initialize_entity_card_tables()
    initialize_news_tables()
    initialize_music_tables()
    
    logger.info("✅ Always-on tables ready (20 tables)")


def initialize_optional_tables():
    """
    Initialize tables for optional features.

    KG tables are always created regardless of the feature flag so that code
    paths which reference them never hit "no such table" errors.  The feature
    flag gates the *pipeline execution*, not table existence.
    """
    logger.info("Checking optional feature tables...")

    try:
        initialize_kg_tables()
        initialize_kg_pipeline_tables()
        _seed_kg_core_nodes()
    except ImportError as e:
        logger.error("Failed to initialize KG tables (ChromaDB not installed?): %s", e)
        logger.info("To enable KG: pip install chromadb")


def _seed_kg_core_nodes():
    """Seed the KG with user + assistant Entity nodes and a connecting State node.

    Only runs when kg_node_metadata is completely empty so it never touches
    an existing graph.  Mirrors the Entity - State - Entity pattern used by
    the rest of the KG pipeline.
    Skips if setup hasn't completed (no user/assistant identity to seed with).
    """
    from app.assistant.utils.path_utils import setup_complete
    if not setup_complete():
        return
    import json
    import uuid
    from datetime import datetime, timezone

    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
    from app.models.base import get_session

    session = get_session()
    try:
        if session.query(Node).first() is not None:
            return

        from app.assistant.utils.path_utils import get_resources_dir
        from app.assistant.utils.assistant_name import get_assistant_name

        user_data_file = get_resources_dir() / "user" / "resource_user_data.json"
        if user_data_file.exists():
            data = json.loads(user_data_file.read_text(encoding="utf-8"))
            user_label = (
                data.get("preferred_name")
                or data.get("first_name")
                or data.get("full_name")
                or "User"
            )
            full_name = data.get("full_name") or user_label
            job = data.get("job", "")
        else:
            import os
            user_label = os.environ.get("PRIMARY_USER", "User")
            full_name = user_label
            job = ""

        assistant_label = get_assistant_name()
        now = datetime.now(timezone.utc)

        user_node = Node(
            id=str(uuid.uuid4()),
            label=user_label,
            node_type="Entity",
            description=f"{user_label} is the primary user.",
            aliases=list({user_label, full_name, "user"}),
            category="person",
            semantic_label=user_label,
            hash_tags=["user", "person", "self"],
            attributes={"job": job} if job else {},
            confidence=1.0,
            importance=1.0,
            source="seed",
            confidence_tier="axiom",
            locked_by_user_at=now,
            created_at=now,
            updated_at=now,
        )

        assistant_node = Node(
            id=str(uuid.uuid4()),
            label=assistant_label,
            node_type="Entity",
            description=f"{user_label}'s AI assistant.",
            aliases=[assistant_label, f"{assistant_label} AI", "assistant", "AI Assistant"],
            category="AI",
            semantic_label=assistant_label,
            hash_tags=["assistant", "ai", "ai_assistant"],
            attributes={},
            confidence=1.0,
            importance=1.0,
            source="seed",
            confidence_tier="axiom",
            locked_by_user_at=now,
            created_at=now,
            updated_at=now,
        )

        state_node = Node(
            id=str(uuid.uuid4()),
            label=f"{assistant_label} Assists {user_label}",
            node_type="State",
            description=(
                f"{assistant_label} serves as {user_label}'s AI assistant, "
                "helping with tasks, information, and conversation."
            ),
            category="relationship",
            semantic_label="Assistant Relationship",
            hash_tags=["assistant_relationship", "ai_human_interaction", "ongoing"],
            start_date=now,
            start_date_confidence="confirmed",
            attributes={"status": "active"},
            confidence=1.0,
            importance=1.0,
            source="seed",
            confidence_tier="axiom",
            locked_by_user_at=now,
            created_at=now,
            updated_at=now,
        )

        session.add_all([user_node, assistant_node, state_node])
        session.flush()

        user_edge = Edge(
            id=str(uuid.uuid4()),
            source_id=user_node.id,
            target_id=state_node.id,
            relationship_type="Participant",
            sentence=f"{user_label} is the user being assisted in this relationship.",
            attributes={"role": "user", "role_type": "recipient"},
            confidence=1.0,
            importance=1.0,
            source="seed",
            confidence_tier="axiom",
            created_at=now,
            updated_at=now,
        )

        assistant_edge = Edge(
            id=str(uuid.uuid4()),
            source_id=assistant_node.id,
            target_id=state_node.id,
            relationship_type="Participant",
            sentence=f"{assistant_label} is the AI assistant in this relationship.",
            attributes={"role": "assistant", "role_type": "provider"},
            confidence=1.0,
            importance=1.0,
            source="seed",
            confidence_tier="axiom",
            created_at=now,
            updated_at=now,
        )

        session.add_all([user_edge, assistant_edge])
        session.commit()
        logger.info(
            "Seeded KG core nodes: %s (Entity), %s (Entity), %s (State), 2 edges",
            user_label, assistant_label, state_node.label,
        )
    except Exception as e:
        session.rollback()
        logger.error("Failed to seed KG core nodes: %s", e, exc_info=True)
    finally:
        session.close()


def initialize_all_tables():
    """
    Initialize ALL tables
    - Always-on tables (19)
    - Optional tables based on settings (0-13)
    
    This is the main entry point called from create_app()
    """
    logger.info("=" * 60)
    logger.info("DATABASE INITIALIZATION")
    logger.info("=" * 60)
    
    # Always create these
    initialize_always_on_tables()
    
    # Optional based on settings
    initialize_optional_tables()
    
    logger.info("=" * 60)
    logger.info("✅ Database initialization complete!")
    logger.info("=" * 60)


def check_missing_tables():
    """
    Diagnostic: Check which tables exist vs which should exist
    
    Returns:
        dict: {
            'existing': list of existing table names,
            'expected': list of expected table names,
            'missing': list of missing table names
        }
    """
    from sqlalchemy import inspect
    engine = get_current_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    
    # Import all models to get expected tables
    initialize_always_on_tables()  # This registers always-on models with Base
    
    expected_tables = set(Base.metadata.tables.keys())
    missing_tables = expected_tables - existing_tables
    extra_tables = existing_tables - expected_tables
    
    return {
        'existing': sorted(list(existing_tables)),
        'expected': sorted(list(expected_tables)),
        'missing': sorted(list(missing_tables)),
        'extra': sorted(list(extra_tables))
    }

