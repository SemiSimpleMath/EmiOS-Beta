"""
Normalized music weight tables (distinct weights for genre/artist/track).

Rationale:
- Storing "genre weight" on every row (925k+) duplicates data.
- Artist/track weights should be sparse and adjustable without rewriting the big table.

All factors are unbounded positive floats. We treat them as weights/multipliers.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Index, String

from app.assistant.database.db_instance import db


class MusicGenreWeight(db.Model):
    __tablename__ = "music_genre_weights"

    genre = Column(String(128), primary_key=True)  # normalized lowercase genre
    factor = Column(Float, nullable=False, default=1.0)
    updated_at_utc = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("idx_music_genre_weights_updated", "updated_at_utc"),)


class MusicArtistWeight(db.Model):
    __tablename__ = "music_artist_weights"

    artist = Column(String(500), primary_key=True)  # normalized lowercase artist
    factor = Column(Float, nullable=False, default=1.0)
    updated_at_utc = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("idx_music_artist_weights_updated", "updated_at_utc"),)


class MusicTrackWeight(db.Model):
    __tablename__ = "music_track_weights"

    # normalized key: "<title>|||<artist>"
    track_key = Column(String(700), primary_key=True)

    # stored for convenience/debugging (not required for joins)
    title = Column(String(500), nullable=True)
    artist = Column(String(500), nullable=True)

    factor = Column(Float, nullable=False, default=1.0)
    updated_at_utc = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("idx_music_track_weights_updated", "updated_at_utc"),)

