# this is the __init__ file for the routes.
from .index_route import index_route_bp
from .main import main_bp
from .chat_bot import chat_bot_bp
from .process_request import process_request_bp
from .process_audio import process_audio_bp
from .idle_route import idle_route_bp
from .render_repo_route import render_repo_route_bp
from .tool_route import tool_route_bp
from .agent_flow import agent_flow_route_bp
from .ask_user_route import ask_user_route_bp
from .daily_summary import daily_summary_route_bp
from .entity_cards_editor import entity_cards_editor_bp
from .entity_cards_admin import entity_cards_admin_bp
from .google_oauth import google_oauth_bp
from .health_check import health_check_bp
from .debug_status import debug_status_bp
from .debug_logging import debug_logging_bp
from .ticket_api import ticket_api_bp
from .twilio_sms import twilio_sms_bp
from .slack_room import slack_room_bp
from .slack_events import slack_events_bp
from .telegram_webhook import telegram_webhook_bp
from .playwright_modal_test import playwright_modal_test_bp
from .runtime_monitor import runtime_monitor_bp
from .task_graph_route import task_graph_bp
from .kg_maintenance import kg_maintenance_bp
from .entity_card_maintenance import entity_card_maintenance_bp
from .smart_home_bridge import smart_home_bridge_bp
from .insights import insights_bp

# KG/Taxonomy/Graph Visualizer routes
try:
    from .kg_visualizer import kg_visualizer_bp
    from .taxonomy_viewer import taxonomy_viewer_bp
except ImportError as e:
    kg_visualizer_bp = None
    taxonomy_viewer_bp = None

# /me — the lens (clean-room rewrite of the visualizer).
from .me import me_bp
