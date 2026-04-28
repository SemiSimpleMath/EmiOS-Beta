"""
Played Songs Model - Tracks music play history for DJ Manager.

Stores song play counts to avoid repetition and track listening patterns.
"""

from datetime import datetime, timezone, timedelta
from sqlalchemy import Column, Integer, String, DateTime, Index, func
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)
from app.models.base import get_session
from app.assistant.database.db_instance import db


class PlayedSong(db.Model):
    """
    Tracks songs that have been played by the DJ.
    
    The combination of (title, artist) is unique - we update counts on replay.
    """
    __tablename__ = 'played_songs'
    
    id = Column(Integer, primary_key=True)
    
    # Song identification
    title = Column(String(500), nullable=False)
    artist = Column(String(500), nullable=False)
    search_query = Column(String(500), nullable=True)  # Query used to find it
    
    # Timestamps
    first_played_utc = Column(DateTime, nullable=False)
    last_played_utc = Column(DateTime, nullable=False)
    
    # Play counts (updated on each play)
    play_count_today = Column(Integer, default=1)
    play_count_week = Column(Integer, default=1)
    play_count_month = Column(Integer, default=1)
    play_count_year = Column(Integer, default=1)
    play_count_all_time = Column(Integer, default=1)
    
    # Audio targets at time of pick (LLM sliders 0-100)
    last_energy_slider = Column(Integer, nullable=True)  # 0-100
    last_valence_slider = Column(Integer, nullable=True)  # 0-100
    last_loudness_slider = Column(Integer, nullable=True)  # 0-100
    last_speechiness_slider = Column(Integer, nullable=True)  # 0-100
    last_acousticness_slider = Column(Integer, nullable=True)  # 0-100
    last_instrumentalness_slider = Column(Integer, nullable=True)  # 0-100
    last_liveness_slider = Column(Integer, nullable=True)  # 0-100
    last_tempo_slider = Column(Integer, nullable=True)  # 0-100
    
    # For count reset tracking
    last_count_reset_date = Column(String(10), nullable=True)  # YYYY-MM-DD
    
    __table_args__ = (
        Index('idx_played_songs_title_artist', 'title', 'artist', unique=True),
        Index('idx_played_songs_last_played', 'last_played_utc'),
    )
    
    def __repr__(self):
        return f"<PlayedSong {self.title} by {self.artist} (plays: {self.play_count_all_time})>"


def _norm_str(s: str) -> str:
    """
    Normalize a title/artist string for matching.

    Keep this conservative (trim + lowercase) to avoid false merges.
    """
    return (s or "").strip().lower()


def _norm_col(col):
    """SQLite-safe normalization for SQLAlchemy filters: lower(trim(col))."""
    return func.lower(func.trim(col))


# -----------------------------------------------------------------------------
# Cooldown scoring (diversification)
# -----------------------------------------------------------------------------
#
# Model:
# - When a track/artist is just played, its weight is near-zero.
# - It "recovers" linearly each day until it reaches 1.0 (fully eligible again).
#
# Track (stricter): 0.05/day → full recovery ~20 days
# Artist (more forgiving): 0.10/day → full recovery ~10 days
TRACK_RECOVERY_PER_DAY = 0.05
ARTIST_RECOVERY_PER_DAY = 0.10

# Avoid true zero weights (keeps selection stable if an entire candidate set is "too recent").
MIN_COOLDOWN_WEIGHT = 0.01


def _clamp01(x: float) -> float:
    try:
        xf = float(x)
    except Exception:
        return 0.0
    return 0.0 if xf < 0.0 else 1.0 if xf > 1.0 else xf


def _days_from_hours(hours: float | None) -> float:
    if hours is None:
        return 9999.0
    try:
        h = float(hours)
    except Exception:
        return 9999.0
    if h < 0:
        h = 0.0
    return h / 24.0


def get_artist_hours_since_last(artist: str) -> float | None:
    """
    Return hours since this artist was last played (any track).
    Uses case/whitespace-insensitive matching on artist name.
    """
    session = get_session()
    now_utc = datetime.now(timezone.utc)
    try:
        a_norm = _norm_str(artist)
        if not a_norm:
            return None

        last_played = (
            session.query(func.max(PlayedSong.last_played_utc))
            .filter(_norm_col(PlayedSong.artist) == a_norm)
            .scalar()
        )
        if last_played is None:
            return None

        # SQLite often returns naive datetimes; treat them as UTC.
        if getattr(last_played, "tzinfo", None) is None:
            last_played = last_played.replace(tzinfo=timezone.utc)

        return (now_utc - last_played).total_seconds() / 3600.0
    finally:
        session.close()


