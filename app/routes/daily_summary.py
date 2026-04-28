from flask import Blueprint, request, jsonify, current_app
from app.services.daily_summary_renderer import generate_daily_summary_page, convert_daily_summary_result_to_html_data
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.multi_agent_manager_factory.MultiAgentManagerFactory import MultiAgentManagerFactory
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.utils.pydantic_classes import Message
from app.assistant.maintenance_manager.daily_summary_storage import DailySummaryStorage

daily_summary_route_bp = Blueprint('daily_summary', __name__)

@daily_summary_route_bp.route('/daily_summary', methods=['GET'])
def daily_summary_page():
    """
    Display the daily summary page.
    """
    try:
        # Get date parameter or use today
        date_str = request.args.get('date')
        
        # Try to get stored daily summary
        storage = DailySummaryStorage()
        stored_data = storage.get_daily_summary(date_str)
        
        # If requesting today (no date param) and no summary exists, show "not generated"
        # Don't fall back to old summaries - that's confusing
        if stored_data:
            # Use stored data (pass the entire structure, conversion function will extract summary)
            html_data = convert_daily_summary_result_to_html_data(stored_data)
            html_content = generate_daily_summary_page(html_data)
            return html_content
        else:
            # No summaries available at all - show informative message
            from datetime import datetime
            from zoneinfo import ZoneInfo
            
            now = datetime.now(ZoneInfo('America/Los_Angeles'))
            no_data_message = {
                'header': {
                    'date_str': now.strftime('%B %d, %Y'),
                    'day_of_week': now.strftime('%A')
                },
                'narrative': "No daily summary is available yet. The daily summary is generated automatically during your configured quiet hours, or you can trigger one manually.",
                'schedule': [],
                'free_time_windows': [],
                'upcoming_events': [],
                'task_plan': {
                    'load_recommendation': 'unknown',
                    'load_rationale': 'No summary data available.',
                    'tasks': []
                },
                'email_triage': {'urgent': [], 'time_sensitive': [], 'fyi': [], 'ignore': []},
                'metrics': {
                    'workload_score': 0,
                    'total_free_minutes': 0,
                    'total_task_minutes': 0,
                    'buffers_added_minutes': 0
                },
                'assumptions': ['No daily summary has been generated yet.'],
                'conflicts': []
            }
            
            html_content = generate_daily_summary_page(no_data_message)
            return html_content
        
    except Exception as e:
        current_app.logger.error(f"Error generating daily summary page: {e}")
        return jsonify({'error': 'Could not generate daily summary page'}), 500

@daily_summary_route_bp.route('/daily_summary/generate', methods=['POST'])
def generate_daily_summary():
    """
    Generate a new daily summary using the daily_summary_manager.
    """
    try:
        # Create the daily summary manager
        factory = MultiAgentManagerFactory()
        manager = factory.create_manager('daily_summary_manager')
        
        # Create the initial message
        message = Message(
            data_type="agent_activation",
            sender="Flask App",
            receiver="daily_summary_manager",
            content="Generate daily summary",
            task="Generate comprehensive daily summary including calendar events, todo tasks, and scheduling recommendations.",
            scope_context=build_system_scope_context(
                owner_id="flask_app",
                actor_id="daily_summary_route",
                surface="api",
            ),
        )
        
        # Run the manager through canonical scoped path
        result = DI.manager_invoker.invoke(manager, message)
        
        # Convert the result to HTML format
        html_data = convert_daily_summary_result_to_html_data(result)
        html_content = generate_daily_summary_page(html_data)
        
        return html_content
        
    except Exception as e:
        current_app.logger.error(f"Error generating daily summary: {e}")
        return jsonify({'error': 'Could not generate daily summary'}), 500

