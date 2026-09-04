"""
Music Route - Apple MusicKit integration tab for Emi.
"""
from __future__ import annotations

from flask import Blueprint, render_template, jsonify, request
from sqlalchemy import text

from app.assistant.music_manager import get_music_manager
from app.assistant.dj_manager import get_dj_manager
from app.assistant.utils.logging_config import get_logger
from app.assistant.database.db_instance import db
from app.assistant.dj_manager.feature_scaler import db_to_llm_features

from app.models.music_weights import MusicGenreWeight, MusicArtistWeight, MusicTrackWeight

logger = get_logger(__name__)

music_bp = Blueprint('music', __name__)

MIN_WEIGHT_FACTOR = 0.05
DEFAULT_WEIGHT_DELTA = 0.1


def _norm_key(s: str) -> str:
    return (s or "").strip().lower()


def _track_key(title: str, artist: str) -> str:
    return f"{_norm_key(title)}|||{_norm_key(artist)}"


def _track_key_from_track_id(track_id: str) -> str:
    """
    Stable key for track-level weights when we have a dataset track_id.

    Using title/artist as the primary key is inherently lossy (remasters, featuring, punctuation).
    The DJ picks from the Spotify dataset which already has a unique track_id, so prefer that.
    """
    return f"spotify:{_norm_key(track_id)}"


def _norm_artist_for_lookup(artist: str) -> str:
    """
    Normalize artist names for matching across datasets.

    Keep this conservative: strip whitespace/case and a leading 'the '.
    """
    a = _norm_key(artist)
    if a.startswith("the "):
        a = a[4:]
    return a


def _get_weight_factor(scope: str, key: str) -> float:
    """
    Return the stored weight factor for a scope/key.
    These are weights (unbounded floats), not normalized probabilities.
    """
    try:
        if scope == "genre":
            row = db.session.get(MusicGenreWeight, _norm_key(key))
        elif scope == "artist":
            row = db.session.get(MusicArtistWeight, _norm_key(key))
        elif scope == "track":
            row = db.session.get(MusicTrackWeight, _norm_key(key))
        else:
            row = None
        if not row:
            return 1.0
        try:
            return float(row.factor)
        except Exception:
            return 1.0
    except Exception:
        return 1.0


def _upsert_weight_factor(scope: str, key: str, new_factor: float, *, title: str | None = None, artist: str | None = None) -> float:
    f = max(0.0, float(new_factor))
    k = _norm_key(key)
    if scope == "genre":
        row = db.session.get(MusicGenreWeight, k)
        if row is None:
            row = MusicGenreWeight(genre=k, factor=f)
            db.session.add(row)
        else:
            row.factor = f
    elif scope == "artist":
        row = db.session.get(MusicArtistWeight, k)
        if row is None:
            row = MusicArtistWeight(artist=k, factor=f)
            db.session.add(row)
        else:
            row.factor = f
    elif scope == "track":
        row = db.session.get(MusicTrackWeight, k)
        if row is None:
            row = MusicTrackWeight(track_key=k, title=title, artist=artist, factor=f)
            db.session.add(row)
        else:
            row.factor = f
            if title and not row.title:
                row.title = title
            if artist and not row.artist:
                row.artist = artist
    else:
        return f
    db.session.commit()
    return f


@music_bp.route('/music')
def music_tab():
    """Render the music player tab."""
    music_manager = get_music_manager()
    return render_template(
        'music.html',
        is_configured=music_manager.is_configured(),
    )