def initialize_played_songs_table():
    """Create the played_songs table if it doesn't exist."""
    from app.assistant.database.db_instance import db
    PlayedSong.__table__.create(db.engine, checkfirst=True)


def record_song_play(
    title: str, 
    artist: str, 
    search_query: str = None,
    audio_targets: dict = None,
) -> PlayedSong:
    """
    Record that a song was played.
    
    If the song exists, increment counts. If not, create new entry.
    Also handles resetting daily/weekly/etc counts when needed.
    
    NOTE: This is called from DJ Manager at PICK time, which is protected by
    _pick_in_progress flag. No dedupe needed here - the caller handles it.
    """
    session = get_session()
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    
    try:
        # Fuzzy lookup (case/whitespace-insensitive) so history penalties apply reliably
        # even if upstream sources vary in casing/spaces.
        t_norm = _norm_str(title)
        a_norm = _norm_str(artist)

        # NOTE: duplicates can exist differing only by case because SQLite uniqueness is
        # case-sensitive by default. We pick one canonical row (lowest id) and merge.
        matches = (
            session.query(PlayedSong)
            .filter(_norm_col(PlayedSong.title) == t_norm, _norm_col(PlayedSong.artist) == a_norm)
            .order_by(PlayedSong.id.asc())
            .all()
        )
        song = matches[0] if matches else None
        
        def _set_audio_targets(ps: PlayedSong, targets: dict) -> None:
            if not isinstance(targets, dict):
                return

            def clamp_int(v):
                try:
                    iv = int(round(float(v)))
                except Exception:
                    return None
                return max(0, min(100, iv))

            mapping = {
                "energy": "last_energy_slider",
                "valence": "last_valence_slider",
                "loudness": "last_loudness_slider",
                "speechiness": "last_speechiness_slider",
                "acousticness": "last_acousticness_slider",
                "instrumentalness": "last_instrumentalness_slider",
                "liveness": "last_liveness_slider",
                "tempo": "last_tempo_slider",
            }

            for k, col in mapping.items():
                if k not in targets:
                    continue
                v = clamp_int(targets.get(k))
                if v is None:
                    continue
                setattr(ps, col, v)

        if song:
            # Check if we need to reset counts (new day/week/month/year)
            _maybe_reset_counts(song, now_utc, today_str)

            # If there are duplicates for the same normalized (title, artist), merge them into
            # the canonical row so future lookups and penalties are consistent.
            if len(matches) > 1:
                for other in matches[1:]:
                    try:
                        _maybe_reset_counts(other, now_utc, today_str)
                    except Exception as e:
                        logger.debug("Failed to reset counts on duplicate song row: %s", e, exc_info=True)

                    # Timestamps: keep earliest first_played and latest last_played
                    try:
                        if other.first_played_utc and (
                            song.first_played_utc is None or other.first_played_utc < song.first_played_utc
                        ):
                            song.first_played_utc = other.first_played_utc
                    except Exception as e:
                        logger.debug("Failed to merge first_played_utc on duplicate: %s", e, exc_info=True)
                    try:
                        if other.last_played_utc and (
                            song.last_played_utc is None or other.last_played_utc > song.last_played_utc
                        ):
                            song.last_played_utc = other.last_played_utc
                    except Exception as e:
                        logger.debug("Failed to merge last_played_utc on duplicate: %s", e, exc_info=True)

                    # Counts: fold into canonical
                    for field in (
                        "play_count_today",
                        "play_count_week",
                        "play_count_month",
                        "play_count_year",
                        "play_count_all_time",
                    ):
                        try:
                            song_val = int(getattr(song, field) or 0)
                            other_val = int(getattr(other, field) or 0)
                            setattr(song, field, song_val + other_val)
                        except Exception as e:
                            logger.debug("Failed to accumulate %s during duplicate merge: %s", field, e, exc_info=True)

                    # Keep a search query if canonical is empty
                    if (not song.search_query) and other.search_query:
                        song.search_query = other.search_query

                    # Audio targets: prefer canonical if set, else take other
                    for field in (
                        "last_energy_slider",
                        "last_valence_slider",
                        "last_loudness_slider",
                        "last_speechiness_slider",
                        "last_acousticness_slider",
                        "last_instrumentalness_slider",
                        "last_liveness_slider",
                        "last_tempo_slider",
                    ):
                        try:
                            if getattr(song, field) is None and getattr(other, field) is not None:
                                setattr(song, field, getattr(other, field))
                        except Exception:
                            pass

                    try:
                        session.delete(other)
                    except Exception as e:
                        logger.debug("Failed to delete duplicate song row: %s", e, exc_info=True)
            
            # Increment counts
            song.last_played_utc = now_utc
            song.play_count_today += 1
            song.play_count_week += 1
            song.play_count_month += 1
            song.play_count_year += 1
            song.play_count_all_time += 1
            
            if search_query:
                song.search_query = search_query

            # Update audio targets (0-100)
            if audio_targets is not None:
                _set_audio_targets(song, audio_targets)
        else:
            # Create new entry
            song = PlayedSong(
                title=title,
                artist=artist,
                search_query=search_query,
                first_played_utc=now_utc,
                last_played_utc=now_utc,
                play_count_today=1,
                play_count_week=1,
                play_count_month=1,
                play_count_year=1,
                play_count_all_time=1,
                last_count_reset_date=today_str,
            )
            if audio_targets is not None:
                _set_audio_targets(song, audio_targets)
            session.add(song)
        
        session.commit()
        return song
        
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


