"""
User Settings Manager
Manages user preferences, API keys, and feature flags stored in user_settings.json
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class UserSettingsManager:
    """Manage user settings stored in JSON file"""
    
    def __init__(self, settings_file: Optional[str] = None):
        """
        Initialize the UserSettingsManager
        
        Args:
            settings_file: Path to settings file. If None, uses default location.
        """
        if settings_file is None:
            # Default to user_settings.json in user_settings_data subdirectory.
            # In the packaged/Docker deployment the file must live on the
            # persistent volume (EMI_DATA_DIR), not inside the image — an
            # image-local copy is silently discarded on every rebuild and
            # every feature toggle is lost. The repo-tree location is kept as
            # the dev default.
            emi_data_dir = os.environ.get("EMI_DATA_DIR")
            if emi_data_dir:
                settings_file = Path(emi_data_dir) / 'user_settings_data' / 'user_settings.json'
            else:
                settings_file = Path(__file__).parent / 'user_settings_data' / 'user_settings.json'
            self._maybe_migrate_image_copy(Path(settings_file) if settings_file else Path(__file__).parent / 'user_settings_data' / 'user_settings.json')
        
        self.settings_file = Path(settings_file)
        self._settings: Dict[str, Any] = {}
        self._load_settings()

    def _maybe_migrate_image_copy(self, data_dir_file: Path) -> None:
        """Copy an image-local settings file to the persistent volume on first boot.

        Older images stored user_settings.json next to the source file inside
        /app; the rebuild that moves it to EMI_DATA_DIR would otherwise discard
        the user's feature toggles. Only copies when the volume copy is absent
        and the image-local copy exists.
        """
        try:
            if data_dir_file.exists():
                return
            image_copy = Path(__file__).parent / 'user_settings_data' / 'user_settings.json'
            if not image_copy.exists():
                return
            data_dir_file.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(image_copy, data_dir_file)
            from app.assistant.utils.logging_config import get_logger
            get_logger(__name__).info(
                "Migrated settings from image-local copy to %s", data_dir_file
            )
        except Exception as e:
            from app.assistant.utils.logging_config import get_logger
            get_logger(__name__).warning(
                "Could not migrate image-local settings: %s", e
            )
    
    def _load_settings(self) -> None:
        """Load settings from JSON file"""
        from app.assistant.utils.logging_config import get_logger
        _logger = get_logger(__name__)

        if not self.settings_file.exists():
            _logger.info("Settings file not found at %s — creating defaults.", self.settings_file)
            self._create_default_settings()
            return

        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                self._settings = json.load(f)
        except json.JSONDecodeError as e:
            _logger.error(
                "Settings file at %s is malformed and cannot be parsed: %s",
                self.settings_file, e, exc_info=True,
            )
            raise RuntimeError(
                f"Settings file is corrupt: {self.settings_file}. "
                "Fix or delete it before starting."
            ) from e

        self._migrate_settings()

    def _migrate_settings(self) -> None:
        """Backfill keys that were added after the user's settings file was first created."""
        features = self._settings.setdefault("features", {})
        dirty = False

        forced_true = {"enable_kg"}
        for key in forced_true:
            if not features.get(key):
                features[key] = True
                dirty = True

        if dirty:
            self._save_settings()

    def reload_settings(self) -> None:
        """Reload settings from disk. Call this after external changes to the settings file."""
        self._load_settings()
    
    def _create_default_settings(self) -> None:
        """Create default settings file"""
        default_settings = {
            "_comment": "User Settings - Feature toggles and preferences (API keys are in environment variables)",
            "features": {
                "enable_email": True,
                "enable_calendar": True,
                "enable_tasks": True,
                "enable_weather": True,
                "enable_news": True,
                "enable_scheduler": True,
                "enable_kg": True,
                "enable_entity_cards": True,
                "enable_speak_mode": False
            },
            "preferences": {
                "theme": "dark",
                "default_llm_provider": "openai",
                "default_tts_provider": "elevenlabs",
                "default_stt_provider": "deepgram",
                "auto_inject_entities": True,
                "entity_card_confidence_threshold": 0.5
            },
            "api_settings": {
                "openai": {
                    "model": "gpt-4.1-mini",
                    "temperature": 0.7,
                    "max_tokens": 2000,
                    "tts_voice": "nova"
                },
                "anthropic": {
                    "model": "claude-3-sonnet-20240229",
                    "temperature": 0.7,
                    "max_tokens": 2000
                },
                "gemini": {
                    "tts_voice": "Kore",
                    "tts_model": "gemini-2.5-flash-preview-tts"
                },
                "elevenlabs": {
                    "voice_id": "",
                    "model_id": "eleven_monolingual_v1"
                },
                "deepgram": {
                    "model": "nova-2",
                    "language": "en-US"
                }
            },
            "user_info": {
                "name": "",
                "timezone": "UTC",
                "location": ""
            },
            "quiet_mode": {
                "enabled": False,
                "universal_hours": {
                    "start": "23:00",
                    "end": "07:00"
                },
                "per_feature": {
                    "email": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    },
                    "news": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    },
                    "calendar": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    },
                    "tasks": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    },
                    "scheduler": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    },
                    "weather": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    },
                    "kg": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    },
                    "entity_cards": {
                        "enabled": False,
                        "start": "23:00",
                        "end": "07:00"
                    }
                }
            },
            "logging": {
                "_comment": "Runtime logging controls (Dev UI). console_level affects terminal output; overrides are per-logger.",
                "console_level": "WARNING",
                "overrides": {}
            },
            "prompt_debug": {
                "_comment": "Per-agent prompt printing controls (system/user).",
                "default": {
                    "system": False,
                    "user": False,
                    "results": False
                },
                "agents": {}
            },
            "system": {
                "first_run": True,
                "setup_complete": False,
                "version": "1.0.0",
                "last_updated": datetime.now().isoformat()
            }
        }
        
        self._settings = default_settings
        self._save_settings()
    
    def _save_settings(self) -> None:
        """Save settings to JSON file"""
        # Update last_updated timestamp
        if 'system' in self._settings:
            self._settings['system']['last_updated'] = datetime.now().isoformat()
        
        # Ensure directory exists
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            from app.assistant.utils.logging_config import get_logger
            get_logger(__name__).error(
                "Failed to save settings to %s: %s", self.settings_file, e, exc_info=True
            )
            raise
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Get a setting value using dot notation
        
        Args:
            key_path: Path to setting (e.g., "api_keys.openai_api_key" or "features.enable_kg")
            default: Default value if key not found
            
        Returns:
            The setting value or default
            
        Example:
            >>> settings.get('api_keys.openai_api_key')
            >>> settings.get('features.enable_kg', True)
        """
        keys = key_path.split('.')
        value = self._settings
        
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        
        # Override with environment variable if exists (for API keys)
        if 'api_key' in key_path:
            env_key = key_path.split('.')[-1].upper()
            env_value = os.environ.get(env_key)
            if env_value:
                return env_value
        
        return value
    
    def set(self, key_path: str, value: Any, save: bool = True) -> None:
        """
        Set a setting value using dot notation
        
        Args:
            key_path: Path to setting (e.g., "api_keys.openai_api_key")
            value: Value to set
            save: Whether to save immediately to file
            
        Example:
            >>> settings.set('api_keys.openai_api_key', 'sk-...')
            >>> settings.set('features.enable_kg', False)
        """
        keys = key_path.split('.')
        current = self._settings
        
        # Navigate to the parent dictionary
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        
        # Set the value
        current[keys[-1]] = value
        
        if save:
            self._save_settings()
    
    def get_api_key(self, provider: str) -> Optional[str]:
        """
        Get API key for a specific provider from environment variables ONLY
        
        Args:
            provider: Provider name (e.g., 'openai', 'google', 'anthropic')
            
        Returns:
            API key or None if not set
        """
        # Always check environment variable (never store API keys in JSON)
        env_key = f"{provider.upper()}_API_KEY"
        return os.environ.get(env_key)
    
    def has_api_key(self, provider: str) -> bool:
        """
        Check if API key exists for a provider (without exposing the key)
        
        Args:
            provider: Provider name (e.g., 'openai', 'google')
            
        Returns:
            True if API key is set, False otherwise
        """
        api_key = self.get_api_key(provider)
        return bool(api_key and api_key.strip())
    
    def has_google_oauth_credentials(self) -> bool:
        """
        Check if Google OAuth credentials exist for the default account.

        The OAuth account store is the source of truth — a row there with
        `is_active = True` means the user has connected their Google account
        through the modern integrations flow. The legacy
        `lib/credentials/credentials.json` file is no longer required and
        was the cause of the integrations-page-vs-features-page disagreement.

        Returns:
            True if OAuth credentials exist for DEFAULT_GOOGLE_ACCOUNT_ID,
            False otherwise.
        """
        try:
            from app.assistant.lib.google_auth.account_ids import DEFAULT_GOOGLE_ACCOUNT_ID
            from app.assistant.lib.google_auth.oauth_account_store import get_google_oauth_account_store

            store = get_google_oauth_account_store()
            row = store.get_credentials(account_id=DEFAULT_GOOGLE_ACCOUNT_ID)
            return row is not None
        except Exception as e:
            from app.assistant.utils.logging_config import get_logger

            logger = get_logger(__name__)
            logger.error("Error checking OAuth credentials: %s", e)
            logger.debug("has_google_oauth_credentials exception details", exc_info=True)
            return False
    
    def is_feature_enabled(self, feature: str) -> bool:
        """
        Check if a feature is enabled.
        Always reads fresh from disk to respect real-time setting changes.
        
        Args:
            feature: Feature name (e.g., 'kg', 'taxonomy', 'entity_cards')
            
        Returns:
            True if enabled, False otherwise
        """
        # Reload settings to get latest values (in case changed via UI)
        self._load_settings()
        key_path = f"features.enable_{feature}"
        return self.get(key_path, False)
    
    def enable_feature(self, feature: str, enabled: bool = True) -> None:
        """
        Enable or disable a feature
        
        Args:
            feature: Feature name (e.g., 'kg', 'taxonomy')
            enabled: True to enable, False to disable
        """
        key_path = f"features.enable_{feature}"
        self.set(key_path, enabled)
    
    def get_preference(self, pref_name: str, default: Any = None) -> Any:
        """Get a user preference"""
        return self.get(f"preferences.{pref_name}", default)
    
    def set_preference(self, pref_name: str, value: Any) -> None:
        """Set a user preference"""
        self.set(f"preferences.{pref_name}", value)
    
    def get_all_settings(self) -> Dict[str, Any]:
        """
        Get all settings
        
        Returns:
            Complete settings dictionary
        """
        return self._settings.copy()
    
    def get_public_settings(self) -> Dict[str, Any]:
        """
        Get settings safe to expose to frontend
        Includes API key status from environment variables
        
        Returns:
            Settings dictionary with API key availability status
        """
        public_settings = self._settings.copy()
        
        # Add API key status from environment variables
        public_settings['api_keys_status'] = {
            'openai': self.has_api_key('openai'),
            'google': self.has_api_key('google'),
            'anthropic': self.has_api_key('anthropic'),
            'opencode': self.has_api_key('opencode'),
            'elevenlabs': self.has_api_key('elevenlabs'),
            'deepgram': self.has_api_key('deepgram')
        }
        
        return public_settings
    
    def get_feature_dependencies(self) -> Dict[str, Dict[str, Any]]:
        """
        Get feature dependencies and requirements
        
        Returns:
            Dictionary mapping features to their dependencies
        """
        return {
            'email': {
                'requires_api_keys': [],
                'requires_oauth': ['google'],
                'requires_features': [],
                'description': 'Email fetching requires Google OAuth credentials'
            },
            'calendar': {
                'requires_api_keys': [],
                'requires_oauth': ['google'],
                'requires_features': [],
                'description': 'Calendar requires Google OAuth credentials'
            },
            'tasks': {
                'requires_api_keys': [],
                'requires_oauth': ['google'],
                'requires_features': [],
                'description': 'Tasks requires Google OAuth credentials'
            },
            'weather': {
                'requires_api_keys': [],
                'requires_oauth': [],
                'requires_features': [],
                'description': 'Weather fetching (no API key required)'
            },
            'news': {
                'requires_api_keys': [],
                'requires_oauth': [],
                'requires_features': [],
                'description': 'News fetching (no API key required)'
            },
            'scheduler': {
                'requires_api_keys': [],
                'requires_oauth': [],
                'requires_features': [],
                'description': 'Scheduler events (no API key required)'
            },
            'kg': {
                # The LLM requirement is satisfied by whichever provider is
                # actually configured (opencode in this deployment, openai for
                # others). 'llm' is resolved at check time to the active
                # DEFAULT_LLM_PROVIDER key.
                'requires_api_keys': ['llm'],
                'requires_oauth': [],
                'requires_features': [],
                'description': 'Knowledge graph requires an LLM API key for processing'
            },
            'entity_cards': {
                'requires_api_keys': ['llm'],
                'requires_oauth': [],
                'requires_features': [],
                'description': 'Entity cards require an LLM API key for extraction'
            },
            'speak_mode': {
                'requires_api_keys': ['elevenlabs', 'deepgram'],
                'requires_oauth': [],
                'requires_features': [],
                'description': 'Speak mode requires ElevenLabs (TTS) and Deepgram (STT) APIs'
            }
        }
    
    def check_feature_can_be_enabled(self, feature: str) -> Dict[str, Any]:
        """
        Check if a feature can be enabled based on dependencies
        
        Args:
            feature: Feature name (e.g., 'daily_summary', 'auto_planner')
            
        Returns:
            Dictionary with 'can_enable', 'missing_api_keys', 'missing_oauth', 'missing_features', 'reason'
        """
        dependencies = self.get_feature_dependencies()
        
        if feature not in dependencies:
            return {
                'can_enable': True,
                'missing_api_keys': [],
                'missing_oauth': [],
                'missing_features': [],
                'reason': 'No dependencies'
            }
        
        deps = dependencies[feature]
        missing_api_keys = []
        missing_oauth = []
        missing_features = []
        
        # Check API key requirements
        for api_key_provider in deps.get('requires_api_keys', []):
            # 'llm' is a virtual provider: the requirement is satisfied by
            # whichever provider is the active default (DEFAULT_LLM_PROVIDER),
            # falling back to any configured LLM key.
            if api_key_provider == 'llm':
                active = (os.environ.get("DEFAULT_LLM_PROVIDER") or "").strip() or "openai"
                if self.get_api_key(active):
                    continue
                # Fall back to any known LLM provider key so the feature can
                # still enable when a key exists but the default is unset.
                if any(self.get_api_key(p) for p in ("opencode", "openai", "anthropic", "gemini", "google")):
                    continue
                missing_api_keys.append("llm")
                continue
            if not self.get_api_key(api_key_provider):
                missing_api_keys.append(api_key_provider)
        
        # Check OAuth requirements
        for oauth_provider in deps.get('requires_oauth', []):
            if oauth_provider == 'google' and not self.has_google_oauth_credentials():
                missing_oauth.append(oauth_provider)
        
        # Check feature requirements
        for required_feature in deps.get('requires_features', []):
            if not self.is_feature_enabled(required_feature):
                missing_features.append(required_feature)
        
        can_enable = len(missing_api_keys) == 0 and len(missing_oauth) == 0 and len(missing_features) == 0
        
        reason = None
        if not can_enable:
            reason_parts = []
            if missing_api_keys:
                reason_parts.append(f"Missing API keys: {', '.join(missing_api_keys)}")
            if missing_oauth:
                reason_parts.append(f"Missing OAuth credentials: {', '.join(missing_oauth)}")
            if missing_features:
                reason_parts.append(f"Required features disabled: {', '.join(missing_features)}")
            reason = '; '.join(reason_parts)
        
        return {
            'can_enable': can_enable,
            'missing_api_keys': missing_api_keys,
            'missing_oauth': missing_oauth,
            'missing_features': missing_features,
            'reason': reason,
            'description': deps.get('description', '')
        }
    
    def get_available_features(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all features and their availability status
        
        Returns:
            Dictionary mapping feature names to availability info
        """
        features = self.get('features', {})
        result = {}
        
        for feature_key, enabled in features.items():
            # Extract feature name (remove 'enable_' prefix)
            feature_name = feature_key.replace('enable_', '')
            
            # Check if it can be enabled
            check_result = self.check_feature_can_be_enabled(feature_name)
            
            result[feature_name] = {
                'currently_enabled': enabled,
                'can_enable': check_result['can_enable'],
                'missing_api_keys': check_result['missing_api_keys'],
                'missing_oauth': check_result['missing_oauth'],
                'missing_features': check_result['missing_features'],
                'reason': check_result['reason'],
                'description': check_result.get('description', '')
            }
        
        return result
    
    def validate_settings(self) -> Dict[str, list]:
        """
        Validate settings and return any issues
        
        Returns:
            Dictionary with 'errors' and 'warnings' lists
        """
        errors = []
        warnings = []
        
        # Check if critical API keys are set
        if not self.get_api_key('openai'):
            warnings.append("OpenAI API key is not set. KG and Entity Cards will not work.")

        if not self.has_google_oauth_credentials():
            warnings.append("Google OAuth credentials are not set. Email, Calendar, and Tasks will not work.")

        entity_threshold = self.get('preferences.entity_card_confidence_threshold', 0.5)
        if not 0 <= entity_threshold <= 1:
            errors.append("Entity card confidence threshold must be between 0 and 1")
        
        # Check for features enabled without required API keys/OAuth
        features_status = self.get_available_features()
        for feature_name, status in features_status.items():
            if status['currently_enabled'] and not status['can_enable']:
                warnings.append(
                    f"Feature '{feature_name}' is enabled but cannot function: {status['reason']}"
                )
        
        return {
            'errors': errors,
            'warnings': warnings
        }
    
    def reset_to_defaults(self) -> None:
        """Reset all settings to default values"""
        self._create_default_settings()
    
    def is_quiet_mode_active(self, feature: Optional[str] = None) -> bool:
        """
        Check if quiet mode is currently active for a feature.
        Always reads fresh from disk to respect real-time setting changes.
        
        Args:
            feature: Feature name (email, news, calendar, tasks, scheduler, weather).
                    If None, checks universal quiet mode.
        
        Returns:
            True if quiet mode is active, False otherwise
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from app.assistant.utils.logging_config import get_logger
        
        logger = get_logger(__name__)
        
        # Reload settings to get latest values (in case changed via UI)
        self._load_settings()
        
        quiet_settings = self.get('quiet_mode', {})
        
        logger.info(f"🔍 QUIET MODE CHECK for feature='{feature}'")
        logger.info(f"   Global quiet_mode.enabled = {quiet_settings.get('enabled', False)}")
        
        # If quiet mode is globally disabled, return False
        if not quiet_settings.get('enabled', False):
            logger.info(f"   ➜ Result: FALSE (global quiet mode disabled)")
            return False
        
        # Get timezone from user settings
        timezone_str = self.get('user_info.timezone', 'America/Los_Angeles')
        try:
            tz = ZoneInfo(timezone_str)
        except Exception:
            logger.error(
                "Invalid timezone '%s' in user_info.timezone — falling back to UTC. "
                "Fix this in settings.",
                timezone_str,
            )
            tz = ZoneInfo('UTC')
        
        now = datetime.now(tz)
        current_time = now.time()
        logger.info(f"   Current time: {current_time.strftime('%H:%M')} ({timezone_str})")
        
        # Check per-feature quiet hours if feature is specified
        if feature:
            per_feature = quiet_settings.get('per_feature', {}).get(feature, {})
            logger.info(f"   Per-feature settings for '{feature}': {per_feature}")
            
            if per_feature.get('enabled', False):
                # Use feature-specific hours
                start_str = per_feature.get('start', '23:00')
                end_str = per_feature.get('end', '07:00')
                logger.info(f"   Using PER-FEATURE hours: {start_str} - {end_str}")
            else:
                # Use universal hours
                universal = quiet_settings.get('universal_hours', {})
                start_str = universal.get('start', '23:00')
                end_str = universal.get('end', '07:00')
                logger.info(f"   Using UNIVERSAL hours: {start_str} - {end_str}")
        else:
            # Use universal hours
            universal = quiet_settings.get('universal_hours', {})
            start_str = universal.get('start', '23:00')
            end_str = universal.get('end', '07:00')
            logger.info(f"   Using UNIVERSAL hours (no feature specified): {start_str} - {end_str}")
        
        # Parse time strings
        start_hour, start_min = map(int, start_str.split(':'))
        end_hour, end_min = map(int, end_str.split(':'))
        
        start_time = datetime.strptime(start_str, '%H:%M').time()
        end_time = datetime.strptime(end_str, '%H:%M').time()
        
        # Check if current time is in quiet hours
        if start_time <= end_time:
            # Same day range (e.g., 09:00 - 17:00)
            is_quiet = start_time <= current_time <= end_time
            logger.info(f"   Same-day range check: {start_str} <= {current_time.strftime('%H:%M')} <= {end_str}")
        else:
            # Overnight range (e.g., 23:00 - 07:00)
            is_quiet = current_time >= start_time or current_time <= end_time
            logger.info(f"   Overnight range check: {current_time.strftime('%H:%M')} >= {start_str} OR {current_time.strftime('%H:%M')} <= {end_str}")
        
        logger.info(f"   ➜ Result: {is_quiet}")
        
        return is_quiet
    
    def export_settings(self, export_path: str, include_api_keys: bool = False) -> None:
        """
        Export settings to a file
        
        Args:
            export_path: Path to export file
            include_api_keys: Whether to include API keys in export
        """
        export_data = self.get_public_settings() if not include_api_keys else self._settings.copy()
        
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
    
    def import_settings(self, import_path: str, merge: bool = True) -> None:
        """
        Import settings from a file
        
        Args:
            import_path: Path to import file
            merge: If True, merge with existing settings. If False, replace completely.
        """
        with open(import_path, 'r', encoding='utf-8') as f:
            imported_settings = json.load(f)
        
        if merge:
            # Deep merge the settings
            self._deep_merge(self._settings, imported_settings)
        else:
            self._settings = imported_settings
        
        self._save_settings()
    
    def _deep_merge(self, base: dict, update: dict) -> None:
        """Deep merge update dict into base dict"""
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value


# Global instance
_settings_manager: Optional[UserSettingsManager] = None


def get_settings_manager() -> UserSettingsManager:
    """Get the global settings manager instance"""
    global _settings_manager
    if _settings_manager is None:
        _settings_manager = UserSettingsManager()
    return _settings_manager


def reload_settings() -> None:
    """Reload settings from file"""
    global _settings_manager
    _settings_manager = UserSettingsManager()


# Convenience functions
def get_setting(key_path: str, default: Any = None) -> Any:
    """Get a setting value"""
    return get_settings_manager().get(key_path, default)


def set_setting(key_path: str, value: Any) -> None:
    """Set a setting value"""
    get_settings_manager().set(key_path, value)


def get_api_key(provider: str) -> Optional[str]:
    """Get API key for a provider"""
    return get_settings_manager().get_api_key(provider)


def is_feature_enabled(feature: str) -> bool:
    """Check if a feature is enabled"""
    return get_settings_manager().is_feature_enabled(feature)


def can_run_feature(feature: str) -> bool:
    """
    Check if a feature can run (enabled AND has required API keys)
    
    Args:
        feature: Feature name (e.g., 'email', 'daily_summary', 'kg')
        
    Returns:
        True if feature is enabled and has all required API keys
    """
    settings = get_settings_manager()
    
    # Check if feature is enabled
    if not settings.is_feature_enabled(feature):
        return False
    
    # Check if required API keys are available
    check_result = settings.check_feature_can_be_enabled(feature)
    return check_result['can_enable']

