# app/routes/process_audio.py
from flask import request, Blueprint, jsonify, current_app
from app.assistant.ServiceLocator.service_locator import DI
from app.create_app import socketio
from app.assistant.utils.pydantic_classes import Message
import uuid
from datetime import datetime, timezone
import os
import subprocess
from werkzeug.utils import secure_filename
from app.services.speech_to_text import SpeechToTextEngineFactory
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

process_audio_bp = Blueprint('process_audio', __name__)
@process_audio_bp.route('/process_audio', methods=['POST'])
def process_audio():
    logger.debug("process_audio: request received")
    try:
        sid = request.form.get('socket_id')
        audio_output = request.form.get('audio_output', 'false').lower() == 'true'
        streaming = request.form.get('streaming', 'false').lower() == 'true'
        speaking_mode = request.form.get('speaking_mode', 'false').lower() == 'true'

        if 'audio' not in request.files:
            return jsonify({'error': 'No audio file provided'}), 400

        audio_file = request.files['audio']
        if audio_file.filename == '':
            return jsonify({'error': 'Empty audio file'}), 400

        upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)

        original_filename = secure_filename(audio_file.filename)
        input_path = os.path.join(upload_folder, f"{uuid.uuid4().hex}_{original_filename}")
        audio_file.save(input_path)
        input_size = os.path.getsize(input_path)
        if input_size < 2048:
            try:
                os.remove(input_path)
            except Exception:
                logger.debug("process_audio: could not remove short input file %s", input_path, exc_info=True)
            return jsonify({'error': 'Audio too short or empty'}), 400

        # Enforce server-side cap (belt-and-suspenders with Flask MAX_CONTENT_LENGTH)
        max_bytes = 30 * 1024 * 1024
        if input_size > max_bytes:
            try:
                os.remove(input_path)
            except Exception:
                logger.debug("process_audio: could not remove oversized input file %s", input_path, exc_info=True)
            logger.error("process_audio: upload too large: %d bytes", input_size)
            return jsonify({'error': 'Recording is too large. Please keep voice messages under 3 minutes.'}), 413

        logger.info("process_audio: file=%s size=%d mime=%s", audio_file.filename, input_size, audio_file.mimetype)

        converted_path = input_path.rsplit('.', 1)[0] + '.wav'
        logger.debug("process_audio: converting to WAV: %s", converted_path)

        # 🔧 Robust ffmpeg conversion
        conversion_result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", input_path,
                "-vn",
                "-acodec", "pcm_s16le",
                "-ac", "1",
                "-ar", "16000",
                converted_path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        if conversion_result.returncode != 0:
            logger.error("process_audio: ffmpeg conversion failed: %s", conversion_result.stderr.decode())
            try:
                os.remove(input_path)
            except Exception:
                logger.debug("process_audio: could not remove input after failed conversion %s", input_path, exc_info=True)
            return jsonify({'error': 'Audio conversion failed (invalid or unsupported recording)'}), 400

        logger.debug("process_audio: converted to %s", converted_path)

        # 🎙️ Transcribe with Whisper
        stt_engine = SpeechToTextEngineFactory.create_engine("whisper")
        transcribed_text = stt_engine.transcribe(converted_path)
        text = transcribed_text.text if transcribed_text else "[No transcription]"
        logger.info("process_audio: transcribed %d chars", len(text))

        # We don't send the transcribed text directly — the UI sends it as a chat message.

        # Clean up files
        for path in [input_path, converted_path]:
            try:
                os.remove(path)
            except Exception as e:
                logger.debug("process_audio: failed to delete %s: %s", path, e)

        return jsonify({'transcribed_text': text}), 200

    except Exception as e:
        logger.error("process_audio: unhandled error: %s", e)
        logger.debug("process_audio: exception details", exc_info=True)
        return jsonify({'error': 'Processing failed'}), 500
