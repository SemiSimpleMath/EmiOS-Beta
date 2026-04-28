"""
Auxiliary genre statistics for the Spotify music table.

Purpose:
- Provide `track_count` per genre so sampling can normalize by genre size.
- Keeps normalization data separate from the large `music_tracks_spotify` table.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String

from app.assistant.database.db_instance import db


class MusicGenreStats(db.Model):
    __tablename__ = "music_genre_stats"

    # normalized lowercase genre (match MusicGenreWeight.genre)
    genre = Column(String(128), primary_key=True)

    track_count = Column(Integer, nullable=False, default=0)
    updated_at_utc = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