@music_bp.route('/api/music/token', methods=['GET'])
def get_developer_token():
    """
    Get a fresh MusicKit developer token.
    
    Query params:
        origin: Optional origin for web token validation
    
    Returns:
        JSON with developer_token or error
    """
    music_manager = get_music_manager()
    
    logger.info(f"🎵 Token request - is_configured: {music_manager.is_configured()}")
    
    if not music_manager.is_configured():
        logger.error("MusicKit not configured - private key not loaded")
        return jsonify({
            "error": "MusicKit not configured",
            "message": "Private key not found",
        }), 500
    
    # Get origin from query param or referer
    origin = request.args.get('origin')
    if not origin:
        # Try to infer from request
        origin = request.host_url.rstrip('/')
    
    logger.info(f"🎵 Generating token with origin: {origin}")
    
    token = music_manager.generate_developer_token(origin=origin)
    
    if not token:
        logger.error("Token generation returned None")
        return jsonify({
            "error": "Token generation failed",
            "message": "Check server logs for details",
        }), 500
    
    logger.info(f"🎵 Token generated successfully (length: {len(token)})")
    
    return jsonify({
        "developer_token": token,
        "origin": origin,
    })


@music_bp.route('/api/music/state', methods=['GET'])
def get_playback_state():
    """Get current playback state."""
    music_manager = get_music_manager()
    return jsonify(music_manager.get_playback_state())


@music_bp.route('/api/music/state', methods=['POST'])
def update_playback_state():
    """Update playback state from frontend."""
    music_manager = get_music_manager()
    state = request.get_json() or {}
    music_manager.update_playback_state(state)
    return jsonify({"status": "ok"})


# =============================================================================
# DJ Manager API
# =============================================================================

@music_bp.route('/api/music/dj/status', methods=['GET'])
def get_dj_status():
    """Get DJ manager status."""
    dj_manager = get_dj_manager()
    return jsonify(dj_manager.get_status())


@music_bp.route('/api/music/dj/enable', methods=['POST'])
def enable_dj():
    """Enable DJ mode - auto pause/resume based on AFK."""
    dj_manager = get_dj_manager()
    dj_manager.enable()
    return jsonify({"status": "enabled", "dj": dj_manager.get_status()})


@music_bp.route('/api/music/dj/disable', methods=['POST'])
def disable_dj():
    """Disable DJ mode."""
    dj_manager = get_dj_manager()
    dj_manager.disable()
    return jsonify({"status": "disabled", "dj": dj_manager.get_status()})


@music_bp.route('/api/music/dj/settings', methods=['POST'])
def update_dj_settings():
    """
    Update DJ settings.

    Body (all optional):
      { pause_on_afk: bool }
    """
    dj_manager = get_dj_manager()
    data = request.get_json() or {}

    if "pause_on_afk" in data:
        dj_manager.set_pause_on_afk(bool(data.get("pause_on_afk")))

    return jsonify({"status": "ok", "dj": dj_manager.get_status()})


@music_bp.route('/api/music/dj/pick', methods=['POST'])
def dj_pick_song():
    """Have the DJ agent pick and play a contextually appropriate song."""
    dj_manager = get_dj_manager()
    
    if not dj_manager.is_enabled():
        return jsonify({"error": "DJ mode not enabled"}), 400
    
    picked = dj_manager.pick_song(reason="api_manual")
    if not picked:
        return jsonify({"status": "skipped", "reason": "no_pick_result"})

    if picked.get("skip_music", False):
        return jsonify({"status": "skipped", "reason": picked.get("skip_reason", "skip_music")})

    title = (picked.get("title") or "").strip()
    artist = (picked.get("artist") or "").strip()
    targets = picked.get("targets", {}) if isinstance(picked.get("targets"), dict) else {}

    if not title:
        return jsonify({"status": "skipped", "reason": "empty_query"})

    # Keep explicit sequencing: pick -> record -> play
    from app.assistant.dj_manager.query_utils import build_search_query
    q = build_search_query(title, artist)
    dj_manager.record_pick(title=title, artist=artist or "Unknown", targets=targets, search_query=q)
    success = dj_manager.play_song(
        q,
        mode="queue_next",
        payload={
            "dataset": {
                "title": title,
                "artist": artist,
                "genre": picked.get("genre") if isinstance(picked, dict) else None,
                "sliders": picked.get("sliders") if isinstance(picked, dict) else None,
                "prob_factor": picked.get("prob_factor") if isinstance(picked, dict) else None,
            }
        },
    )

    return jsonify({"status": "playing" if success else "failed", "title": title, "artist": artist})