@daily_summary_route_bp.route('/daily_summary/api', methods=['POST'])
def daily_summary_api():
    """
    API endpoint to get daily summary data as JSON.
    """
    try:
        # Create the daily summary manager
        factory = MultiAgentManagerFactory()
        manager = factory.create_manager('daily_summary_manager')
        
        # Create the initial message
        message = Message(
            data_type="agent_activation",
            sender="Flask App",
            receiver="daily_summary_manager",
            content="Generate daily summary",
            task="Generate comprehensive daily summary including calendar events, todo tasks, and scheduling recommendations.",
            scope_context=build_system_scope_context(
                owner_id="flask_app",
                actor_id="daily_summary_route",
                surface="api",
            ),
        )
        
        # Run the manager through canonical scoped path
        result = DI.manager_invoker.invoke(manager, message)
        
        # Convert the result to HTML format
        html_data = convert_daily_summary_result_to_html_data(result)
        
        return jsonify(html_data), 200
        
    except Exception as e:
        current_app.logger.error(f"Error generating daily summary API: {e}")
        return jsonify({'error': 'Could not generate daily summary'}), 500

@daily_summary_route_bp.route('/daily_summary/trigger', methods=['POST'])
def trigger_daily_summary():
    """
    Manually trigger the daily summary generation (for testing).
    """
    try:
        from app.assistant.maintenance_manager.daily_summary_scheduler import DailySummaryScheduler
        
        # Create scheduler and run daily summary
        scheduler = DailySummaryScheduler()
        result = scheduler.run_daily_summary()
        
        if result.get("success"):
            return jsonify({
                "success": True,
                "message": "Daily summary generated successfully",
                "timestamp": result.get("timestamp"),
                "result": result.get("result")
            }), 200
        else:
            return jsonify({
                "success": False,
                "message": "Daily summary generation failed",
                "error": result.get("error")
            }), 500
        
    except Exception as e:
        current_app.logger.error(f"Error triggering daily summary: {e}")
        return jsonify({'error': 'Could not trigger daily summary'}), 500

@daily_summary_route_bp.route('/daily_summary/stored', methods=['GET'])
def get_stored_summaries():
    """
    Get list of available stored daily summaries.
    """
    try:
        storage = DailySummaryStorage()
        available_dates = storage.list_available_summaries()
        stats = storage.get_summary_stats()
        
        return jsonify({
            "available_dates": available_dates,
            "stats": stats
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Error getting stored summaries: {e}")
        return jsonify({'error': 'Could not get stored summaries'}), 500

@daily_summary_route_bp.route('/daily_summary/stored/<date_str>', methods=['GET'])
def get_stored_summary(date_str):
    """
    Get a specific stored daily summary by date.
    """
    try:
        storage = DailySummaryStorage()
        stored_data = storage.get_daily_summary(date_str)
        
        if stored_data:
            return jsonify(stored_data), 200
        else:
            return jsonify({'error': f'No daily summary found for date: {date_str}'}), 404
        
    except Exception as e:
        current_app.logger.error(f"Error getting stored summary for {date_str}: {e}")
        return jsonify({'error': 'Could not get stored summary'}), 500

@daily_summary_route_bp.route('/daily_summary/stored/<date_str>', methods=['DELETE'])
def delete_stored_summary(date_str):
    """
    Delete a stored daily summary by date.
    """
    try:
        storage = DailySummaryStorage()
        success = storage.delete_daily_summary(date_str)
        
        if success:
            return jsonify({'message': f'Daily summary for {date_str} deleted successfully'}), 200
        else:
            return jsonify({'error': f'No daily summary found for date: {date_str}'}), 404
        
    except Exception as e:
        current_app.logger.error(f"Error deleting stored summary for {date_str}: {e}")
        return jsonify({'error': 'Could not delete stored summary'}), 500

@daily_summary_route_bp.route('/daily_summary/latest', methods=['GET'])
def get_latest_summary():
    """
    Get the most recent daily summary.
    """
    try:
        storage = DailySummaryStorage()
        latest_data = storage.get_latest_daily_summary()
        
        if latest_data:
            return jsonify(latest_data), 200
        else:
            return jsonify({'error': 'No daily summaries found'}), 404
        
    except Exception as e:
        current_app.logger.error(f"Error getting latest summary: {e}")
        return jsonify({'error': 'Could not get latest summary'}), 500
