"""Learned taste weights, loaded once per pick and composed into sampling.

The music_{track,artist,genre}_weights tables are the user's recorded taste —
written by the player's boost/ban buttons (and, since 2026-09, by implicit
playback signals). For seven months they were WRITE-ONLY for selection: the
only runtime reader decorated the UI payload, so a banned artist (factor 0.0)
sampled exactly like a loved one. This module is the read side the tables
never had.

Deterministic joins only: normalized keys, no wording heuristics. Track keys
exist in two generations — legacy "<title>|||<artist>" rows (2026-02, written
by the old adjust route) and current "spotify:<track_id>" rows — so the track
lookup checks both. All lookups are dict hits against three tiny tables
(~130 rows) loaded in one pass; nothing touches the DB per-song.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Set

from sqlalchemy import text

from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


@dataclass
class LearnedWeights:
    """In-memory snapshot of the three weight tables."""
    track: Dict[str, float] = field(default_factory=dict)    # "spotify:<id>" AND "<title>|||<artist>"
    artist: Dict[str, float] = field(default_factory=dict)   # normalized artist
    genre: Dict[str, float] = field(default_factory=dict)    # normalized genre

    def factor_for(self, *, track_id: str, title: str, artist: str, genre: str) -> float:
        """track_w × artist_w × genre_w for one song; 1.0 where nothing is recorded.

        A 0.0 anywhere (a Ban) zeroes the product — the sampler's weighted
        draw then never selects the song, which is what Ban always promised.
        """
        t_w = self.track.get(f"spotify:{_norm(track_id)}") if track_id else None
        if t_w is None:
            t_w = self.track.get(f"{_norm(title)}|||{_norm(artist)}")
        a_w = self.artist.get(_norm(artist))
        g_w = self.genre.get(_norm(genre))
        out = 1.0
        for w in (t_w, a_w, g_w):
            if w is not None:
                out *= max(0.0, float(w))
        return out

    def preferred_genres(self, threshold: float = 1.0) -> Set[str]:
        """Genres the user has boosted above neutral — the data-driven
        replacement for the old hardcoded boost_genres lists."""
        return {g for g, f in self.genre.items() if float(f) > threshold}


def load_learned_weights() -> LearnedWeights:
    """One pass over the three tables. Raises on DB failure — the caller
    decides whether a pick without taste is acceptable, and says so loudly."""
    lw = LearnedWeights()
    session = get_session()
    try:
        for k, f in session.execute(text("SELECT track_key, factor FROM music_track_weights")):
            lw.track[str(k)] = float(f)
        for a, f in session.execute(text("SELECT artist, factor FROM music_artist_weights")):
            lw.artist[_norm(str(a))] = float(f)
        for g, f in session.execute(text("SELECT genre, factor FROM music_genre_weights")):
            lw.genre[_norm(str(g))] = float(f)
    finally:
        session.close()
    return lw
