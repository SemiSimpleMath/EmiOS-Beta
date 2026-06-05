from __future__ import annotations

"""
Music dataset access.

Supports both:
- CSV (small/legacy, `MusicDataset`)
- SQLite (primary for large tables, `SqliteMusicDataset`)

Provides:
- nearest_matches(): get N close matches to target audio sliders
- sample_for_prompt(): build a close-match pool, then pick a small prompt sample

Distance is computed in slider-space (0-100) so all features are comparable.

Note: the SQLite path uses an approximate SQL prefilter (energy/valence window + ORDER BY
energy/valence closeness), then computes the full weighted slider distance in Python.
"""

import csv
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.dj_manager.feature_scaler import (
    DEFAULT_LOUDNESS_DB_SCALE,
    DEFAULT_TEMPO_BPM_SCALE,
    db_to_llm_features,
)

logger = get_logger(__name__)


DATASET_PATH = get_repo_root() / "data" / "music_data" / "curated_music_data.csv"


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(str(x).strip())
    except Exception:
        return None


def _ascii_safe(s: str) -> str:
    """
    Best-effort ASCII normalization for prompts/logs.

    We keep prompts ASCII-only to avoid Windows encoding issues, but we also want
    readable strings (strip diacritics and drop non-ascii).
    """
    import unicodedata

    s = s or ""
    # Decompose accents, drop diacritics
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # Now strip remaining non-ascii safely
    return s.encode("ascii", "ignore").decode("ascii")


@dataclass(frozen=True)
class DatasetSong:
    track_id: str
    track_name: str
    artist: str
    genre: str
    # Audio sliders 0-100 (all floats rounded to 1 decimal in scaler, but we store as float)
    sliders: Dict[str, float]
    # Search query in "Song by Artist" format (ASCII-safe)
    search_query: str
    # Optional probability factor (for weighted sampling). Defaults to 1.0 if missing.
    prob_factor: float = 1.0