@music_bp.route('/api/music/dj/pick_once', methods=['POST'])
def dj_pick_song_once():
    """
    One-shot DJ pick.

    This is intentionally allowed even when DJ mode is OFF:
    - It does NOT enable continuous mode.
    - It does NOT toggle AFK pause/resume.
    """
    dj_manager = get_dj_manager()

    picked = dj_manager.pick_song_once(reason="api_once")
    if not picked:
        return jsonify({"status": "skipped", "reason": "no_pick_result"})

    if picked.get("skip_music", False):
        return jsonify({"status": "skipped", "reason": picked.get("skip_reason", "skip_music")})

    title = (picked.get("title") or "").strip()
    artist = (picked.get("artist") or "").strip()
    targets = picked.get("targets", {}) if isinstance(picked.get("targets"), dict) else {}

    if not title:
        return jsonify({"status": "skipped", "reason": "empty_query"})

    from app.assistant.dj_manager.query_utils import build_search_query

    q = build_search_query(title, artist)
    dj_manager.record_pick(title=title, artist=artist or "Unknown", targets=targets, search_query=q)
    success = dj_manager.play_song(
        q,
        mode="queue_next",
        payload={
            "dataset": {
                "title": title,
                "artist": artist,
                "genre": picked.get("genre") if isinstance(picked, dict) else None,
                "sliders": picked.get("sliders") if isinstance(picked, dict) else None,
                "prob_factor": picked.get("prob_factor") if isinstance(picked, dict) else None,
            }
        },
    )

    return jsonify({"status": "playing" if success else "failed", "title": title, "artist": artist})


