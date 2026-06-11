"""
situation_brief.py — Schema, storage and retrieval for situation briefs.

A SituationBrief is the output of the context_activation stage: a structured
record that captures the KG convergence results and the LLM-synthesised dossier
for a specific activation event.

Storage:
    Briefs are written as JSON files under:
        resources/context_engine/<owner_id>/situation_briefs/<brief_id>.json

    The latest brief per owner is also written to a lightweight pointer file:
        resources/context_engine/<owner_id>/situation_brief_latest.json

    This design keeps the brief retrievable by the reasoning_agent stage and
    by the room_session_manager without coupling them to the pipeline run.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from app.assistant.utils.atomic_write import write_text_atomic
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SituationBrief(BaseModel):
    brief_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:16])
    owner_id: str
    created_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Activation inputs
    user_message: str
    seeds: List[str]

    # Stage outputs
    kg_briefing: str
    situation_brief: str       # LLM-synthesised markdown dossier from context_activation
    reasoning_output: Optional[str] = None  # populated by reasoning_agent stage

    # Timing
    activation_elapsed_seconds: Optional[float] = None
    reasoning_elapsed_seconds: Optional[float] = None


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Resolve repository root (two levels above app/)."""
    return Path(__file__).resolve().parents[3]


def _briefs_dir(owner_id: str) -> Path:
    return _repo_root() / "resources" / "context_engine" / owner_id / "situation_briefs"


def _latest_pointer_path(owner_id: str) -> Path:
    return _repo_root() / "resources" / "context_engine" / owner_id / "situation_brief_latest.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_brief(brief: SituationBrief) -> Path:
    """
    Persist the brief to disk.

    Writes:
        - <briefs_dir>/<brief_id>.json  — full record
        - <owner_dir>/situation_brief_latest.json — pointer to latest

    Returns:
        Path to the full brief file.
    """
    brief_path = _briefs_dir(brief.owner_id) / f"{brief.brief_id}.json"
    payload = brief.model_dump_json(indent=2)
    write_text_atomic(brief_path, payload)

    pointer = {
        "brief_id": brief.brief_id,
        "created_at_utc": brief.created_at_utc,
        "user_message": brief.user_message,
        "seeds": brief.seeds,
        "brief_path": str(brief_path),
    }
    write_text_atomic(_latest_pointer_path(brief.owner_id), json.dumps(pointer, indent=2))

    logger.debug("situation_brief: saved brief_id=%s path=%s", brief.brief_id, brief_path)
    return brief_path


def load_latest_brief(owner_id: str) -> Optional[SituationBrief]:
    """
    Load the most recently saved SituationBrief for the given owner.

    Returns None if no brief exists yet.
    """
    pointer_path = _latest_pointer_path(owner_id)
    if not pointer_path.exists():
        return None

    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        brief_path = Path(pointer["brief_path"])
        if not brief_path.exists():
            logger.debug("situation_brief: pointer points to missing file %s", brief_path)
            return None
        return SituationBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("situation_brief: could not load latest brief: %s", e, exc_info=True)
        raise


def load_brief_by_id(owner_id: str, brief_id: str) -> SituationBrief:
    """Load a specific brief by its ID. Raises FileNotFoundError if not found."""
    brief_path = _briefs_dir(owner_id) / f"{brief_id}.json"
    if not brief_path.exists():
        raise FileNotFoundError(f"No brief found at {brief_path}")
    return SituationBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
