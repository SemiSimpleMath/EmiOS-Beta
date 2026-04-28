"""
Music weight overrides (per genre/artist/track).

These allow the UI to nudge probability down (or up later) without rewriting
millions of rows in the main dataset table.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Index, String
from sqlalchemy.schema import PrimaryKeyConstraint

from app.assistant.database.db_instance import db


class MusicWeightOverride(db.Model):
    __tablename__ = "music_weight_overrides"

    # scope: "genre" | "artist" | "track"
    scope = Column(String(16), nullable=False)

    # normalized key (lowercase). For "track" scope we use: "<title>|||<artist>"
    key = Column(String(700), nullable=False)

    # multiplier applied to the base prob_factor (>= 0)
    factor = Column(Float, nullable=False, default=1.0)

    updated_at_utc = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        PrimaryKeyConstraint("scope", "key", name="pk_music_weight_overrides"),
        Index("idx_music_weight_overrides_scope", "scope"),
        Index("idx_music_weight_overrides_updated", "updated_at_utc"),
    )