@music_bp.route('/api/music/song_meta', methods=['GET'])
def get_song_meta():
    """
    Return dataset-derived metadata for a song (if found in spotify dataset table).

    Query params:
      - title
      - artist
      - fuzzy: "1" to enable fuzzy matching (opt-in)
    """
    track_id = (request.args.get("track_id") or "").strip()
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    fuzzy = str(request.args.get("fuzzy") or "0").strip().lower() in ("1", "true", "yes", "y", "on")
    if not track_id and not title and not artist:
        return jsonify({"found": False, "error": "missing_title_and_artist"}), 400

    sql_exact = text(
        """
        SELECT track_name, artist_name, genre, prob_factor,
               energy, valence, loudness, speechiness, acousticness, instrumentalness, liveness, tempo
        FROM music_tracks_spotify
        WHERE LOWER(track_name) = LOWER(:title) AND LOWER(artist_name) = LOWER(:artist)
        LIMIT 1
        """
    )
    sql_by_track_id = text(
        """
        SELECT track_name, artist_name, genre, prob_factor,
               energy, valence, loudness, speechiness, acousticness, instrumentalness, liveness, tempo, track_id
        FROM music_tracks_spotify
        WHERE track_id = :track_id
        LIMIT 1
        """
    )
    # Conservative fallback: keep title exact, allow artist LIKE.
    # This prevents accidentally selecting variants like "Jessica - Live at ...".
    sql_title_exact_artist_like = text(
        """
        SELECT track_name, artist_name, genre, prob_factor,
               energy, valence, loudness, speechiness, acousticness, instrumentalness, liveness, tempo
        FROM music_tracks_spotify
        WHERE LOWER(track_name) = LOWER(:title)
          AND LOWER(artist_name) LIKE LOWER(:artist_like)
        LIMIT 1
        """
    )

    # Opt-in fuzzy: both title and artist LIKE (returns a small candidate set; we score in Python).
    sql_fuzzy_title_artist_like = text(
        """
        SELECT track_name, artist_name, genre, prob_factor,
               energy, valence, loudness, speechiness, acousticness, instrumentalness, liveness, tempo
        FROM music_tracks_spotify
        WHERE LOWER(track_name) LIKE LOWER(:title_like)
          AND LOWER(artist_name) LIKE LOWER(:artist_like)
        LIMIT 30
        """
    )

    row = None
    match_strategy = None
    match_score = None
    try:
        if track_id:
            row = db.session.execute(sql_by_track_id, {"track_id": track_id}).fetchone()
            if row is not None:
                match_strategy = "track_id"
        if row is None and title and artist:
            row = db.session.execute(sql_exact, {"title": title, "artist": artist}).fetchone()
            if row is not None:
                match_strategy = "exact"
        if row is None and title and artist:
            # Common dataset normalization: "The X" vs "X"
            a_norm = _norm_artist_for_lookup(artist)
            if a_norm and a_norm != _norm_key(artist):
                row = db.session.execute(
                    sql_exact,
                    {"title": title, "artist": a_norm},
                ).fetchone()
                if row is not None:
                    match_strategy = "exact_norm_artist"
        if row is None:
            # Last-resort fuzzy artist match. Keep title exact if provided.
            if title and artist:
                a_norm = _norm_artist_for_lookup(artist)
                like = f"%{a_norm or artist}%"
                row = db.session.execute(
                    sql_title_exact_artist_like,
                    {"title": title, "artist_like": like},
                ).fetchone()
                if row is not None:
                    match_strategy = "title_exact_artist_like"

        # Opt-in: fuzzy title+artist matching
        if row is None and fuzzy and title and artist:
            t_like = f"%{_norm_key(title)}%"
            a_norm = _norm_artist_for_lookup(artist)
            a_like = f"%{a_norm or _norm_key(artist)}%"
            candidates = db.session.execute(
                sql_fuzzy_title_artist_like,
                {"title_like": t_like, "artist_like": a_like},
            ).fetchall()

            def _tokens(s: str) -> set[str]:
                import re

                s2 = (s or "").strip().lower()
                # Remove punctuation, split on whitespace.
                s2 = re.sub(r"[^a-z0-9\s]+", " ", s2)
                parts = [p for p in s2.split() if len(p) >= 2]
                # Drop very common junk tokens
                drop = {"feat", "ft", "remaster", "remastered", "live", "edit", "version", "radio", "mix"}
                return {p for p in parts if p not in drop}

            t_in = _tokens(title)
            a_in = _tokens(artist)

            best = None
            best_score = -1.0
            for r in candidates or []:
                tn = r[0]
                an = r[1]
                t_row = _tokens(str(tn or ""))
                a_row = _tokens(str(an or ""))
                # Jaccard overlap (title weighted more than artist)
                def jacc(a: set[str], b: set[str]) -> float:
                    if not a or not b:
                        return 0.0
                    inter = len(a & b)
                    union = len(a | b)
                    return float(inter) / float(union) if union else 0.0

                score = 0.7 * jacc(t_in, t_row) + 0.3 * jacc(a_in, a_row)
                # Prefer closer-length titles (helps avoid "Live at ..." variants)
                try:
                    score -= 0.02 * abs(len(str(tn or "")) - len(title)) / max(1.0, float(len(title)))
                except Exception:
                    pass
                if score > best_score:
                    best_score = score
                    best = r

            # Only accept if overlap is decent (avoid super generic matches)
            if best is not None and best_score >= 0.35:
                row = best
                match_strategy = "fuzzy_like_scored"
                match_score = round(float(best_score), 3)
    except Exception as e:
        logger.warning(f"song_meta query failed: {e}")
        row = None

    if not row:
        return jsonify({"found": False, "track_id": track_id or None, "title": title, "artist": artist, "match_strategy": None})

    # row may include track_id (when matched by track_id) — handle both shapes.
    track_id_row = None
    if len(row) >= 13:
        (
            track_name,
            artist_name,
            genre,
            base_pf,
            energy,
            valence,
            loudness,
            speechiness,
            acousticness,
            instrumentalness,
            liveness,
            tempo,
            track_id_row,
        ) = row
    else:
        (
            track_name,
            artist_name,
            genre,
            base_pf,
            energy,
            valence,
            loudness,
            speechiness,
            acousticness,
            instrumentalness,
            liveness,
            tempo,
        ) = row

    sliders = db_to_llm_features(
        {
            "energy": energy,
            "valence": valence,
            "loudness": loudness,
            "speechiness": speechiness,
            "acousticness": acousticness,
            "instrumentalness": instrumentalness,
            "liveness": liveness,
            "tempo": tempo,
        }
    )
    for k in ("energy", "valence", "loudness", "speechiness", "acousticness", "instrumentalness", "liveness", "tempo"):
        sliders.setdefault(k, 50.0)

    base_pf_f = float(base_pf or 0.0)  # row/base channel (after migration usually 1.0)
    w_track = _get_weight_factor("track", _track_key(track_name, artist_name))
    w_artist = _get_weight_factor("artist", _norm_key(artist_name))
    w_genre = _get_weight_factor("genre", _norm_key(genre or ""))
    effective_weight = max(0.0, base_pf_f) * max(0.0, w_track) * max(0.0, w_artist) * max(0.0, w_genre)

    return jsonify(
        {
            "found": True,
            "track_id": track_id_row or None,
            "title": track_name,
            "artist": artist_name,
            "genre": genre,
            "sliders": sliders,
            "match_strategy": match_strategy,
            "match_score": match_score,
            # Weight terminology
            "row_weight": base_pf_f,
            "effective_weight": effective_weight,
            "weights": {"track": w_track, "artist": w_artist, "genre": w_genre},

            # Backward-compatible keys (to be removed later)
            "base_prob_factor": base_pf_f,
            "prob_factor": effective_weight,
            "override_factors": {"track": w_track, "artist": w_artist, "genre": w_genre},
        }
    )


