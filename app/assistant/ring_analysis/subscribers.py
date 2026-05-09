"""Compatibility shim — wires the camera dispatcher onto event_hub.

Behavior moved into:
  - camera_registry.py        : declarative camera config loader
  - camera_dispatcher.py      : single dispatcher subscriber + per-camera storage
                                + analyzer routing + pod minting
  - camera_post_handlers.py   : named functions referenced by registry
                                (e.g. bedroom_emergency_alarm)
  - configs/cameras.json      : the registry itself

Adding a new camera no longer requires editing this file.
"""
from __future__ import annotations

from app.assistant.ring_analysis.camera_dispatcher import register_camera_dispatcher
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def register_ring_subscribers() -> None:
    """Wire camera-snapshot subscribers onto event_hub. Call once at startup."""
    register_camera_dispatcher()
