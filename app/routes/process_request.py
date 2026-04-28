# app/routes/process_request.py

import os
import uuid
from datetime import datetime

from flask import request, Blueprint, jsonify, current_app
from werkzeug.utils import secure_filename

from app.assistant.runtime import start_monitored_thread
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.identity_names import get_required_primary_user_name

process_request_bp = Blueprint('process_request', __name__)

from app.assistant.utils.logging_config import get_logger
from app.assistant.performance.performance_monitor import performance_monitor
logger = get_logger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def _get_filestorage_size_bytes(file_storage) -> int:
    """
    Best-effort size calculation for Werkzeug FileStorage without consuming the stream.
    """
    try:
        stream = getattr(file_storage, "stream", None)
        if stream is None:
            return 0
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = int(stream.tell())
        stream.seek(pos)
        return size
    except Exception:
        return 0


@process_request_bp.route('/process_request', methods=['POST'])
def process_request():
    timer_id = performance_monitor.start_timer('process_request', str(uuid.uuid4()))
    
    try:
        # socket_id is optional — preferred path is the register_chat_client binding,
        # but older JS clients may still send it. If present, we use it as a
        # safety-net bind below so the room_id -> socket mapping is fresh for
        # this request even if register didn't fire (e.g. Cloudflare tunnel quirks).
        sid = (request.form.get('socket_id') or "").strip() or None
        request_id = str(uuid.uuid4())
        text = (request.form.get('text') or "").strip()
        speaking_mode = request.form.get('speaking_mode')
        
        is_memo_mode = request.form.get('memo') == 'true'
        
        image_file = request.files.get('image')
        has_image = bool(image_file and getattr(image_file, "filename", ""))

        if is_memo_mode and has_image:
            performance_monitor.end_timer(timer_id, {'status': 'error', 'error': 'Images not supported in memo mode'})
            return jsonify({'error': 'Images are not supported in memo mode'}), 400

        if not text and not has_image:
            performance_monitor.end_timer(timer_id, {'status': 'error', 'error': 'No text or image provided'})
            return jsonify({'error': 'No text or image provided'}), 400

        # IMPORTANT:
        # Safety-net bind: ensure the socket is bound to this request's room_id
        # on the server, even if `register_chat_client` wasn't delivered (e.g.
        # Cloudflare tunnel websocket quirks). The in-flight request gives us
        # an authoritative (room_id, socket_id) pair — use it.
        target_room = str(request.form.get("room_id") or "master_room").strip()
        try:
            if sid:
                current_app.DI.socket_manager.bind(target_room, sid)
        except Exception:
            logger.debug("Could not bind socket for sid=%s room_id=%s", sid, target_room, exc_info=True)

        # Handle memo mode (direct database storage, no agent processing)
        if is_memo_mode:
            return handle_memo_mode(text, sid, timer_id)
        
        # Normal processing (test mode or normal mode)
        return handle_normal_processing(text, sid, speaking_mode, timer_id, request_id=request_id, image_file=image_file, target_room=target_room)
            
    except Exception as e:
        logger.error("Error during processing request: %s", e)
        logger.debug("error during processing request exception details", exc_info=True)
        performance_monitor.end_timer(timer_id, {'status': 'error', 'error': str(e)})
        return jsonify({'error': 'Processing failed'}), 500


def handle_memo_mode(text, socket_id, timer_id):
    """Memo mode: Direct database storage, no agent processing"""
    try:
        from app.assistant.maintenance_manager.save_to_unified_db import save_to_unified_db
        from datetime import timezone

        payload = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc),
            "role": "user",
            "message": text,
            "source": "chat",
            "processed": False,
        }
        save_to_unified_db([payload], source="chat")

        logger.info(f"📝 Memo saved: {text[:50]}...")
        performance_monitor.end_timer(timer_id, {'status': 'success', 'mode': 'memo'})
        return jsonify({'message': 'Memo saved successfully'}), 200

    except Exception as e:
        logger.error("Error in memo mode: %s", e)
        logger.debug("error in memo mode exception details", exc_info=True)
        performance_monitor.end_timer(timer_id, {'status': 'error', 'error': str(e)})
        return jsonify({'error': 'Failed to save memo'}), 500