@music_bp.route('/api/music/chat', methods=['POST'])
def dj_chat():
    """Free-text instruction to the DJ ("no vocals until 5pm", "more upbeat").

    Persisted as a chat message tagged `music` on the master room. The vibe
    planner already reads music-tagged chat (include_tags=['music']) on its
    30-min recheck AND rechecks immediately on new music chat, so a fresh
    instruction reshapes the next pick — no new agent, no parsing here.
    """
    from datetime import datetime, timezone
    from app.assistant.utils.pydantic_classes import Message

    data = request.get_json() or {}
    text_in = (data.get("text") or "").strip()
    if not text_in:
        return jsonify({"error": "empty"}), 400
    if len(text_in) > 500:
        text_in = text_in[:500]

    msg = Message(
        data_type="user_msg",
        sub_data_type=["chat", "music"],   # 'music' is what the vibe planner filters on
        sender="User",
        role="user",
        content=text_in,
        is_chat=True,
        timestamp=datetime.now(timezone.utc),
        room_id="master_room",
        room_surface="ui",
        room_context_id="main",
        room_message_direction="inbound",
        room_initiated_by="user",
    )
    from app.assistant.ServiceLocator.service_locator import DI
    DI.global_blackboard.add_msg(msg)

    # Nudge the DJ so the vibe re-plans now rather than at the next 30-min tick.
    try:
        dj_manager = get_dj_manager()
        if dj_manager.is_enabled():
            dj_manager.request_pick_and_queue(reason="dj_chat_instruction")
    except Exception as e:
        logger.debug("dj_chat: could not poke DJ for immediate recheck: %s", e)

    logger.info("[dj_chat] music instruction recorded: %s", text_in[:120])
    return jsonify({"status": "ok"})


@music_bp.route('/api/music/reaction', methods=['POST'])
def record_playback_reaction():
    """The player reports how a finished play went; we learn from it.

    Body: {title, artist, track_id?, played_seconds, duration_seconds}
    A completion nudges the song's taste weight up, an early skip nudges it
    down — bounded, floored, and never lifting an explicit ban. Returns the
    classification so the client can log it.
    """
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    if not title:
        return jsonify({"error": "missing_title"}), 400
    try:
        played = float(data.get("played_seconds") or 0.0)
        duration = float(data.get("duration_seconds") or 0.0)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_seconds"}), 400

    from app.assistant.dj_manager.playback_feedback import record_reaction
    reaction = record_reaction(
        title=title, artist=artist, track_id=(data.get("track_id") or "").strip(),
        played_seconds=played, duration_seconds=duration,
    )
    return jsonify({"status": "recorded", "reaction": reaction})


