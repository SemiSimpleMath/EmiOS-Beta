# tts_module.py

import os
import tempfile
import subprocess
import sys
import config

from flask import url_for
from flask_socketio import SocketIO
from openai import OpenAI

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

# Initialize SocketIO (assigned from the main Flask app)
socketio = SocketIO()


def _get_openai_client() -> OpenAI:
    """Lazily create the OpenAI client using the settings manager or env var."""
    from app.assistant.user_settings_manager.user_settings import get_settings_manager
    api_key = get_settings_manager().get_api_key("openai") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not configured. Set it in environment or user settings.")
    return OpenAI(api_key=api_key)


def _get_voice_config() -> dict:
    """
    Return the current TTS configuration from user settings.

    Keys:
      provider         — 'openai' | 'gemini'
      openai_voice     — e.g. 'nova'
      gemini_voice     — e.g. 'Kore'
      gemini_model     — e.g. 'gemini-2.5-flash-tts'
      gemini_api_key   — raw key or empty string
    """
    from app.assistant.user_settings_manager.user_settings import get_settings_manager
    sm = get_settings_manager()
    return {
        'provider': sm.get('preferences.default_tts_provider', 'openai'),
        'openai_voice': sm.get('api_settings.openai.tts_voice', 'nova'),
        'gemini_voice': sm.get('api_settings.gemini.tts_voice', 'Kore'),
        'gemini_model': sm.get('api_settings.gemini.tts_model', 'gemini-2.5-flash-preview-tts'),
        'gemini_api_key': sm.get_api_key('google') or os.environ.get('GOOGLE_API_KEY') or '',
    }


def stream_audio_from_openai(text: str, file_path: str, voice: str = 'nova'):
    logger.info("[TTS:openai] Generating audio voice=%s text_len=%d", voice, len(text))
    temp_raw_path = file_path + ".raw.mp3"
    try:
        response = _get_openai_client().audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text,
        )
        with open(temp_raw_path, 'wb') as f:
            for chunk in response.iter_bytes(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        if not boost_audio_volume(temp_raw_path, file_path, gain=2.0):
            logger.warning("[TTS:openai] Volume boost failed — using unboosted audio.")
            os.rename(temp_raw_path, file_path)
        else:
            os.remove(temp_raw_path)

        logger.info("[TTS:openai] Audio saved to %s", file_path)
    except Exception as e:
        exc_str = str(e).lower()
        if "insufficient_quota" in exc_str or "exceeded your current quota" in exc_str:
            logger.error("[TTS:openai] 🚨 QUOTA EXHAUSTED — stopping TTS. %s", e)
            from app.services.llm_client import _check_and_trip_quota, QuotaExhaustedError
            _check_and_trip_quota("openai", e)
            raise QuotaExhaustedError("openai", str(e)) from e
        logger.error("[TTS:openai] Failed: %s", e, exc_info=True)
        raise


def stream_audio_from_gemini(text: str, file_path: str, voice: str, model: str, api_key: str):
    """
    Generate TTS via Gemini API (google-genai SDK).
    Returns raw PCM audio (24 kHz, 16-bit, mono) which we write as WAV, then
    convert to MP3 via ffmpeg so the rest of the pipeline stays consistent.
    """
    import wave
    from google import genai as gai
    from google.genai import types as gtypes

    logger.info("[TTS:gemini] Generating audio model=%s voice=%s text_len=%d", model, voice, len(text))

    try:
        g_client = gai.Client(api_key=api_key)
        response = g_client.models.generate_content(
            model=model,
            contents=text,
            config=gtypes.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=gtypes.SpeechConfig(
                    voice_config=gtypes.VoiceConfig(
                        prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(
                            voice_name=voice,
                        )
                    )
                ),
            ),
        )

        pcm_data = response.candidates[0].content.parts[0].inline_data.data

        # Write PCM → WAV temp file
        wav_path = file_path + ".raw.wav"
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)   # 16-bit
            wf.setframerate(24000)
            wf.writeframes(pcm_data)

        # Convert WAV → MP3 (same format the rest of the system expects)
        success = _wav_to_mp3(wav_path, file_path)
        if not success:
            # Fallback: rename wav to the expected output path (player can handle WAV)
            logger.warning("[TTS:gemini] WAV→MP3 conversion failed — serving raw WAV.")
            os.rename(wav_path, file_path)
        else:
            os.remove(wav_path)

        logger.info("[TTS:gemini] Audio saved to %s", file_path)
    except Exception as e:
        logger.error("[TTS:gemini] Failed: %s", e, exc_info=True)
        raise