def handle_normal_processing(text, socket_id, speaking_mode, timer_id, request_id: str, image_file=None, target_room: str = "master_room"):
    """Unified processing for both test and normal modes"""
    try:
        speaking_requested = speaking_mode == "true"

        metadata: dict = {}

        if image_file and getattr(image_file, "filename", ""):
            if image_file.mimetype not in ALLOWED_IMAGE_MIMES:
                performance_monitor.end_timer(timer_id, {'status': 'error', 'error': 'Invalid image type'})
                return jsonify({'error': f"Unsupported image type: {image_file.mimetype}"}), 400

            size_bytes = _get_filestorage_size_bytes(image_file)
            if size_bytes and size_bytes > MAX_IMAGE_BYTES:
                performance_monitor.end_timer(timer_id, {'status': 'error', 'error': 'Image too large'})
                return jsonify({'error': 'Image too large (max 20MB)'}), 400

            base_upload = current_app.config.get('UPLOAD_FOLDER', 'uploads')
            temp_dir = os.path.join(base_upload, 'temp')
            os.makedirs(temp_dir, exist_ok=True)

            original_filename = secure_filename(image_file.filename) or "image"
            original_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}_{original_filename}")
            image_file.save(original_path)

            from app.assistant.utils.image_processing import prepare_chat_image

            processed = prepare_chat_image(original_path, temp_dir)
            processed_path = processed.output_path

            try:
                if os.path.exists(original_path) and os.path.abspath(original_path) != os.path.abspath(processed_path):
                    os.remove(original_path)
            except Exception:
                logger.debug("Could not remove original upload at %s", original_path, exc_info=True)

            metadata["attachments"] = [
                {
                    "type": "image",
                    "path": processed_path,
                    "original_filename": original_filename,
                    "content_type": image_file.mimetype,
                    "size_bytes": int(size_bytes or 0),
                    "processed_width": processed.output_width,
                    "processed_height": processed.output_height,
                }
            ]

        reply_to = {"type": "socketio", "room_id": target_room, "tts_requested": bool(speaking_requested)}
        metadata["reply_to"] = reply_to
        metadata["interaction_mode"] = "speaking" if speaking_requested else "text"
        logger.info("[TTS:process_request] speaking_requested=%s reply_to=%r", speaking_requested, reply_to)

        try:
            current_app.DI.reply_router.set_route(request_id, reply_to)
        except Exception:
            logger.debug("Could not set reply route for request_id=%s", request_id, exc_info=True)

        from app.assistant.runtime import start_monitored_thread

        start_monitored_thread(
            owner="process_request",
            name="ui-chat-async",
            target=_run_room_inbound,
            kwargs={
                "socket_id": socket_id,
                "body": text,
                "request_id": request_id,
                "metadata": metadata,
                "room_id": target_room,
            },
            daemon=True,
            kind="request_worker",
            metadata={"component": "process_request"},
        )
        logger.info("Dispatched ui chat to %s async. request_id=%s", target_room, request_id)

        performance_monitor.end_timer(timer_id, {'status': 'success'})
        return jsonify({'message': 'Request sent'}), 200

    except Exception as e:
        logger.error("Error during processing request: %s", e)
        logger.debug("processing request exception details", exc_info=True)
        performance_monitor.end_timer(timer_id, {'status': 'error', 'error': str(e)})
        return jsonify({'error': 'Processing failed'}), 500


def _run_room_inbound(*, socket_id: str, body: str, request_id: str, metadata: dict, room_id: str = "master_room") -> None:
    try:
        primary_user_name = get_required_primary_user_name()
        DI.room_session_manager.handle_ui_inbound(
            socket_id=socket_id,
            body=body,
            room_id=room_id,
            request_id=request_id,
            sender_name=primary_user_name,
            send_reply=True,
            message_persistence_mode="global_blackboard_and_unified_log",
            metadata=metadata,
        )
        logger.info("%s chat completed. request_id=%s", room_id, request_id)
    except Exception as e:
        logger.error("%s chat failed. request_id=%s error=%s", room_id, request_id, e)
        logger.debug("%s chat exception details", room_id, exc_info=True)