@music_bp.route('/api/music/weights/adjust', methods=['POST'])
def adjust_music_weight():
    """
    Adjust weights for a scope/key in one endpoint.

    Body:
      {
        scope: "track" | "artist" | "genre",
        title?: string,
        artist?: string,
        genre?: string,

        # Option A (additive adjust):
        delta?: number,        # positive increases, negative decreases (default 0)

        # Option B (explicit set, e.g. Ban):
        set_factor?: number    # set exact factor (0 allowed)
      }

    Notes:
    - Decrements are floored to MIN_WEIGHT_FACTOR unless you explicitly set 0.
    - Increments have no ceiling.
    """
    data = request.get_json() or {}
    scope = (data.get("scope") or "").strip().lower()
    if scope not in ("track", "artist", "genre"):
        return jsonify({"error": "invalid_scope"}), 400

    title = ""
    artist = ""
    if scope == "track":
        track_id = (data.get("track_id") or "").strip()
        if not track_id:
            return jsonify({"error": "missing_track_id"}), 400
        key = _track_key_from_track_id(track_id)
        # Optional: persist human-readable title/artist too for debugging
        title = (data.get("title") or "").strip()
        artist = (data.get("artist") or "").strip()
    elif scope == "artist":
        artist = (data.get("artist") or "").strip()
        if not artist:
            return jsonify({"error": "missing_artist"}), 400
        key = _norm_key(artist)
    else:
        genre = (data.get("genre") or "").strip()
        if not genre:
            return jsonify({"error": "missing_genre"}), 400
        key = _norm_key(genre)

    cur = float(_get_weight_factor(scope, key))

    if "set_factor" in data:
        try:
            new_factor = float(data.get("set_factor"))
        except Exception:
            return jsonify({"error": "invalid_set_factor"}), 400
        new_factor = max(0.0, new_factor)
        stored = _upsert_weight_factor(
            scope,
            key,
            new_factor,
            title=(title if scope == "track" else None),
            artist=(artist if scope == "track" else None),
        )
        return jsonify(
            {"status": "ok", "scope": scope, "key": key, "old_factor": cur, "new_factor": stored, "mode": "set"}
        )

    try:
        delta_f = float(data.get("delta", 0.0))
    except Exception:
        return jsonify({"error": "invalid_delta"}), 400

    new_factor = cur + delta_f
    if delta_f < 0:
        new_factor = max(MIN_WEIGHT_FACTOR, new_factor)
    else:
        new_factor = max(0.0, new_factor)

    stored = _upsert_weight_factor(
        scope,
        key,
        new_factor,
        title=(title if scope == "track" else None),
        artist=(artist if scope == "track" else None),
    )
    return jsonify(
        {"status": "ok", "scope": scope, "key": key, "old_factor": cur, "new_factor": stored, "mode": "delta"}
    )


@music_bp.route('/api/music/dj/command', methods=['POST'])
def dj_command():
    """
    Send a command through DJ manager.
    
    Body:
        command: 'play', 'pause', 'next', 'previous', 'search_and_play', 'set_volume'
        payload: Optional dict with command-specific data
    """
    dj_manager = get_dj_manager()
    data = request.get_json() or {}
    
    command = data.get('command')
    payload = data.get('payload', {})
    
    if not command:
        return jsonify({"error": "No command specified"}), 400
    
    result = False
    if command == 'play':
        result = dj_manager.play()
    elif command == 'pause':
        result = dj_manager.pause()
    elif command == 'next':
        result = dj_manager.next_track()
    elif command == 'previous':
        result = dj_manager.previous_track()
    elif command == 'search_and_play':
        query = payload.get('query', '')
        if query:
            result = dj_manager.search_and_play(query)
        else:
            return jsonify({"error": "No query provided"}), 400
    elif command == 'set_volume':
        volume = payload.get('volume', 1.0)
        result = dj_manager.set_volume(float(volume))
    else:
        return jsonify({"error": f"Unknown command: {command}"}), 400
    
    return jsonify({"status": "sent" if result else "failed", "command": command})