def _wav_to_mp3(input_path: str, output_path: str) -> bool:
    """Convert a WAV file to MP3 using ffmpeg."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-codec:a", "libmp3lame", "-qscale:a", "2", output_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.warning("[TTS] WAV→MP3 failed: %s", e)
        return False


def create_temp_audio_file(audio_file_suffix, temp_audio_dir):
    try:
        os.makedirs(temp_audio_dir, exist_ok=True)
        fd, temp_file_path = tempfile.mkstemp(suffix=audio_file_suffix, dir=temp_audio_dir)
        os.close(fd)
        logger.debug("[TTS] Temp audio file created: %s", temp_file_path)
        return temp_file_path
    except Exception as e:
        logger.error("[TTS] create_temp_audio_file failed: %s", e, exc_info=True)
        raise


def create_audio_file_path_for_web(temp_file_path):
    try:
        filename = os.path.basename(temp_file_path)
        web_path = os.path.join(config.DevelopmentConfig.TEMP_FOLDER_AUDIO_WEB, filename)
        return web_path.replace("\\", "/")
    except Exception as e:
        logger.error("[TTS] create_audio_file_path_for_web failed: %s", e, exc_info=True)
        raise


def boost_audio_volume(input_path: str, output_path: str, gain: float = 2.0) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-filter:a", f"volume={gain}", output_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.debug("[TTS] Volume boosted -> %s", output_path)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning("[TTS] Volume boost failed: %s", e)
        return False


def process_text(text: str, sid: str, socket_io):
    """
    Convert text to speech using the configured provider and emit the audio URL via SocketIO.
    """
    try:
        temp_audio_dir = config.DevelopmentConfig.TEMP_FOLDER_AUDIO
        temp_file_path = create_temp_audio_file(
            config.DevelopmentConfig.AUDIO_FILE_SUFFIX,
            temp_audio_dir,
        )

        cfg = _get_voice_config()
        provider = cfg['provider']

        if provider == 'gemini':
            if not cfg['gemini_api_key']:
                raise ValueError("Gemini TTS selected but GOOGLE_API_KEY is not set.")
            stream_audio_from_gemini(
                text,
                temp_file_path,
                voice=cfg['gemini_voice'],
                model=cfg['gemini_model'],
                api_key=cfg['gemini_api_key'],
            )
        else:
            stream_audio_from_openai(text, temp_file_path, voice=cfg['openai_voice'])

        file_url = "static/" + create_audio_file_path_for_web(temp_file_path)

        payload = {"message": "Audio file generated", "audio_url": file_url, "text": text}
        logger.info("[TTS] Emitting audio_file to room=%s url=%s", sid, file_url)
        socket_io.emit('audio_file', payload, room=sid)
        logger.info("[TTS] audio_file emitted to room=%s", sid)
    except Exception as e:
        logger.error("[TTS] process_text failed: %s", e, exc_info=True)
        socket_io.emit('audio_file_error', {"error": str(e), "text": text}, room=sid)


def generate_sample(provider: str, voice: str) -> bytes:
    """
    Generate a short TTS sample clip and return raw audio bytes (MP3 or WAV).
    Used by the /api/voice/sample preview endpoint.
    """
    sample_text = "Hi there! This is a sample of my voice. I hope you like it."
    cfg = _get_voice_config()

    fd, tmp_path = tempfile.mkstemp(suffix=config.DevelopmentConfig.AUDIO_FILE_SUFFIX)
    os.close(fd)
    try:
        if provider == 'gemini':
            api_key = cfg['gemini_api_key']
            if not api_key:
                raise ValueError("Gemini TTS selected but GOOGLE_API_KEY is not set.")
            stream_audio_from_gemini(sample_text, tmp_path, voice=voice, model=cfg['gemini_model'], api_key=api_key)
        else:
            stream_audio_from_openai(sample_text, tmp_path, voice=voice)

        with open(tmp_path, 'rb') as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