def _maybe_reset_counts(song: PlayedSong, now_utc: datetime, today_str: str):
    """Reset period counts if we've crossed into a new period."""
    if not song.last_count_reset_date:
        song.last_count_reset_date = today_str
        return
    
    last_reset = datetime.strptime(song.last_count_reset_date, "%Y-%m-%d")
    last_reset = last_reset.replace(tzinfo=timezone.utc)
    now_date = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # New day - reset daily count
    if today_str != song.last_count_reset_date:
        song.play_count_today = 0
    
    # New week (Monday = 0)
    last_week = last_reset.isocalendar()[1]
    current_week = now_utc.isocalendar()[1]
    if current_week != last_week or now_utc.year != last_reset.year:
        song.play_count_week = 0
    
    # New month
    if now_utc.month != last_reset.month or now_utc.year != last_reset.year:
        song.play_count_month = 0
    
    # New year
    if now_utc.year != last_reset.year:
        song.play_count_year = 0
    
    song.last_count_reset_date = today_str


def get_recently_played(limit: int = 20) -> list:
    """
    Get recently played songs, oldest first (so most recent is at bottom).
    
    Includes vibe data (energy, valence) for each song.
    Groups songs by date with clear day dividers.
    """
    from app.assistant.utils.time_utils import utc_to_local, get_local_time
    
    session = get_session()
    try:
        # Get most recent N, then reverse to show oldest first
        songs = session.query(PlayedSong).order_by(
            PlayedSong.last_played_utc.desc()
        ).limit(limit).all()
        
        # Reverse so oldest is first, most recent is last
        songs = list(reversed(songs))
        
        result = []
        current_date = None
        today_local = get_local_time().strftime("%Y-%m-%d")
        
        for s in songs:
            # Format local time and date
            played_time_local = ""
            played_date = ""
            if s.last_played_utc:
                local_dt = utc_to_local(s.last_played_utc)
                played_time_local = local_dt.strftime("%I:%M %p").lstrip("0")
                played_date = local_dt.strftime("%Y-%m-%d")
            
            # Add day divider if date changed
            if played_date and played_date != current_date:
                current_date = played_date
                if played_date == today_local:
                    day_label = "TODAY"
                else:
                    # Format as "Yesterday" or "Mon Jan 15"
                    local_dt = utc_to_local(s.last_played_utc)
                    yesterday = (get_local_time() - timedelta(days=1)).strftime("%Y-%m-%d")
                    if played_date == yesterday:
                        day_label = "YESTERDAY"
                    else:
                        day_label = local_dt.strftime("%a %b %d").upper()
                
                result.append({
                    "is_day_divider": True,
                    "day_label": day_label,
                    "date": played_date,
                })
            
            # Audio targets (0-100 sliders)
            audio_targets = {
                "energy": s.last_energy_slider,
                "valence": s.last_valence_slider,
                "loudness": s.last_loudness_slider,
                "speechiness": s.last_speechiness_slider,
                "acousticness": s.last_acousticness_slider,
                "instrumentalness": s.last_instrumentalness_slider,
                "liveness": s.last_liveness_slider,
                "tempo": s.last_tempo_slider,
            }
            
            result.append({
                "title": s.title,
                "artist": s.artist,
                "played_at": played_time_local,
                "last_played_utc": s.last_played_utc.isoformat() if s.last_played_utc else None,
                "play_count_today": s.play_count_today,
                "play_count_all_time": s.play_count_all_time,
                # Audio targets (0-100)
                "audio_targets": audio_targets,
                # Convenience fields (keep old keys but now represent sliders)
                "energy": s.last_energy_slider,
                "valence": s.last_valence_slider,
            })
        
        return result
    finally:
        session.close()


