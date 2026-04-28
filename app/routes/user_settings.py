"""
User Settings Routes
API endpoints for managing user settings
"""

from flask import Blueprint, jsonify, request
import os
from app.assistant.user_settings_manager.user_settings import get_settings_manager
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

user_settings_bp = Blueprint('user_settings', __name__)


def notify_settings_changed():
    """Publish settings_changed event to EventHub"""
    try:
        if hasattr(DI, 'event_hub') and DI.event_hub:
            from app.assistant.utils.pydantic_classes import Message
            msg = Message(content="Settings updated")
            msg.event_topic = "settings_changed"
            DI.event_hub.publish(msg)
    except Exception as e:
        logger.warning("Could not publish settings_changed event: %s", e, exc_info=True)


@user_settings_bp.route('/api/settings', methods=['GET'])
def get_settings():
    """Get all user settings (public, no API keys)"""
    try:
        settings_manager = get_settings_manager()
        public_settings = settings_manager.get_public_settings()
        
        return jsonify({
            'success': True,
            'settings': public_settings
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error retrieving settings: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/<setting_key>', methods=['GET'])
def get_setting(setting_key):
    """Get a specific setting value"""
    try:
        settings_manager = get_settings_manager()
        value = settings_manager.get(setting_key)
        
        return jsonify({
            'success': True,
            'key': setting_key,
            'value': value
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error retrieving setting: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/<setting_key>', methods=['PUT'])
def update_setting(setting_key):
    """Update a specific setting value"""
    try:
        data = request.get_json()
        
        if 'value' not in data:
            return jsonify({
                'success': False,
                'message': 'Missing "value" in request body'
            }), 400
        
        settings_manager = get_settings_manager()
        settings_manager.set(setting_key, data['value'])
        
        # Notify that settings changed
        notify_settings_changed()
        
        return jsonify({
            'success': True,
            'key': setting_key,
            'value': data['value'],
            'message': 'Setting updated successfully'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error updating setting: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/api-keys/status', methods=['GET'])
def get_api_keys_status():
    """Get status of API keys from environment variables (whether they're set, not the actual keys)"""
    try:
        settings_manager = get_settings_manager()
        
        providers = ['openai', 'google', 'gemini', 'anthropic', 'elevenlabs', 'deepgram']
        status = {}
        
        for provider in providers:
            api_key = settings_manager.get_api_key(provider)
            status[provider] = {
                'configured': bool(api_key and api_key.strip()),
                'source': 'environment_variable'
            }
        
        return jsonify({
            'success': True,
            'api_keys': status,
            'note': 'API keys are read from environment variables only'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error retrieving API key status: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/features', methods=['GET'])
def get_features():
    """Get all feature flags"""
    try:
        settings_manager = get_settings_manager()
        features = settings_manager.get('features', {})
        
        # Auto-disable features that can't run (missing OAuth/API keys)
        availability = settings_manager.get_available_features()
        for feature_name, status in availability.items():
            feature_key = f"enable_{feature_name}"
            if feature_key in features:
                # If feature is enabled but missing requirements, auto-disable it
                if features[feature_key] and not status['can_enable']:
                    features[feature_key] = False
                    settings_manager.enable_feature(feature_name, False)
        
        return jsonify({
            'success': True,
            'features': features
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error retrieving features: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/features/availability', methods=['GET'])
def get_features_availability():
    """Get feature availability status based on dependencies"""
    try:
        settings_manager = get_settings_manager()
        availability = settings_manager.get_available_features()
        
        return jsonify({
            'success': True,
            'features': availability
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error retrieving feature availability: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/features/<feature_name>/check', methods=['GET'])
def check_feature_availability(feature_name):
    """Check if a specific feature can be enabled"""
    try:
        settings_manager = get_settings_manager()
        check_result = settings_manager.check_feature_can_be_enabled(feature_name)
        
        return jsonify({
            'success': True,
            'feature': feature_name,
            'availability': check_result
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error checking feature availability: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/features/<feature_name>', methods=['PUT'])
def toggle_feature(feature_name):
    """Toggle a feature on/off"""
    try:
        data = request.get_json()
        
        if 'enabled' not in data:
            return jsonify({
                'success': False,
                'message': 'Missing "enabled" in request body'
            }), 400
        
        settings_manager = get_settings_manager()
        settings_manager.enable_feature(feature_name, data['enabled'])
        
        # Notify that settings changed
        notify_settings_changed()
        
        return jsonify({
            'success': True,
            'feature': feature_name,
            'enabled': data['enabled'],
            'message': f'Feature {feature_name} {"enabled" if data["enabled"] else "disabled"}'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error toggling feature: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/preferences', methods=['GET'])
def get_preferences():
    """Get all user preferences"""
    try:
        settings_manager = get_settings_manager()
        preferences = settings_manager.get('preferences', {})
        
        return jsonify({
            'success': True,
            'preferences': preferences
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error retrieving preferences: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/preferences/<pref_name>', methods=['PUT'])
def update_preference(pref_name):
    """Update a user preference"""
    try:
        data = request.get_json()
        
        if 'value' not in data:
            return jsonify({
                'success': False,
                'message': 'Missing "value" in request body'
            }), 400
        
        settings_manager = get_settings_manager()
        settings_manager.set_preference(pref_name, data['value'])
        
        # Notify that settings changed
        notify_settings_changed()
        
        return jsonify({
            'success': True,
            'preference': pref_name,
            'value': data['value'],
            'message': f'Preference {pref_name} updated successfully'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error updating preference: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/validate', methods=['GET'])
def validate_settings():
    """Validate current settings"""
    try:
        settings_manager = get_settings_manager()
        validation_result = settings_manager.validate_settings()
        
        return jsonify({
            'success': True,
            'validation': validation_result,
            'is_valid': len(validation_result['errors']) == 0
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error validating settings: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/export', methods=['GET'])
def export_settings():
    """Export settings (without API keys)"""
    try:
        settings_manager = get_settings_manager()
        public_settings = settings_manager.get_public_settings()
        
        return jsonify({
            'success': True,
            'settings': public_settings,
            'timestamp': settings_manager.get('system.last_updated')
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error exporting settings: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/import', methods=['POST'])
def import_settings():
    """Import settings"""
    try:
        data = request.get_json()
        
        if 'settings' not in data:
            return jsonify({
                'success': False,
                'message': 'Missing "settings" in request body'
            }), 400
        
        settings_manager = get_settings_manager()
        
        # Merge imported settings
        merge = data.get('merge', True)
        for key, value in data['settings'].items():
            if key not in ['_comment', '_warning']:  # Skip metadata
                settings_manager.set(key, value, save=False)
        
        # Save once at the end
        settings_manager._save_settings()
        
        return jsonify({
            'success': True,
            'message': 'Settings imported successfully'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error importing settings: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/reset', methods=['POST'])
def reset_settings():
    """Reset settings to defaults (requires confirmation)"""
    try:
        data = request.get_json() or {}
        
        if not data.get('confirm'):
            return jsonify({
                'success': False,
                'message': 'Reset requires confirmation: set "confirm": true in request body'
            }), 400
        
        settings_manager = get_settings_manager()
        settings_manager.reset_to_defaults()
        
        return jsonify({
            'success': True,
            'message': 'Settings reset to defaults successfully'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error resetting settings: {str(e)}'
        }), 500


# Quiet Mode Routes

@user_settings_bp.route('/api/settings/quiet-mode', methods=['GET'])
def get_quiet_mode():
    """Get quiet mode settings"""
    try:
        settings_manager = get_settings_manager()
        quiet_mode = settings_manager.get('quiet_mode', {})
        
        return jsonify({
            'success': True,
            'quiet_mode': quiet_mode
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error retrieving quiet mode settings: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/quiet-mode/toggle', methods=['POST'])
def toggle_quiet_mode():
    """Toggle quiet mode on/off"""
    try:
        settings_manager = get_settings_manager()
        current_status = settings_manager.get('quiet_mode.enabled', False)
        new_status = not current_status
        
        settings_manager.set('quiet_mode.enabled', new_status)
        notify_settings_changed()
        
        return jsonify({
            'success': True,
            'enabled': new_status,
            'message': f'Quiet mode {"enabled" if new_status else "disabled"}'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error toggling quiet mode: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/quiet-mode/universal', methods=['PUT'])
def update_universal_quiet_hours():
    """Update universal quiet hours"""
    try:
        data = request.get_json()
        start = data.get('start')
        end = data.get('end')
        
        if not start or not end:
            return jsonify({
                'success': False,
                'message': 'Start and end times are required'
            }), 400
        
        settings_manager = get_settings_manager()
        settings_manager.set('quiet_mode.universal_hours.start', start)
        settings_manager.set('quiet_mode.universal_hours.end', end)
        notify_settings_changed()
        
        return jsonify({
            'success': True,
            'message': 'Universal quiet hours updated',
            'universal_hours': {
                'start': start,
                'end': end
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error updating universal quiet hours: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/quiet-mode/feature/<feature>', methods=['PUT'])
def update_feature_quiet_hours(feature):
    """Update per-feature quiet hours"""
    try:
        data = request.get_json()
        enabled = data.get('enabled', False)
        start = data.get('start')
        end = data.get('end')
        
        settings_manager = get_settings_manager()
        
        # Update feature-specific settings
        if enabled is not None:
            settings_manager.set(f'quiet_mode.per_feature.{feature}.enabled', enabled)
        if start:
            settings_manager.set(f'quiet_mode.per_feature.{feature}.start', start)
        if end:
            settings_manager.set(f'quiet_mode.per_feature.{feature}.end', end)
        
        notify_settings_changed()
        
        return jsonify({
            'success': True,
            'message': f'Quiet hours for {feature} updated',
            'feature_settings': settings_manager.get(f'quiet_mode.per_feature.{feature}', {})
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error updating feature quiet hours: {str(e)}'
        }), 500


@user_settings_bp.route('/api/settings/quiet-mode/status', methods=['GET'])
def get_quiet_mode_status():
    """Check if quiet mode is currently active"""
    try:
        settings_manager = get_settings_manager()
        feature = request.args.get('feature')  # Optional feature parameter
        
        is_active = settings_manager.is_quiet_mode_active(feature)
        
        return jsonify({
            'success': True,
            'is_active': is_active,
            'feature': feature
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error checking quiet mode status: {str(e)}'
        }), 500


@user_settings_bp.route('/api/voice/settings', methods=['GET'])
def get_voice_settings():
    """Return current TTS provider + per-provider voice config."""
    try:
        settings_manager = get_settings_manager()
        provider = settings_manager.get('preferences.default_tts_provider', 'openai')
        openai_voice = settings_manager.get('api_settings.openai.tts_voice', 'nova')
        gemini_voice = settings_manager.get('api_settings.gemini.tts_voice', 'Kore')
        gemini_model = settings_manager.get('api_settings.gemini.tts_model', 'gemini-2.5-flash-preview-tts')
        gemini_api_key = settings_manager.get_api_key('gemini') or ''
        return jsonify({
            'success': True,
            'provider': provider,
            'openai_voice': openai_voice,
            'gemini_voice': gemini_voice,
            'gemini_model': gemini_model,
            'gemini_configured': bool(gemini_api_key),
        })
    except Exception as e:
        logger.debug("get_voice_settings failed: %s", e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@user_settings_bp.route('/api/voice/settings', methods=['PUT'])
def update_voice_settings():
    """Persist TTS provider + per-provider voice config."""
    try:
        data = request.get_json() or {}
        settings_manager = get_settings_manager()

        if 'provider' in data:
            settings_manager.set('preferences.default_tts_provider', data['provider'])
        if 'openai_voice' in data:
            settings_manager.set('api_settings.openai.tts_voice', data['openai_voice'])
        if 'gemini_voice' in data:
            settings_manager.set('api_settings.gemini.tts_voice', data['gemini_voice'])
        if 'gemini_model' in data:
            settings_manager.set('api_settings.gemini.tts_model', data['gemini_model'])

        notify_settings_changed()
        return jsonify({'success': True, 'message': 'Voice settings saved.'})
    except Exception as e:
        logger.debug("update_voice_settings failed: %s", e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@user_settings_bp.route('/api/voice/elevenlabs-voices', methods=['GET'])
def list_elevenlabs_voices():
    """Proxy the ElevenLabs /voices endpoint so the frontend never handles the raw API key."""
    try:
        import urllib.request
        settings_manager = get_settings_manager()
        api_key = settings_manager.get_api_key('elevenlabs')
        if not api_key:
            return jsonify({'success': False, 'message': 'ElevenLabs API key not configured.'}), 400

        req = urllib.request.Request(
            'https://api.elevenlabs.io/v1/voices',
            headers={'xi-api-key': api_key, 'Accept': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as _json
            raw = _json.loads(resp.read().decode())

        voices = [
            {'voice_id': v['voice_id'], 'name': v['name'], 'category': v.get('category', '')}
            for v in raw.get('voices', [])
        ]
        voices.sort(key=lambda v: v['name'])
        return jsonify({'success': True, 'voices': voices})
    except Exception as e:
        logger.debug("list_elevenlabs_voices failed: %s", e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@user_settings_bp.route('/api/voice/sample', methods=['POST'])
def voice_sample():
    """Generate a short TTS preview clip and return the audio bytes directly."""
    try:
        from flask import Response
        data = request.get_json() or {}
        provider = data.get('provider', 'openai')
        voice = data.get('voice', 'nova')

        from app.services.text_to_speech import generate_sample
        audio_bytes = generate_sample(provider=provider, voice=voice)

        mime = 'audio/mpeg'
        return Response(audio_bytes, mimetype=mime, headers={
            'Content-Disposition': f'inline; filename="sample_{voice}.mp3"',
            'Cache-Control': 'no-store',
        })
    except Exception as e:
        logger.debug("voice_sample failed: %s", e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


# ── Wellness / tracked-activities configuration ────────────────────────────────

def _wellness_config_path():
    from app.assistant.utils.path_utils import get_app_root
    return get_app_root() / "assistant" / "pipelines" / "dayflow" / "step_configs" / "config_tracked_activities.json"


@user_settings_bp.route('/api/settings/wellness', methods=['GET'])
def get_wellness_config():
    """Return the current tracked-activities wellness config."""
    import json as _json
    path = _wellness_config_path()
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return jsonify({'success': True, 'config': data})
    except Exception as e:
        logger.debug("Failed to read wellness config: %s", e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@user_settings_bp.route('/api/settings/wellness', methods=['PUT'])
def update_wellness_config():
    """Update tracked-activities wellness config.

    Accepts a partial payload — only the ``activities`` dict is required.
    Each activity key maps to an object with at least ``display_name``,
    ``field_name``, and optionally ``threshold.minutes``.
    """
    import json as _json
    path = _wellness_config_path()
    try:
        current = _json.loads(path.read_text(encoding="utf-8"))
        incoming = request.json or {}

        if "activities" in incoming:
            current["activities"] = incoming["activities"]

        path.write_text(_json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        return jsonify({'success': True, 'config': current})
    except Exception as e:
        logger.debug("Failed to write wellness config: %s", e, exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500

