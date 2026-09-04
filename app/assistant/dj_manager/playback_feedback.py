"""Turn playback reactions into implicit taste — the quiet learning channel.

The boost/ban buttons are the LOUD channel (explicit, large moves). This is the
quiet one: a track finished nudges its weight up a notch, a track killed in the
first few seconds nudges it down — small, bounded, floored. Same explicit +
organic split chosen for the entity-alias lexicon.

Bounded on purpose: one reaction moves a weight by at most NUDGE, and never
below MIN_FACTOR or through the ceiling — so no single skip bans a song and no
run of replays runs a weight away. Ban (an explicit 0.0) is left untouched;
implicit signal never resurrects a banned track above the floor.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

# A completion at/after this fraction of the track counts as "listened through".
COMPLETE_FRACTION = 0.80
# A skip before this many seconds counts as an early reject.
EARLY_SKIP_SECONDS = 20.0

NUDGE = 0.15           # per-reaction weight step
MIN_FACTOR = 0.05      # floor for implicit moves (matches the route's MIN_WEIGHT_FACTOR)
MAX_FACTOR = 3.0       # ceiling so replays can't run a weight away


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def record_reaction(*, title: str, artist: str, track_id: str = "",
                    played_seconds: float, duration_seconds: float) -> str:
    """Record how the user reacted to a play and nudge the track+artist weights.

    Returns the classification: 'completed', 'skipped_early', or 'neutral'
    (a mid-track skip — real listening, no strong signal either way).
    """
    t, a = (title or "").strip(), (artist or "").strip()
    if not t:
        return "neutral"

    frac = (played_seconds / duration_seconds) if duration_seconds and duration_seconds > 0 else 0.0
    if frac >= COMPLETE_FRACTION:
        reaction, count_col, direction = "completed", "completed_count", +1
    elif played_seconds <= EARLY_SKIP_SECONDS:
        reaction, count_col, direction = "skipped_early", "skipped_early_count", -1
    else:
        return "neutral"

    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        # 1) bump the reaction counter on the played_songs row (title+artist unique)
        session.execute(
            text(f"UPDATE played_songs SET {count_col} = COALESCE({count_col}, 0) + 1 "
                 f"WHERE lower(trim(title)) = :t AND lower(trim(artist)) = :a"),
            {"t": _norm(t), "a": _norm(a)},
        )

        # 2) nudge the learned weights. Track: prefer the stable spotify:<id> key
        #    (what the buttons write) when we have an id, else the legacy composite.
        tkey = f"spotify:{_norm(track_id)}" if track_id else f"{_norm(t)}|||{_norm(a)}"
        _nudge(session, "music_track_weights", "track_key", tkey, direction, now,
               extra_cols={"title": t, "artist": a})
        # Artist moves at half step — one song is weaker evidence about the artist.
        _nudge(session, "music_artist_weights", "artist", _norm(a), direction, now,
               step=NUDGE / 2.0)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    logger.info("[dj_feedback] %s: '%s' by %s (%.0f/%.0fs)", reaction, t, a,
                played_seconds, duration_seconds)
    return reaction


def _nudge(session, table, key_col, key, direction, now, *, step=NUDGE, extra_cols=None):
    """Move one weight by ±step, floored/ceilinged. A banned weight (0.0) is
    never lifted by implicit signal — an explicit ban outranks listening."""
    row = session.execute(
        text(f"SELECT factor FROM {table} WHERE {key_col} = :k"), {"k": key}
    ).fetchone()
    cur = float(row[0]) if row is not None else 1.0

    if cur <= 0.0 and direction > 0:
        return  # don't resurrect a ban on a completion

    new = cur + direction * step
    new = max(MIN_FACTOR, min(MAX_FACTOR, new))
    if row is not None:
        session.execute(
            text(f"UPDATE {table} SET factor = :f, updated_at_utc = :u WHERE {key_col} = :k"),
            {"f": new, "u": now, "k": key},
        )
    else:
        cols = {key_col: key, "factor": new, "updated_at_utc": now}
        if extra_cols:
            cols.update(extra_cols)
        names = ", ".join(cols)
        binds = ", ".join(f":{c}" for c in cols)
        session.execute(text(f"INSERT INTO {table} ({names}) VALUES ({binds})"), cols)