def get_last_played_song() -> dict:
    """Get the most recently played song."""
    session = get_session()
    try:
        song = session.query(PlayedSong).order_by(
            PlayedSong.last_played_utc.desc()
        ).first()
        
        if song:
            return {
                "title": song.title,
                "artist": song.artist,
                "search_query": song.search_query,
            }
        return None
    finally:
        session.close()


def get_song_play_stats(title: str, artist: str) -> dict:
    """
    Get play statistics for a specific song.
    
    Returns stats used for scoring: plays today, plays this week,
    hours since last played.
    """
    session = get_session()
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    
    try:
        t_norm = _norm_str(title)
        a_norm = _norm_str(artist)

        songs = (
            session.query(PlayedSong)
            .filter(_norm_col(PlayedSong.title) == t_norm, _norm_col(PlayedSong.artist) == a_norm)
            .order_by(PlayedSong.id.asc())
            .all()
        )

        if not songs:
            # Never played - return empty stats (will get high score)
            return {
                "found": False,
                "plays_today": 0,
                "plays_week": 0,
                "plays_all_time": 0,
                "hours_since_last": None,
            }
        
        plays_today = 0
        plays_week = 0
        plays_all_time = 0
        newest_last_played = None

        # Maybe reset counts if it's a new day/week, then aggregate across duplicates.
        for s in songs:
            try:
                _maybe_reset_counts(s, now_utc, today_str)
            except Exception as e:
                logger.debug("Failed to reset counts for song %r during aggregation: %s", getattr(s, 'title', '?'), e, exc_info=True)

            try:
                plays_today += int(s.play_count_today or 0)
            except Exception:
                pass
            try:
                plays_week += int(s.play_count_week or 0)
            except Exception:
                pass
            try:
                plays_all_time += int(s.play_count_all_time or 0)
            except Exception:
                pass

            lp = getattr(s, "last_played_utc", None)
            if lp is not None and getattr(lp, "tzinfo", None) is None:
                lp = lp.replace(tzinfo=timezone.utc)
            if lp is not None and (newest_last_played is None or lp > newest_last_played):
                newest_last_played = lp

        # Calculate hours since last played (based on most recent among duplicates)
        hours_since = None
        if newest_last_played is not None:
            delta = now_utc - newest_last_played
            hours_since = delta.total_seconds() / 3600
        
        return {
            "found": True,
            "plays_today": plays_today,
            "plays_week": plays_week,
            "plays_all_time": plays_all_time,
            "hours_since_last": hours_since,
        }
    finally:
        session.close()


def score_song_candidate(title: str, artist: str) -> float:
    """
    Score a song candidate based on play history.
    
    Higher score = more likely to be picked.
    Songs played recently get lower scores.
    
    Cooldown model (diversification):
    - Track weight recovers linearly by TRACK_RECOVERY_PER_DAY
    - Artist weight recovers linearly by ARTIST_RECOVERY_PER_DAY
    - Final weight = track_weight * artist_weight (floored by MIN_COOLDOWN_WEIGHT)
    """
    stats = get_song_play_stats(title, artist)
    
    # Never played - highest score
    if not stats["found"]:
        return 1.0

    # Track cooldown: based on hours since this exact (title, artist) was last played.
    track_days = _days_from_hours(stats.get("hours_since_last"))
    track_w = _clamp01(TRACK_RECOVERY_PER_DAY * track_days)
    if track_w < MIN_COOLDOWN_WEIGHT:
        track_w = MIN_COOLDOWN_WEIGHT

    # Artist cooldown: based on hours since ANY track by this artist was last played.
    artist_hours = get_artist_hours_since_last(artist)
    artist_days = _days_from_hours(artist_hours)
    artist_w = _clamp01(ARTIST_RECOVERY_PER_DAY * artist_days)
    if artist_w < MIN_COOLDOWN_WEIGHT:
        artist_w = MIN_COOLDOWN_WEIGHT

    score = track_w * artist_w
    # Keep in a stable range for the selector.
    return max(MIN_COOLDOWN_WEIGHT, min(1.0, float(score)))