class MusicDataset:
    def __init__(self, csv_path: Path = DATASET_PATH):
        self._csv_path = csv_path
        self._loaded = False
        self._songs: List[DatasetSong] = []

    def _load(self) -> None:
        if self._loaded:
            return
        if not self._csv_path.exists():
            raise FileNotFoundError(f"Dataset not found at {self._csv_path}")

        t0 = time.perf_counter()
        songs: List[DatasetSong] = []
        with self._csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                track_id = str(row.get("track_id", "")).strip()
                track_name_raw = str(row.get("track_name", "")).strip()
                # Support both schemas:
                # - curated_music_data.csv: artists, track_genre
                # - spotify_data_weighted*.csv: artist_name, genre
                artists_raw = str(row.get("artists") or row.get("artist_name") or "").strip()
                genre_raw = str(row.get("track_genre") or row.get("genre") or "").strip()

                # Use first listed artist for search query
                artist_primary = artists_raw.split(";")[0].strip() if artists_raw else ""

                # Convert dataset units -> sliders
                sliders = db_to_llm_features(
                    {
                        "energy": row.get("energy"),
                        "valence": row.get("valence"),
                        "loudness": row.get("loudness"),
                        "speechiness": row.get("speechiness"),
                        "acousticness": row.get("acousticness"),
                        "instrumentalness": row.get("instrumentalness"),
                        "liveness": row.get("liveness"),
                        "tempo": row.get("tempo"),
                    }
                )

                # Ensure all expected keys exist (fill missing with midpoint)
                for k in (
                    "energy",
                    "valence",
                    "loudness",
                    "speechiness",
                    "acousticness",
                    "instrumentalness",
                    "liveness",
                    "tempo",
                ):
                    sliders.setdefault(k, 50.0)

                track_name = _ascii_safe(track_name_raw)
                artist = _ascii_safe(artist_primary or "Unknown")
                genre = _ascii_safe(genre_raw)
                search_query = _ascii_safe(f"{track_name_raw} by {artist_primary or 'Unknown'}")

                pf = _to_float(row.get("prob_factor"))
                prob_factor = max(0.0, pf) if pf is not None else 1.0

                songs.append(
                    DatasetSong(
                        track_id=track_id,
                        track_name=track_name,
                        artist=artist,
                        genre=genre,
                        sliders=sliders,
                        search_query=search_query,
                        prob_factor=prob_factor,
                    )
                )

        self._songs = songs
        self._loaded = True
        dt = time.perf_counter() - t0
        logger.info(f"Loaded dataset: {len(self._songs)} songs from {self._csv_path} (took {dt:.1f}s)")

    @staticmethod
    def _distance(target: Dict[str, float], song: DatasetSong) -> float:
        # Weighted L1 in slider space.
        # Emphasize energy/valence/instrumentalness; still consider others.
        weights = {
            "energy": 2.0,
            "valence": 2.0,
            "instrumentalness": 1.5,
            "speechiness": 1.2,
            "acousticness": 1.2,
            "loudness": 1.0,
            "tempo": 1.0,
            "liveness": 0.7,
        }
        d = 0.0
        for k, w in weights.items():
            tv = float(target.get(k, 50.0))
            sv = float(song.sliders.get(k, 50.0))
            d += w * abs(tv - sv)
        return d

    def nearest_matches(self, target_sliders: Dict[str, Any], n: int = 100) -> List[DatasetSong]:
        self._load()

        # Normalize target sliders to floats
        target: Dict[str, float] = {}
        for k in (
            "energy",
            "valence",
            "loudness",
            "speechiness",
            "acousticness",
            "instrumentalness",
            "liveness",
            "tempo",
        ):
            try:
                target[k] = float(target_sliders.get(k, 50))
            except Exception:
                target[k] = 50.0

        # Simple partial selection via sort (OK for small datasets; for very large datasets
        # this will be slow, but we are intentionally trying the simple path first).
        ranked = sorted(self._songs, key=lambda s: self._distance(target, s))
        return ranked[: max(1, int(n))]

    def sample_for_prompt(
        self,
        target_sliders: Dict[str, Any],
        *,
        music_filters: Optional[Dict[str, Any]] = None,
        match_pool_size: int = 100,
        base_pool_size: Optional[int] = None,
        prompt_pick_count: int = 10,
        exclude_played_within_hours: float = 24.0,
        exclude_if_played_today: bool = True,
        boost_genres: Optional[List[str]] = None,
        boost_factor: float = 4.0,
        max_energy_delta: float = 5.0,
        max_valence_delta: float = 10.0,
        seed: Optional[int] = None,
    ) -> Tuple[List[DatasetSong], List[DatasetSong]]:
        """
        Returns (pool_100, prompt_10).
        """
        # Start with a larger nearest-neighbor set so we can filter out recently played
        # without ending up with too few candidates.
        base_pool_n = int(base_pool_size) if base_pool_size is not None else max(match_pool_size * 5, match_pool_size)
        base_pool_n = max(match_pool_size, base_pool_n)
        base_pool = self.nearest_matches(target_sliders, n=base_pool_n)
        logger.info(
            "Dataset sampling: base_pool=%s match_pool=%s prompt_pick=%s exclude_today=%s exclude_within_h=%s seed=%s target=%s",
            len(base_pool),
            match_pool_size,
            prompt_pick_count,
            exclude_if_played_today,
            exclude_played_within_hours,
            seed,
            {k: target_sliders.get(k) for k in ('energy','valence','loudness','speechiness','acousticness','instrumentalness','liveness','tempo')},
        )

        # Debug: energy-only closeness distribution (to answer: "were there 100 songs closer in energy?")
        try:
            tgt_e = float(target_sliders.get("energy", 50))
        except Exception:
            tgt_e = 50.0

        def energy_stats(songs: List[DatasetSong]) -> Dict[str, Any]:
            diffs = [abs(float(s.sliders.get("energy", 50.0)) - tgt_e) for s in songs]
            diffs_sorted = sorted(diffs)
            return {
                "count": len(diffs_sorted),
                "within_5": sum(1 for d in diffs_sorted if d <= 5),
                "within_10": sum(1 for d in diffs_sorted if d <= 10),
                "within_15": sum(1 for d in diffs_sorted if d <= 15),
                "p50": diffs_sorted[int(0.50 * (len(diffs_sorted) - 1))] if diffs_sorted else None,
                "p90": diffs_sorted[int(0.90 * (len(diffs_sorted) - 1))] if diffs_sorted else None,
                "max": diffs_sorted[-1] if diffs_sorted else None,
            }

        base_e = energy_stats(base_pool)
        logger.info(
            "Energy closeness (target=%.1f) base_pool=%s within5=%s within10=%s within15=%s p50=%s p90=%s max=%s",
            tgt_e,
            base_e["count"],
            base_e["within_5"],
            base_e["within_10"],
            base_e["within_15"],
            base_e["p50"],
            base_e["p90"],
            base_e["max"],
        )

        # Filter out recently played songs using the played_songs DB.
        # Keep this lightweight: only check items in base_pool (not full dataset).
        pool: List[DatasetSong] = []
        stats_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        seen_track_ids: set[str] = set()

        try:
            from app.models.played_songs import get_song_play_stats
        except Exception:
            get_song_play_stats = None

        def is_recently_played(s: DatasetSong) -> bool:
            if not get_song_play_stats:
                return False
            key = (s.track_name, s.artist)
            if key not in stats_cache:
                stats_cache[key] = get_song_play_stats(title=s.track_name, artist=s.artist)
            st = stats_cache[key]
            if not st.get("found"):
                return False
            if exclude_if_played_today and (st.get("plays_today") or 0) > 0:
                return True
            hrs = st.get("hours_since_last")
            if hrs is not None and hrs < exclude_played_within_hours:
                return True
            return False

        def norm(s: Any) -> str:
            return _ascii_safe(str(s or "")).strip().lower()

        def passes_music_filters(s: DatasetSong) -> bool:
            """
            Apply optional user-requested filters (artist/genre/keywords).

            These are "soft-hard" constraints: if present, we constrain provided songs to match.
            """
            if not isinstance(music_filters, dict) or not music_filters:
                return True

            song_genre = norm(s.genre)
            song_artist = norm(s.artist)
            song_q = norm(s.search_query)

            inc_genres = [norm(x) for x in (music_filters.get("include_genres") or []) if x]
            exc_genres = [norm(x) for x in (music_filters.get("exclude_genres") or []) if x]
            inc_artists = [norm(x) for x in (music_filters.get("include_artists") or []) if x]
            exc_artists = [norm(x) for x in (music_filters.get("exclude_artists") or []) if x]
            inc_kw = [norm(x) for x in (music_filters.get("include_keywords") or []) if x]

            # Exclusions first
            for g in exc_genres:
                if g and g in song_genre:
                    return False
            for a in exc_artists:
                if a and a in song_artist:
                    return False

            # Inclusions (if specified)
            if inc_genres:
                if not any(g and g in song_genre for g in inc_genres):
                    return False
            if inc_artists:
                if not any(a and a in song_artist for a in inc_artists):
                    return False
            if inc_kw:
                if not any(k and k in song_q for k in inc_kw):
                    return False

            return True

        def passes_hard_constraints(s: DatasetSong) -> bool:
            # Hard constraints make energy/valence dominate perception.
            try:
                e = float(s.sliders.get("energy", 50.0))
                v = float(s.sliders.get("valence", 50.0))
            except Exception:
                return True
            if abs(e - tgt_e) > float(max_energy_delta):
                return False
            if abs(v - tgt_v) > float(max_valence_delta):
                return False
            return True

        try:
            tgt_v = float(target_sliders.get("valence", 50))
        except Exception:
            tgt_v = 50.0

        rejected_recent = 0
        rejected_constraints = 0
        rejected_dupe = 0
        rejected_music_filters = 0

        for s in base_pool:
            if not passes_music_filters(s):
                rejected_music_filters += 1
                continue
            if is_recently_played(s):
                rejected_recent += 1
                continue
            if not passes_hard_constraints(s):
                rejected_constraints += 1
                continue
            if s.track_id and s.track_id in seen_track_ids:
                rejected_dupe += 1
                continue
            if s.track_id:
                seen_track_ids.add(s.track_id)
            pool.append(s)
            if len(pool) >= match_pool_size:
                break

        logger.info(
            "Dataset sampling filters: kept=%s rejected_music_filters=%s rejected_recent=%s rejected_constraints=%s rejected_dupe=%s (max_energy_delta=%s max_valence_delta=%s)",
            len(pool),
            rejected_music_filters,
            rejected_recent,
            rejected_constraints,
            rejected_dupe,
            max_energy_delta,
            max_valence_delta,
        )

        try:
            pool_e = energy_stats(pool)
            logger.info(
                "Energy closeness (target=%.1f) filtered_pool=%s within5=%s within10=%s within15=%s p50=%s p90=%s max=%s",
                tgt_e,
                pool_e["count"],
                pool_e["within_5"],
                pool_e["within_10"],
                pool_e["within_15"],
                pool_e["p50"],
                pool_e["p90"],
                pool_e["max"],
            )
        except Exception as e:
            logger.debug(f"Could not compute/log energy stats for filtered pool: {e}", exc_info=True)

        # IMPORTANT: Do NOT fall back to a looser pool. If hard constraints make the
        # pool smaller than match_pool_size, we accept fewer candidates.

        if seed is not None:
            rng = random.Random(seed)
        else:
            rng = random

        if len(pool) <= prompt_pick_count:
            return pool, pool

        # Build numeric target for distance calc (nearest_matches does similar)
        target: Dict[str, float] = {}
        for k in ("energy", "valence", "loudness", "speechiness", "acousticness", "instrumentalness", "liveness", "tempo"):
            try:
                target[k] = float(target_sliders.get(k, 50))
            except Exception:
                target[k] = 50.0

        # Weighted sampling (without replacement):
        # - favor preferred genres (boost_factor)
        # - ALSO favor closer matches (downweight high-distance items)
        #
        # This keeps randomness while ensuring the provided list stays "close" to the vibe.
        boost_set = {norm(g) for g in (boost_genres or []) if g and str(g).strip()}

        def weight_for(song: DatasetSong) -> float:
            # Genre weight
            genre_w = float(boost_factor) if (boost_set and norm(song.genre) in boost_set) else 1.0

            # Distance weight: 1/(1+dist) keeps positive weights without huge dynamic range.
            d = self._distance(target, song)
            dist_w = 1.0 / (1.0 + max(0.0, d))

            # Probability factor (from dataset). Default is 1.0.
            pf = float(getattr(song, "prob_factor", 1.0) or 0.0)
            return max(0.0, pf) * genre_w * dist_w

        # Weighted sampling without replacement (Efraimidis–Spirakis / A-ExpJ style).
        # Produces a size-k sample in one pass over the pool.
        prompt: List[DatasetSong] = []
        try:
            import math

            keyed: List[tuple[float, DatasetSong]] = []
            for s in pool:
                w = float(max(0.0, weight_for(s)))
                if w <= 0:
                    continue
                # A-ExpJ: key = log(U)/w (U in (0,1]); select largest keys
                u = max(1e-12, float(rng.random()))
                key = math.log(u) / w
                keyed.append((key, s))

            if len(keyed) >= prompt_pick_count:
                keyed.sort(key=lambda x: x[0])  # most negative -> least likely
                prompt = [s for _, s in keyed[-prompt_pick_count:]]
            else:
                # Not enough positive-weight items; fall back to uniform sample.
                remaining = list(pool)
                if len(remaining) <= prompt_pick_count:
                    prompt = remaining
                else:
                    prompt = rng.sample(remaining, k=prompt_pick_count)
        except Exception:
            # Fallback to simple loop if anything goes wrong
            prompt = []
            remaining = list(pool)
            while remaining and len(prompt) < prompt_pick_count:
                weights = [max(0.0, weight_for(s)) for s in remaining]
                total_w = sum(weights)
                if total_w <= 0:
                    pick = rng.choice(remaining)
                else:
                    r = rng.random() * total_w
                    acc = 0.0
                    pick = remaining[-1]
                    for s, w in zip(remaining, weights):
                        acc += w
                        if acc >= r:
                            pick = s
                            break
                prompt.append(pick)
                remaining.remove(pick)

        # Log the sampled 10 with their distances (proves random choice from close pool)
        try:
            for i, s in enumerate(prompt):
                d = self._distance(target, s)
                logger.info(
                    "ProvidedSong[%s]: %s (%s) dist=%.2f w=%.3f sliders=%s pf=%.3f",
                    i + 1,
                    s.search_query,
                    s.genre,
                    d,
                    weight_for(s),
                    s.sliders,
                    float(getattr(s, "prob_factor", 1.0) or 0.0),
                )
        except Exception as e:
            logger.warning(f"Could not log provided song sample details: {e}")

        return pool, prompt


