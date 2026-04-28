from flask import request, Blueprint, jsonify, current_app
from app.assistant.preference_manager.preference_manager import PreferenceManager
from app.assistant.utils.pydantic_classes import Message

idle_route_bp = Blueprint('handle_idle_route', __name__)


@idle_route_bp.route('/handle_idle_route', methods=['POST'])
def handle_idle_route():

    try:
        data = request.get_json()
        socket_id = data.get('socket_id')
        timestamp = data.get('timestamp')
        preferences = data.get('preferences', [])  # Extract preferences from request

        if not socket_id:
            return jsonify({'success': False, 'message': 'Missing socket_id'}), 400

        # Process preferences if any exist
        if preferences:
            current_app.logger.info(f"Processing {len(preferences)} preferences")
            preference_manager = PreferenceManager()
            
            for preference in preferences:
                try:
                    # Create a message for the label agent
                    # Combine title and body for labeling
                    combined_content = f"{preference.get('title', '')}\n\n{preference.get('body', '')}"
                    
                    feedback_msg = Message(
                        data_type="agent_msg",
                        sender="IdleRoute",
                        receiver="LabelAgent",
                        content=combined_content,
                        agent_input=combined_content,  # Label agent expects this field
                        data={
                            "category": preference.get('category', 'news'),
                            "preference": preference.get('preference'),
                            "title": preference.get('title'),
                            "id": preference.get('id')
                        }
                    )
                    
                    # Process the feedback
                    preference_manager.handle_feedback(feedback_msg)
                    current_app.logger.info(f"Processed preference for: {preference.get('title', 'Unknown')}")
                    
                except Exception as e:
                    current_app.logger.error(f"Error processing preference {preference}: {e}")
                    continue

        return jsonify({'success': True, 'message': 'Idle state and preferences recorded'}), 200

    except Exception as e:
        current_app.logger.error(f"Error handling idle route: {e}")
        return jsonify({'success': False, 'message': 'Error processing idle route'}), 500
