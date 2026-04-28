"""
DJ Manager Module
=================
Manages automated music playback based on user activity, mood, and preferences.

Architecture:
- manager.py: Event-driven state machine (DJManager)
- events.py: Event dataclasses for the state machine
- socket_client.py: Thin adapter around socket manager
- history.py: Play history recording
- vibe.py: Vibe plan management and interpolation
- selector.py: Candidate scoring and backup queue
"""

from typing import Optional
from app.assistant.dj_manager.manager import DJManager

__all__ = ['DJManager', 'get_dj_manager']

# Singleton instance
_dj_manager: Optional[DJManager] = None


def get_dj_manager(app=None) -> DJManager:
    """Get or create the DJManager singleton."""
    global _dj_manager
    if _dj_manager is None:
        _dj_manager = DJManager(app=app)
    return _dj_manager
