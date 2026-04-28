from flask import Blueprint, jsonify, request
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

ask_user_route_bp = Blueprint('ask_user_route', __name__)
logger = get_logger(__name__)

@ask_user_route_bp.route('/ask_user_answer', methods=['POST'])
def ask_user_answer():
    data = request.get_json(silent=True) or {}
    question_id = data.get("question_id")
    answer = data.get("answer")
    request_id = data.get("request_id")
    room_id = data.get("room_id")
    reply_to = data.get("reply_to")
    user_identity = data.get("user_identity")

    if not isinstance(question_id, str) or not question_id.strip():
        return jsonify({'success': False, 'error': 'Missing question_id'}), 400
    if not isinstance(answer, str) or not answer.strip():
        return jsonify({'success': False, 'error': 'Missing answer'}), 400
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        return jsonify({'success': False, 'error': 'request_id must be a non-empty string when provided'}), 400
    if room_id is not None and (not isinstance(room_id, str) or not room_id.strip()):
        return jsonify({'success': False, 'error': 'room_id must be a non-empty string when provided'}), 400
    if reply_to is not None and not isinstance(reply_to, dict):
        return jsonify({'success': False, 'error': 'reply_to must be an object when provided'}), 400
    if user_identity is not None and (not isinstance(user_identity, str) or not user_identity.strip()):
        return jsonify({'success': False, 'error': 'user_identity must be a non-empty string when provided'}), 400

    try:
        DI.question_service.resolve_question(
            question_id=question_id.strip(),
            answer=answer.strip(),
            request_id=request_id.strip() if isinstance(request_id, str) else None,
            room_id=room_id.strip() if isinstance(room_id, str) else None,
            reply_to=reply_to if isinstance(reply_to, dict) else None,
            user_identity=user_identity.strip() if isinstance(user_identity, str) else None,
        )
        return jsonify({'success': True, 'status': 'Answer delivered to question service'}), 200
    except Exception as e:
        logger.error("ask_user_answer failed for question_id=%r: %s", question_id, e)
        logger.debug("ask_user_answer exception details", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 400


@ask_user_route_bp.route('/ask_user_cancel', methods=['POST'])
def ask_user_cancel():
    data = request.get_json(silent=True) or {}
    question_id = data.get("question_id")
    reason = data.get("reason")
    if not isinstance(question_id, str) or not question_id.strip():
        return jsonify({'success': False, 'error': 'Missing question_id'}), 400
    if reason is not None and not isinstance(reason, str):
        return jsonify({'success': False, 'error': 'reason must be string when provided'}), 400

    try:
        DI.question_service.cancel_question(question_id=question_id.strip(), reason=reason.strip() if isinstance(reason, str) else None)
        return jsonify({'success': True, 'status': 'Question cancelled'}), 200
    except Exception as e:
        logger.error("ask_user_cancel failed for question_id=%r: %s", question_id, e)
        logger.debug("ask_user_cancel exception details", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 400
