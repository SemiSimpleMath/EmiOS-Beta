# app/__init__.py
from flask import Flask, send_from_directory, abort
from flask_socketio import SocketIO
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from sqlalchemy import event
from app.assistant.database.db_instance import db
from app.bootstrap import initialize_services
from pathlib import Path

from app.assistant.utils.logging_config import get_logger
from app.socket_handlers import register_socket_handlers

logger = get_logger(__name__)

# Initialize SocketIO with threading
socketio = SocketIO(async_mode='threading')


def _set_sqlite_pragma_flask(dbapi_conn, connection_record):
    """
    Set SQLite pragmas for Flask-SQLAlchemy connections.
    This ensures the scheduler and other Flask-SQLAlchemy users have the same
    WAL mode and busy_timeout settings as the rest of the application.
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def create_app(config_class="config.DevelopmentConfig"):
    import time as _time
    app = Flask(__name__)
    app.config.from_object(config_class)
    CORS(app, supports_credentials=True)
    JWTManager(app)

    # Inject a per-process static asset version so browsers always fetch fresh
    # JS/CSS after a server restart — avoids stale-cache bugs during development.
    _static_version = str(int(_time.time()))

    @app.context_processor
    def _inject_static_version():
        return {"static_v": _static_version}

    # No login required for this version
    # ping_interval/ping_timeout: even in threading mode these narrow the
    # window where Cloudflare-tunnel-style silent drops go undetected. The
    # real defense is the client heartbeat + server sweeper, set up below.
    socketio.init_app(
        app,
        cors_allowed_origins="*",
        async_mode='threading',
        ping_interval=20,
        ping_timeout=10,
    )
    db.init_app(app)
    
    # CRITICAL: Add PRAGMA settings to Flask-SQLAlchemy engine
    # Without this, scheduler connections don't have WAL mode or busy_timeout!
    with app.app_context():
        engine = db.engine
        event.listen(engine, "connect", _set_sqlite_pragma_flask)
        logger.info("✅ SQLite PRAGMA settings applied to Flask-SQLAlchemy engine")

    with app.app_context():
        # Initialize database tables by feature group
        from app.database.table_initializer import initialize_all_tables
        initialize_all_tables()
        
        # Initialize services
        DI = initialize_services(app)

    app.DI = DI
    DI.socket_io = socketio

    register_socket_handlers(socketio)

    # Initialize event bus (registers DI.socket_manager among other services)
    from app.assistant.initialize_system import initialize_system

    initialize_system()

    # Start the stale-socket sweeper. Evicts bindings whose last_seen is older
    # than 90s — catches Cloudflare-tunnel-style zombie sockets that never fire
    # a Socket.IO disconnect event. Paired with the client-side heartbeat.
    # MUST run after initialize_system() since that's where socket_manager is
    # registered in the DI container.
    try:
        DI.socket_manager.start_stale_sweeper(interval_s=60.0, max_age_s=90.0)
    except Exception as e:
        logger.error("Failed to start socket_manager stale-socket sweeper: %s", e, exc_info=True)


    # Import and register blueprints
    from .routes import (
        index_route_bp,
        main_bp,
        chat_bot_bp,
        process_request_bp,
        process_audio_bp,
        idle_route_bp,
        render_repo_route_bp,
        tool_route_bp,
        agent_flow_route_bp,
        ask_user_route_bp,
        daily_summary_route_bp,
        entity_cards_editor_bp,
        kg_visualizer_bp,
        taxonomy_viewer_bp,
        google_oauth_bp,
        health_check_bp,
        debug_status_bp,
        debug_logging_bp,
        ticket_api_bp,
        twilio_sms_bp,
        slack_room_bp,
        slack_events_bp,
        telegram_webhook_bp,
        playwright_modal_test_bp,
        runtime_monitor_bp,
        smart_home_bridge_bp,
        insights_bp,
    )
    
    # Import user settings route
    from .routes.user_settings import user_settings_bp
    
    # Import setup wizard route
    from .routes.setup import setup_bp
    
    # Import preferences route
    from .routes.preferences import preferences_bp
    
    # Import graph visualizer API (only if chromadb available - disabled in alpha)
    try:
        from .graph_visualizer.api import graph_api
    except ImportError:
        graph_api = None
    
    app.register_blueprint(index_route_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(chat_bot_bp)
    app.register_blueprint(process_request_bp)
    app.register_blueprint(process_audio_bp)
    app.register_blueprint(idle_route_bp)
    app.register_blueprint(render_repo_route_bp)
    app.register_blueprint(tool_route_bp)
    app.register_blueprint(agent_flow_route_bp)
    app.register_blueprint(ask_user_route_bp)
    app.register_blueprint(daily_summary_route_bp)
    app.register_blueprint(entity_cards_editor_bp)
    from app.routes.entity_cards_admin import entity_cards_admin_bp
    app.register_blueprint(entity_cards_admin_bp)
    app.register_blueprint(health_check_bp)
    app.register_blueprint(debug_status_bp)
    app.register_blueprint(debug_logging_bp)
    from app.routes.agent_prompt_debug import agent_prompt_debug_bp
    app.register_blueprint(agent_prompt_debug_bp)
    from app.routes.agent_workbench import agent_workbench_bp
    app.register_blueprint(agent_workbench_bp)
    app.register_blueprint(ticket_api_bp)
    app.register_blueprint(twilio_sms_bp)
    app.register_blueprint(slack_room_bp)
    app.register_blueprint(slack_events_bp)
    app.register_blueprint(telegram_webhook_bp)
    app.register_blueprint(playwright_modal_test_bp)
    app.register_blueprint(runtime_monitor_bp)
    from app.routes.routines_admin import routines_admin_bp
    app.register_blueprint(routines_admin_bp)
    from app.routes.dev_wiki import dev_wiki_bp
    app.register_blueprint(dev_wiki_bp)
    app.register_blueprint(smart_home_bridge_bp)
    app.register_blueprint(insights_bp)
    from app.routes.task_graph_route import task_graph_bp
    app.register_blueprint(task_graph_bp)
    from app.routes.doc_route import doc_bp
    app.register_blueprint(doc_bp)
    from app.routes.kg_maintenance import kg_maintenance_bp
    app.register_blueprint(kg_maintenance_bp)
    from app.routes.kg_proposals import kg_proposals_bp
    app.register_blueprint(kg_proposals_bp)
    from app.routes.kg_dev import kg_dev_bp
    app.register_blueprint(kg_dev_bp)
    from app.routes.kg_node_viewer import kg_node_viewer_bp
    app.register_blueprint(kg_node_viewer_bp)
    from app.routes.kg_interests import kg_interests_bp
    app.register_blueprint(kg_interests_bp)
    from app.routes.wiki_viewer import wiki_viewer_bp
    app.register_blueprint(wiki_viewer_bp)
    from app.routes.entity_card_maintenance import entity_card_maintenance_bp
    app.register_blueprint(entity_card_maintenance_bp)
    
    # Wellness management
    from app.routes.wellness_mgmt import wellness_mgmt_bp
    app.register_blueprint(wellness_mgmt_bp)
    
    # Music player
    from app.routes.music import music_bp
    app.register_blueprint(music_bp)

    # Agent progress (separate tab, separate socket client)
    from app.routes.progress import progress_bp
    app.register_blueprint(progress_bp)
    
    # KG/Taxonomy/Graph Visualizer routes
    if kg_visualizer_bp is not None:
        app.register_blueprint(kg_visualizer_bp)
    if taxonomy_viewer_bp is not None:
        app.register_blueprint(taxonomy_viewer_bp)
    if graph_api is not None:
        app.register_blueprint(graph_api)
    
    app.register_blueprint(google_oauth_bp)
    app.register_blueprint(user_settings_bp)
    app.register_blueprint(setup_bp)
    app.register_blueprint(preferences_bp)
    from app.routes.subsystem_route import subsystem_route_bp
    app.register_blueprint(subsystem_route_bp)

    from app.routes.agent_directives import agent_directives_bp
    app.register_blueprint(agent_directives_bp)

    # Apply saved logging controls after services are initialized (console threshold + per-logger overrides)
    try:
        from app.assistant.utils.logging_config import apply_saved_logging_controls_from_user_settings
        apply_saved_logging_controls_from_user_settings()
    except Exception:
        logger.debug("Could not apply saved logging controls from user settings", exc_info=True)

    @app.errorhandler(413)
    def request_entity_too_large(e):
        from flask import jsonify
        return jsonify({"error": "Recording is too large. Please keep voice messages under 3 minutes."}), 413

    # Serve files from uploads/temp/ (e.g. GeoGuessr screenshots, MCP tool images)
    _uploads_temp_dir = str((Path(__file__).resolve().parent.parent / "uploads" / "temp").resolve())

    @app.route("/uploads/temp/<path:filename>")
    def serve_uploads_temp(filename):
        # Guard against path traversal
        requested = str((Path(_uploads_temp_dir) / filename).resolve())
        if not requested.startswith(_uploads_temp_dir):
            abort(403)
        return send_from_directory(_uploads_temp_dir, filename)

    return app, socketio