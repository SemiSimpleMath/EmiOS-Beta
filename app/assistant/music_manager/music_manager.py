"""
Music Manager
=============
Handles Apple MusicKit integration including JWT token generation and playback state.
"""

import os
import jwt
import time
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

# Apple MusicKit credentials — configure via environment variables or .env
MUSICKIT_KEY_ID = os.getenv("MUSICKIT_KEY_ID", "")
MUSICKIT_TEAM_ID = os.getenv("MUSICKIT_TEAM_ID", "")
_p8_path_env = os.getenv("MUSICKIT_P8_PATH")
MUSICKIT_P8_PATH = Path(_p8_path_env) if _p8_path_env else get_repo_root() / "musickit" / f"AuthKey_{MUSICKIT_KEY_ID}.p8"

# Token validity
# Apple allows developer tokens up to 6 months. Using a long TTL prevents
# "Initialized with an expired token" during long-running browser sessions.
# 180 days = 15552000 seconds.
TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 180


class MusicManager:
    """
    Manages Apple MusicKit integration.
    
    Responsibilities:
    - Generate JWT developer tokens for MusicKit
    - Track playback state
    - Handle WebSocket commands from agents/UI
    """
    
    def __init__(self):
        self._private_key: Optional[str] = None
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0
        
        # Playback state (updated by frontend via WebSocket)
        self.playback_state: Dict[str, Any] = {
            "is_playing": False,
            "current_track": None,
            "queue": [],
            "progress_seconds": 0,
            "duration_seconds": 0,
            "volume": 1.0,
            "last_update_utc": None,
        }
        
        self._load_private_key()

        # Logging throttles (reduce noisy state spam)
        self._last_logged_playing: Optional[bool] = None
        self._last_logged_track: Optional[str] = None  # "title|||artist"
    
    def _load_private_key(self) -> None:
        """Load the .p8 private key from disk."""
        missing_vars = [v for v in ("MUSICKIT_KEY_ID", "MUSICKIT_TEAM_ID") if not os.getenv(v)]
        if missing_vars:
            logger.error(
                "Apple MusicKit is not configured. Missing environment variables: %s. "
                "Set these in your .env file — see .env.example for instructions.",
                ", ".join(missing_vars),
            )
            return

        try:
            if not MUSICKIT_P8_PATH.exists():
                logger.error(
                    "MusicKit private key not found at %s. "
                    "Download your AuthKey_<KEY_ID>.p8 from "
                    "https://developer.apple.com/account/resources/authkeys/list "
                    "and place it at that path, or set MUSICKIT_P8_PATH in .env.",
                    MUSICKIT_P8_PATH,
                )
                return

            self._private_key = MUSICKIT_P8_PATH.read_text()
            logger.info("Loaded MusicKit private key from %s", MUSICKIT_P8_PATH)
        except Exception as e:
            logger.error("Failed to load MusicKit private key: %s", e)
            logger.debug("failed to load MusicKit private key exception details", exc_info=True)
    
    def generate_developer_token(self, origin: Optional[str] = None) -> Optional[str]:
        """
        Generate a JWT developer token for Apple MusicKit.
        
        Args:
            origin: Optional origin claim for web tokens (e.g., "http://localhost:5000")
        
        Returns:
            JWT token string or None if generation fails
        """
        if not self._private_key:
            logger.error("Cannot generate token: private key not loaded")
            return None
        
        # Check if we have a valid cached token (with 5 min buffer)
        if self._cached_token and time.time() < (self._token_expiry - 300):
            logger.debug("Using cached MusicKit token")
            return self._cached_token
        
        try:
            now = int(time.time())
            exp = now + TOKEN_EXPIRY_SECONDS
            
            # JWT claims per Apple MusicKit documentation
            claims = {
                "iss": MUSICKIT_TEAM_ID,
                "iat": now,
                "exp": exp,
            }
            
            # Add origin claim for web tokens if provided
            if origin:
                claims["origin"] = origin
            
            # JWT header
            headers = {
                "alg": "ES256",
                "kid": MUSICKIT_KEY_ID,
            }
            
            token = jwt.encode(
                claims,
                self._private_key,
                algorithm="ES256",
                headers=headers,
            )
            
            self._cached_token = token
            self._token_expiry = exp
            
            logger.info(f"🎵 Generated new MusicKit developer token (expires in {TOKEN_EXPIRY_SECONDS}s)")
            return token
            
        except Exception as e:
            logger.error("Failed to generate MusicKit token: %s", e)
            logger.debug("failed to generate MusicKit token exception details", exc_info=True)
            return None
    
    def update_playback_state(self, state: Dict[str, Any]) -> None:
        """
        Update the playback state from frontend.
        
        Args:
            state: Dictionary with playback state fields
        """
        self.playback_state.update(state)
        self.playback_state["last_update_utc"] = datetime.now(timezone.utc).isoformat()

        # Log only meaningful changes (track change / play-pause), not every heartbeat.
        try:
            playing = bool(state.get("is_playing")) if "is_playing" in state else None
            ct = state.get("current_track") if isinstance(state.get("current_track"), dict) else None
            title = (ct.get("title") or "").strip() if ct else ""
            artist = (ct.get("artist") or "").strip() if ct else ""
            track_key = f"{title}|||{artist}" if (title or artist) else ""

            if playing is not None and playing != self._last_logged_playing:
                logger.info(f"🎵 Playback {'playing' if playing else 'paused'}")
                self._last_logged_playing = playing

            if track_key and track_key != self._last_logged_track:
                logger.info(f"🎵 Now playing: {title} — {artist}")
                self._last_logged_track = track_key
        except Exception:
            # Never let logging logic break state updates
            pass
    
    def get_playback_state(self) -> Dict[str, Any]:
        """Get current playback state."""
        return self.playback_state.copy()
    
    def is_configured(self) -> bool:
        """Check if MusicKit is properly configured."""
        return self._private_key is not None


# Singleton instance
_music_manager: Optional[MusicManager] = None


def get_music_manager() -> MusicManager:
    """Get or create the MusicManager singleton."""
    global _music_manager
    if _music_manager is None:
        _music_manager = MusicManager()
    return _music_manager
