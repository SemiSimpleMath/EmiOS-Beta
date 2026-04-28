from flask import Blueprint, request, jsonify
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

tool_route_bp = Blueprint('tool_route', __name__)

_ALLOWED_TOOLS = {"update_todo_task"}


@tool_route_bp.route('/tool/', methods=['POST'])
def handle_tool_route():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'success': False, 'message': 'Invalid JSON body'}), 400

        tool_name = str(data.get("tool_name") or "").strip()
        if tool_name not in _ALLOWED_TOOLS:
            logger.warning("Rejected disallowed tool_name from UI: %r", tool_name)
            return jsonify({'success': False, 'message': 'Tool not permitted'}), 403

        if tool_name == "update_todo_task":
            return _handle_update_todo_task(data)

        return jsonify({'success': False, 'message': 'Unhandled tool'}), 400

    except Exception as e:
        logger.error("Error handling tool route: %s", e)
        logger.debug("tool route exception details", exc_info=True)
        return jsonify({'success': False, 'message': 'Error processing request'}), 500


def _handle_update_todo_task(data: dict):
    args = data.get("arguments") or {}
    task_id = str(args.get("task_id") or "").strip()
    completed = args.get("completed")
    action = str(data.get("action") or "").strip()
    meta_data = data.get("meta_data") or {}

    if not task_id:
        return jsonify({'success': False, 'message': 'Missing task_id'}), 400
    if not isinstance(completed, bool):
        return jsonify({'success': False, 'message': 'completed must be a boolean'}), 400

    from app.assistant.lib.core_tools.todo_tool.todo_tool import ToDoTool
    from app.assistant.utils.pydantic_classes import ToolMessage, Message

    tool_message = ToolMessage(
        tool_name="update_todo_task",
        tool_data={
            "tool_name": "update_todo_task",
            "arguments": {"task_id": task_id, "completed": completed},
        },
    )
    tool = ToDoTool()
    tool.execute(tool_message)
    logger.info("update_todo_task completed. task_id=%s completed=%s", task_id, completed)

    agent_response = None
    try:
        feedback_input = f"Action taken was {action}. Object metadata {meta_data}"
        feedback_msg = DI.agent_factory.create_agent('tool_feedback').action_handler(
            Message(agent_input=feedback_input)
        )
        result = getattr(feedback_msg, "data", None)
        if isinstance(result, dict):
            agent_response = result.get("chat_str") or result.get("tts_str")
    except Exception as e:
        logger.error("tool_feedback agent failed: %s", e)
        logger.debug("tool_feedback exception details", exc_info=True)

    return jsonify({'success': True, 'agent_response': agent_response}), 200
