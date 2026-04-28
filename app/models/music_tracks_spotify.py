"""
Spotify music track table (large dataset).

This is intended to hold the contents of:
- data/music_data/spotify_data_weighted_nozero.csv

We keep schema close to the CSV so we can query/score efficiently later.
"""

from __future__ import annotations

from sqlalchemy import Column, Float, Index, Integer, String

from app.assistant.database.db_instance import db


class SpotifyMusicTrack(db.Model):
    __tablename__ = "music_tracks_spotify"

    id = Column(Integer, primary_key=True)

    # Identifiers / text fields
    track_id = Column(String(64), nullable=True, index=True)
    track_name = Column(String(500), nullable=False, index=True)
    artist_name = Column(String(500), nullable=False, index=True)
    genre = Column(String(128), nullable=True, index=True)

    # Weighting
    prob_factor = Column(Float, nullable=False, default=1.0, index=True)

    # Spotify-style audio features (native units from the CSV)
    danceability = Column(Float, nullable=True)
    energy = Column(Float, nullable=True, index=True)
    loudness = Column(Float, nullable=True)
    speechiness = Column(Float, nullable=True)
    acousticness = Column(Float, nullable=True)
    instrumentalness = Column(Float, nullable=True, index=True)
    liveness = Column(Float, nullable=True)
    valence = Column(Float, nullable=True, index=True)
    tempo = Column(Float, nullable=True)

    # Additional metadata from the CSV
    popularity = Column(Integer, nullable=True)
    year = Column(Integer, nullable=True, index=True)
    key = Column(Integer, nullable=True)
    mode = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    time_signature = Column(Integer, nullable=True)

    __table_args__ = (
        # Common query path: range filter by energy/valence and optional genre.
        Index("idx_music_tracks_spotify_genre_energy_valence", "genre", "energy", "valence"),
        Index("idx_music_tracks_spotify_energy_valence", "energy", "valence"),
        Index("idx_music_tracks_spotify_artist_track", "artist_name", "track_name"),
    )

