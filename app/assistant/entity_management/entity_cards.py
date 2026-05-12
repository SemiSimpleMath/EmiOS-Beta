"""V1 entity_cards bridge module.

The v1 EntityCard ORM and its forty-plus helpers were retired on 2026-05-10.
This module exists only to provide forward-compatible names for the handful
of callers we haven't yet ported to import directly from
``entity_management.entity_card_v2``. New code should not use this module.

Exports:
  - ``get_entity_card_for_prompt_injection_level(session, name, level)`` —
    thin wrapper around the v2 renderer with the same signature.
  - ``track_entity_card_usage_best_effort(...)`` — no-op (v1's usage table
    is gone; we don't currently track v2 usage).
"""
from __future__ import annotations

from typing import Any

from app.assistant.entity_management.entity_card_v2 import (
    render_v2_card_for_prompt_injection_level,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def get_entity_card_for_prompt_injection_level(
    session: Any,
    entity_name: str,
    *,
    level: int = 0,
) -> str:
    """Level-based prompt injection formatter, backed by entity_card_v2."""
    return render_v2_card_for_prompt_injection_level(session, entity_name, level=level)


def track_entity_card_usage_best_effort(**kwargs) -> None:
    """No-op shim.

    v1's `entity_card_usage` table was retired with the rest of v1. If you
    want per-entity injection tracking in v2, wire it through a fresh table —
    don't resurrect this function.
    """
    return None