class SqliteMusicDataset(MusicDataset):
    """
    SQLite-backed dataset access for large music tables.

    This avoids the expensive CSV load by querying `emi.db` directly.

    Table expected: `music_tracks_spotify` (created via table initializer + importer).
    """

    def __init__(
        self,
        *,
        db_path: Optional[Path] = None,
        table_name: str = "music_tracks_spotify",
        candidate_limit_min: int = 5000,
        candidate_limit_factor: int = 20,
        energy_window_slider: float = 5.0,
        valence_window_slider: float = 15.0,
        prefilter_limit: int = 50000,
        refine_limit: int = 10000,
    ):
        from app.assistant.utils.path_utils import get_data_dir
        self._db_path = db_path or (get_data_dir() / "emi.db")
        self._table = table_name
        self._candidate_limit_min = int(candidate_limit_min)
        self._candidate_limit_factor = int(candidate_limit_factor)
        self._energy_window_slider = float(energy_window_slider)
        self._valence_window_slider = float(valence_window_slider)
        self._prefilter_limit = int(prefilter_limit)
        self._refine_limit = int(refine_limit)

    @staticmethod
    def _distance(target: Dict[str, float], song: DatasetSong) -> float:
        # Keep identical to MusicDataset so behavior matches.
        return MusicDataset._distance(target, song)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _norm_key(s: Any) -> str:
        return str(s or "").strip().lower()

    @classmethod
    def _track_key(cls, title: Any, artist: Any) -> str:
        return f"{cls._norm_key(title)}|||{cls._norm_key(artist)}"

    @classmethod
    def _track_id_key(cls, track_id: Any) -> str:
        """
        Stable key for track-level weights when dataset has a unique track_id.
        """
        return f"spotify:{cls._norm_key(track_id)}"

    def _load_weights(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, float]]:
        """
        Load per-scope weights from normalized tables.

        Returns:
          {
            "track": { "<title>|||<artist>": factor, ... },   # sparse
            "artist": { "<artist>": factor, ... },           # sparse
            "genre": { "<genre>": factor, ... },             # small (~82)
          }
        """
        out: Dict[str, Dict[str, float]] = {"track": {}, "artist": {}, "genre": {}}

        try:
            for genre, factor in conn.execute("SELECT genre, factor FROM music_genre_weights").fetchall():
                out["genre"][self._norm_key(genre)] = float(factor) if factor is not None else 1.0
        except Exception as e:
            logger.debug(f"Could not load music_genre_weights: {e}", exc_info=True)
        try:
            for artist, factor in conn.execute("SELECT artist, factor FROM music_artist_weights").fetchall():
                out["artist"][self._norm_key(artist)] = float(factor) if factor is not None else 1.0
        except Exception as e:
            logger.debug(f"Could not load music_artist_weights: {e}", exc_info=True)
        try:
            for track_key, factor in conn.execute("SELECT track_key, factor FROM music_track_weights").fetchall():
                out["track"][self._norm_key(track_key)] = float(factor) if factor is not None else 1.0
        except Exception as e:
            logger.debug(f"Could not load music_track_weights: {e}", exc_info=True)

        return out

    def _load_genre_counts(self, conn: sqlite3.Connection) -> Dict[str, int]:
        """
        Load per-genre track counts for normalization.

        Expected table:
          music_genre_stats(genre TEXT PRIMARY KEY, track_count INTEGER, ...)
        """
        out: Dict[str, int] = {}
        try:
            rows = conn.execute("SELECT genre, track_count FROM music_genre_stats").fetchall()
            for genre, cnt in rows:
                key = self._norm_key(genre)
                try:
                    out[key] = max(0, int(cnt or 0))
                except Exception:
                    out[key] = 0
        except Exception:
            return out
        return out

    def _build_order_by_distance_sql(self) -> str:
        """
        Approximate the slider-distance in SQL so we can ORDER BY it.

        For 0..1 features we map to slider via *100.
        For loudness/tempo we map to slider using the configured p5..p95 scales.

        Note: currently reserved for future use; the active query path prefilters by
        energy/valence only and computes full distance in Python.
        """
        loud_lo = float(DEFAULT_LOUDNESS_DB_SCALE.lo)
        loud_hi = float(DEFAULT_LOUDNESS_DB_SCALE.hi)
        tempo_lo = float(DEFAULT_TEMPO_BPM_SCALE.lo)
        tempo_hi = float(DEFAULT_TEMPO_BPM_SCALE.hi)

        # Slider expressions in SQL
        energy_s = "COALESCE(energy, 0.5) * 100.0"
        valence_s = "COALESCE(valence, 0.5) * 100.0"
        instr_s = "COALESCE(instrumentalness, 0.5) * 100.0"
        speech_s = "COALESCE(speechiness, 0.5) * 100.0"
        acoustic_s = "COALESCE(acousticness, 0.5) * 100.0"
        live_s = "COALESCE(liveness, 0.5) * 100.0"

        loud_s = f"""
        (CASE
            WHEN loudness IS NULL THEN 50.0
            WHEN loudness <= {loud_lo} THEN 0.0
            WHEN loudness >= {loud_hi} THEN 100.0
            ELSE ((loudness - {loud_lo}) / ({loud_hi} - {loud_lo})) * 100.0
        END)
        """.strip()

        tempo_s = f"""
        (CASE
            WHEN tempo IS NULL THEN 50.0
            WHEN tempo <= {tempo_lo} THEN 0.0
            WHEN tempo >= {tempo_hi} THEN 100.0
            ELSE ((tempo - {tempo_lo}) / ({tempo_hi} - {tempo_lo})) * 100.0
        END)
        """.strip()

        # Weighted L1 distance in slider space.
        # Use placeholders (?) for target sliders in the exact same order below.
        return (
            " ("
            f" 2.0*ABS({energy_s} - ?) "
            f"+ 2.0*ABS({valence_s} - ?) "
            f"+ 1.5*ABS({instr_s} - ?) "
            f"+ 1.2*ABS({speech_s} - ?) "
            f"+ 1.2*ABS({acoustic_s} - ?) "
            f"+ 1.0*ABS({loud_s} - ?) "
            f"+ 1.0*ABS({tempo_s} - ?) "
            f"+ 0.7*ABS({live_s} - ?) "
            " ) "
        )

    def nearest_matches(self, target_sliders: Dict[str, Any], n: int = 100, *, music_filters: Optional[Dict[str, Any]] = None) -> List[DatasetSong]:
        # Normalize target sliders to floats
        target: Dict[str, float] = {}
        for k in (
            "energy",
            "valence",
            "loudness",
            "speechiness",
            "acousticness",
            "instrumentalness",
            "liveness",
            "tempo",
        ):
            try:
                target[k] = float(target_sliders.get(k, 50))
            except Exception:
                target[k] = 50.0

        # Candidate limit: ask SQLite for a gated set, then refine in Python.
        n_int = max(1, int(n))
        prefilter_limit = max(self._prefilter_limit, n_int * self._candidate_limit_factor)
        refine_limit = max(self._refine_limit, n_int)

        # Step 1: coarse gate (cheap, indexed): energy + valence window.
        try:
            tgt_e = float(target.get("energy", 50.0))
        except Exception:
            tgt_e = 50.0
        try:
            tgt_v = float(target.get("valence", 50.0))
        except Exception:
            tgt_v = 50.0

        # Convert slider delta to native delta for 0..1 columns (linear mapping).
        ew = max(0.0, float(self._energy_window_slider))
        energy_lo = max(0.0, min(1.0, (tgt_e - ew) / 100.0))
        energy_hi = max(0.0, min(1.0, (tgt_e + ew) / 100.0))

        vw = max(0.0, float(self._valence_window_slider))
        valence_lo = max(0.0, min(1.0, (tgt_v - vw) / 100.0))
        valence_hi = max(0.0, min(1.0, (tgt_v + vw) / 100.0))

        where = [
            f"{self._table}.prob_factor > 0",
            f"{self._table}.energy BETWEEN ? AND ?",
            f"{self._table}.valence BETWEEN ? AND ?",
        ]
        params: List[Any] = [energy_lo, energy_hi, valence_lo, valence_hi]

        # Apply optional music_filters (similar semantics to CSV path).
        def norm(s: Any) -> str:
            return str(s or "").strip().lower()

        if isinstance(music_filters, dict) and music_filters:
            inc_genres = [norm(x) for x in (music_filters.get("include_genres") or []) if x]
            exc_genres = [norm(x) for x in (music_filters.get("exclude_genres") or []) if x]
            inc_artists = [norm(x) for x in (music_filters.get("include_artists") or []) if x]
            exc_artists = [norm(x) for x in (music_filters.get("exclude_artists") or []) if x]
            inc_kw = [norm(x) for x in (music_filters.get("include_keywords") or []) if x]

            # Exclusions
            for g in exc_genres:
                where.append("LOWER(genre) NOT LIKE ?")
                params.append(f"%{g}%")
            for a in exc_artists:
                where.append("LOWER(artist_name) NOT LIKE ?")
                params.append(f"%{a}%")

            # Inclusions (AND across categories, OR within a category)
            if inc_genres:
                where.append("(" + " OR ".join(["LOWER(genre) LIKE ?"] * len(inc_genres)) + ")")
                params.extend([f"%{g}%" for g in inc_genres])
            if inc_artists:
                where.append("(" + " OR ".join(["LOWER(artist_name) LIKE ?"] * len(inc_artists)) + ")")
                params.extend([f"%{a}%" for a in inc_artists])
            if inc_kw:
                where.append(
                    "("
                    + " OR ".join(["LOWER(track_name) LIKE ?" for _ in inc_kw] + ["LOWER(artist_name) LIKE ?" for _ in inc_kw])
                    + ")"
                )
                params.extend([f"%{k}%" for k in inc_kw])
                params.extend([f"%{k}%" for k in inc_kw])

        where_sql = " AND ".join(where) if where else "1=1"

        # Order by just energy+valence closeness (cheap) to keep prefilter relevant.
        # Full distance is computed in Python in step 2.
        order_expr = (
            " ("
            " 2.0*ABS(COALESCE(energy, 0.5) * 100.0 - ?) "
            " + 2.0*ABS(COALESCE(valence, 0.5) * 100.0 - ?) "
            " ) "
        )
        order_params = [target["energy"], target["valence"]]

        sql = f"""
            SELECT
                track_id,
                track_name,
                artist_name,
                genre,
                prob_factor,
                energy, valence, loudness, speechiness, acousticness, instrumentalness, liveness, tempo
            FROM {self._table}
            WHERE {where_sql}
            ORDER BY {order_expr} ASC
            LIMIT ?
        """

        rows: List[Tuple[Any, ...]] = []
        weights: Dict[str, Dict[str, float]] = {"track": {}, "artist": {}, "genre": {}}
        genre_counts: Dict[str, int] = {}
        t0 = time.perf_counter()
        try:
            conn = self._connect()
        except Exception as e:
            logger.warning(f"SqliteMusicDataset connect failed: {e}")
            return []

        try:
            weights = self._load_weights(conn)
            genre_counts = self._load_genre_counts(conn)
            cur = conn.execute(sql, [*params, *order_params, int(prefilter_limit)])
            rows = cur.fetchall()
        finally:
            try:
                conn.close()
            except Exception as e:
                logger.debug(f"SqliteMusicDataset: failed to close sqlite connection: {e}", exc_info=True)

        dt = time.perf_counter() - t0
        logger.info(
            "SqliteMusicDataset: fetched %s gated row(s) in %.2fs (prefilter_limit=%s) from %s",
            len(rows),
            dt,
            prefilter_limit,
            self._table,
        )
        logger.info(
            "SqliteMusicDataset: gate energy=[%.1f±%.1f] valence=[%.1f±%.1f] -> native ranges e=[%.3f..%.3f] v=[%.3f..%.3f]",
            tgt_e,
            ew,
            tgt_v,
            vw,
            energy_lo,
            energy_hi,
            valence_lo,
            valence_hi,
        )

        songs: List[DatasetSong] = []
        for (
            track_id,
            track_name_raw,
            artist_name_raw,
            genre_raw,
            prob_factor,
            energy,
            valence,
            loudness,
            speechiness,
            acousticness,
            instrumentalness,
            liveness,
            tempo,
        ) in rows:
            track_name_raw = str(track_name_raw or "").strip()
            artist_name_raw = str(artist_name_raw or "").strip()
            if not track_name_raw:
                continue
            if not artist_name_raw:
                artist_name_raw = "Unknown"

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

            t = _ascii_safe(track_name_raw)
            a = _ascii_safe(artist_name_raw)
            g = _ascii_safe(str(genre_raw or "").strip())
            q = _ascii_safe(f"{track_name_raw} by {artist_name_raw}")

            pf = _to_float(prob_factor)
            pf = max(0.0, pf) if pf is not None else 1.0

            # Apply per-scope weights (track/artist/genre)
            try:
                tid = str(track_id or "").strip()
                if tid:
                    w_track = float(weights.get("track", {}).get(self._track_id_key(tid), 1.0))
                else:
                    w_track = float(weights.get("track", {}).get(self._track_key(track_name_raw, artist_name_raw), 1.0))
            except Exception:
                w_track = 1.0
            try:
                w_artist = float(weights.get("artist", {}).get(self._norm_key(artist_name_raw), 1.0))
            except Exception:
                w_artist = 1.0
            try:
                w_genre = float(weights.get("genre", {}).get(self._norm_key(genre_raw), 1.0))
            except Exception:
                w_genre = 1.0

            # Normalize by genre size so big genres don't dominate probability mass.
            # This makes total expected weight per genre ~ genre_factor (up to artist/track effects),
            # rather than proportional to number of tracks in that genre.
            try:
                gkey = self._norm_key(genre_raw)
                gcount = int(genre_counts.get(gkey, 0) or 0)
            except Exception:
                gcount = 0

            denom = float(max(1, gcount))
            effective_pf = (max(0.0, pf) * max(0.0, w_track) * max(0.0, w_artist) * max(0.0, w_genre)) / denom
            if effective_pf <= 0.0:
                continue

            songs.append(
                DatasetSong(
                    track_id=str(track_id or "").strip(),
                    track_name=t,
                    artist=a or "Unknown",
                    genre=g,
                    sliders=sliders,
                    search_query=q,
                    prob_factor=effective_pf,
                )
            )

        # Step 2: exact distance compute, prune obvious misses, keep the best refine_limit.
        ranked = sorted(songs, key=lambda s: self._distance(target, s))
        refined = ranked[:refine_limit]
        return refined[:n_int]

    def sample_for_prompt(self, *args, **kwargs):
        # Use the shared sampling logic (MusicDataset.sample_for_prompt) but with our
        # overridden nearest_matches() implementation.
        return super().sample_for_prompt(*args, **kwargs)

    def _sample_for_prompt_legacy(
        self,
        target_sliders: Dict[str, Any],
        *,
        music_filters: Optional[Dict[str, Any]] = None,
        match_pool_size: int = 100,
        prompt_pick_count: int = 10,
        exclude_played_within_hours: float = 24.0,
        exclude_if_played_today: bool = True,
        boost_genres: Optional[List[str]] = None,
        boost_factor: float = 4.0,
        max_energy_delta: float = 5.0,
        max_valence_delta: float = 10.0,
        seed: Optional[int] = None,
    ) -> Tuple[List[DatasetSong], List[DatasetSong]]:
        """
        Returns (pool_100, prompt_10).
        """
        # Start with a larger nearest-neighbor set so we can filter out recently played
        # without ending up with too few candidates.
        base_pool_n = max(match_pool_size * 5, match_pool_size)
        base_pool = self.nearest_matches(target_sliders, n=base_pool_n)
        logger.info(
            "Dataset sampling: base_pool=%s match_pool=%s prompt_pick=%s exclude_today=%s exclude_within_h=%s seed=%s target=%s",
            len(base_pool),
            match_pool_size,
            prompt_pick_count,
            exclude_if_played_today,
            exclude_played_within_hours,
            seed,
            {k: target_sliders.get(k) for k in ('energy','valence','loudness','speechiness','acousticness','instrumentalness','liveness','tempo')},
        )

        # Debug: energy-only closeness distribution (to answer: "were there 100 songs closer in energy?")
        try:
            tgt_e = float(target_sliders.get("energy", 50))
        except Exception:
            tgt_e = 50.0

        def energy_stats(songs: List[DatasetSong]) -> Dict[str, Any]:
            diffs = [abs(float(s.sliders.get("energy", 50.0)) - tgt_e) for s in songs]
            diffs_sorted = sorted(diffs)
            return {
                "count": len(diffs_sorted),
                "within_5": sum(1 for d in diffs_sorted if d <= 5),
                "within_10": sum(1 for d in diffs_sorted if d <= 10),
                "within_15": sum(1 for d in diffs_sorted if d <= 15),
                "p50": diffs_sorted[int(0.50 * (len(diffs_sorted) - 1))] if diffs_sorted else None,
                "p90": diffs_sorted[int(0.90 * (len(diffs_sorted) - 1))] if diffs_sorted else None,
                "max": diffs_sorted[-1] if diffs_sorted else None,
            }

        base_e = energy_stats(base_pool)
        logger.info(
            "Energy closeness (target=%.1f) base_pool=%s within5=%s within10=%s within15=%s p50=%s p90=%s max=%s",
            tgt_e,
            base_e["count"],
            base_e["within_5"],
            base_e["within_10"],
            base_e["within_15"],
            base_e["p50"],
            base_e["p90"],
            base_e["max"],
        )

        # Filter out recently played songs using the played_songs DB.
        # Keep this lightweight: only check items in base_pool (not full dataset).
        pool: List[DatasetSong] = []
        stats_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        seen_track_ids: set[str] = set()

        try:
            from app.models.played_songs import get_song_play_stats
        except Exception:
            get_song_play_stats = None

        def is_recently_played(s: DatasetSong) -> bool:
            if not get_song_play_stats:
                return False
            key = (s.track_name, s.artist)
            if key not in stats_cache:
                stats_cache[key] = get_song_play_stats(title=s.track_name, artist=s.artist)
            st = stats_cache[key]
            if not st.get("found"):
                return False
            if exclude_if_played_today and (st.get("plays_today") or 0) > 0:
                return True
            hrs = st.get("hours_since_last")
            if hrs is not None and hrs < exclude_played_within_hours:
                return True
            return False

        def norm(s: Any) -> str:
            return _ascii_safe(str(s or "")).strip().lower()

        def passes_music_filters(s: DatasetSong) -> bool:
            """
            Apply optional user-requested filters (artist/genre/keywords).

            These are "soft-hard" constraints: if present, we constrain provided songs to match.
            """
            if not isinstance(music_filters, dict) or not music_filters:
                return True

            song_genre = norm(s.genre)
            song_artist = norm(s.artist)
            song_q = norm(s.search_query)

            inc_genres = [norm(x) for x in (music_filters.get("include_genres") or []) if x]
            exc_genres = [norm(x) for x in (music_filters.get("exclude_genres") or []) if x]
            inc_artists = [norm(x) for x in (music_filters.get("include_artists") or []) if x]
            exc_artists = [norm(x) for x in (music_filters.get("exclude_artists") or []) if x]
            inc_kw = [norm(x) for x in (music_filters.get("include_keywords") or []) if x]

            # Exclusions first
            for g in exc_genres:
                if g and g in song_genre:
                    return False
            for a in exc_artists:
                if a and a in song_artist:
                    return False

            # Inclusions (if specified)
            if inc_genres:
                if not any(g and g in song_genre for g in inc_genres):
                    return False
            if inc_artists:
                if not any(a and a in song_artist for a in inc_artists):
                    return False
            if inc_kw:
                if not any(k and k in song_q for k in inc_kw):
                    return False

            return True

        def passes_hard_constraints(s: DatasetSong) -> bool:
            # Hard constraints make energy/valence dominate perception.
            try:
                e = float(s.sliders.get("energy", 50.0))
                v = float(s.sliders.get("valence", 50.0))
            except Exception:
                return True
            if abs(e - tgt_e) > float(max_energy_delta):
                return False
            if abs(v - tgt_v) > float(max_valence_delta):
                return False
            return True

        try:
            tgt_v = float(target_sliders.get("valence", 50))
        except Exception:
            tgt_v = 50.0

        rejected_recent = 0
        rejected_constraints = 0
        rejected_dupe = 0
        rejected_music_filters = 0

        for s in base_pool:
            if not passes_music_filters(s):
                rejected_music_filters += 1
                continue
            if is_recently_played(s):
                rejected_recent += 1
                continue
            if not passes_hard_constraints(s):
                rejected_constraints += 1
                continue
            if s.track_id and s.track_id in seen_track_ids:
                rejected_dupe += 1
                continue
            if s.track_id:
                seen_track_ids.add(s.track_id)
            pool.append(s)
            if len(pool) >= match_pool_size:
                break

        logger.info(
            "Dataset sampling filters: kept=%s rejected_music_filters=%s rejected_recent=%s rejected_constraints=%s rejected_dupe=%s (max_energy_delta=%s max_valence_delta=%s)",
            len(pool),
            rejected_music_filters,
            rejected_recent,
            rejected_constraints,
            rejected_dupe,
            max_energy_delta,
            max_valence_delta,
        )

        try:
            pool_e = energy_stats(pool)
            logger.info(
                "Energy closeness (target=%.1f) filtered_pool=%s within5=%s within10=%s within15=%s p50=%s p90=%s max=%s",
                tgt_e,
                pool_e["count"],
                pool_e["within_5"],
                pool_e["within_10"],
                pool_e["within_15"],
                pool_e["p50"],
                pool_e["p90"],
                pool_e["max"],
            )
        except Exception as e:
            logger.debug(f"Could not compute/log energy stats for filtered pool: {e}", exc_info=True)

        # IMPORTANT: Do NOT fall back to a looser pool. If hard constraints make the
        # pool smaller than match_pool_size, we accept fewer candidates.

        if seed is not None:
            rng = random.Random(seed)
        else:
            rng = random

        if len(pool) <= prompt_pick_count:
            return pool, pool

        # Build numeric target for distance calc (nearest_matches does similar)
        target: Dict[str, float] = {}
        for k in ("energy", "valence", "loudness", "speechiness", "acousticness", "instrumentalness", "liveness", "tempo"):
            try:
                target[k] = float(target_sliders.get(k, 50))
            except Exception:
                target[k] = 50.0

        # Weighted sampling (without replacement):
        # - favor preferred genres (boost_factor)
        # - ALSO favor closer matches (downweight high-distance items)
        #
        # This keeps randomness while ensuring the provided list stays "close" to the vibe.
        boost_set = {norm(g) for g in (boost_genres or []) if g and str(g).strip()}

        def weight_for(song: DatasetSong) -> float:
            # Genre weight
            genre_w = float(boost_factor) if (boost_set and norm(song.genre) in boost_set) else 1.0

            # Distance weight: 1/(1+dist) keeps positive weights without huge dynamic range.
            d = self._distance(target, song)
            dist_w = 1.0 / (1.0 + max(0.0, d))
            # Probability factor (from dataset). Default is 1.0.
            pf = float(getattr(song, "prob_factor", 1.0) or 0.0)
            return max(0.0, pf) * genre_w * dist_w

        prompt: List[DatasetSong] = []
        remaining = list(pool)
        while remaining and len(prompt) < prompt_pick_count:
            weights = [max(0.0, weight_for(s)) for s in remaining]
            total_w = sum(weights)
            if total_w <= 0:
                # fallback to uniform
                pick = rng.choice(remaining)
            else:
                r = rng.random() * total_w
                acc = 0.0
                pick = remaining[-1]
                for s, w in zip(remaining, weights):
                    acc += w
                    if acc >= r:
                        pick = s
                        break
            prompt.append(pick)
            remaining.remove(pick)

        # Log the sampled 10 with their distances (proves random choice from close pool)
        try:
            for i, s in enumerate(prompt):
                d = self._distance(target, s)
                logger.info(
                    "ProvidedSong[%s]: %s (%s) dist=%.2f w=%.1f sliders=%s",
                    i + 1,
                    s.search_query,
                    s.genre,
                    d,
                    weight_for(s),
                    s.sliders,
                )
        except Exception as e:
            logger.warning(f"Could not log provided song sample details: {e}")

        return pool, prompt

