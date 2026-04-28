from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.dj_manager.query_utils import parse_search_query, build_search_query

logger = get_logger(__name__)


@dataclass
class ScoredCandidate:
    search_query: str
    reasoning: str
    title: str
    artist: str
    score: float
    probability: float = 0.0
    # Optional dataset meta (for UI + debugging)
    track_id: str | None = None
    genre: str | None = None
    sliders: Dict[str, Any] | None = None
    prob_factor: float | None = None


class CandidateSelector:
    def __init__(self):
        self._backups: List[ScoredCandidate] = []

    def clear_backups(self) -> None:
        self._backups.clear()

    def has_backups(self) -> bool:
        return len(self._backups) > 0

    def backup_count(self) -> int:
        return len(self._backups)

    def peek_backups(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Non-destructively return top-N backups as dicts (in current sorted order).
        Useful for sending a small candidate batch to the player.
        """
        out: List[Dict[str, Any]] = []
        n = max(0, int(limit or 0))
        for s in (self._backups or [])[:n]:
            out.append(
                {
                    "track_id": s.track_id or "",
                    "title": s.title,
                    "artist": s.artist,
                    "search_query": s.search_query,
                    "reasoning": s.reasoning,
                    "genre": s.genre,
                    "sliders": s.sliders,
                    "prob_factor": s.prob_factor,
                }
            )
        return out

    def pop_backup(self) -> Optional[ScoredCandidate]:
        if not self._backups:
            return None
        return self._backups.pop(0)

    def choose(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Returns:
          {
            "chosen": {"title": ..., "artist": ..., "search_query": ..., "reasoning": ...},
            "backups_count": int
          }

        Note:
        - Backup candidates are stored internally and can be retrieved via `pop_backup()`.
        """
        if not candidates:
            return None

        try:
            import random
            from app.models.played_songs import score_song_candidate
        except Exception as e:
            logger.error("Candidate scoring unavailable: %s", e)
            logger.debug("candidate scoring unavailable exception details", exc_info=True)
            score_song_candidate = None

        scored: List[ScoredCandidate] = []
        for c in candidates:
            reasoning = c.get("reasoning", "")

            title = (c.get("title") or "").strip()
            artist = (c.get("artist") or "").strip()
            track_id = str(c.get("track_id") or "").strip() or None
            genre = (c.get("genre") or None)
            sliders = c.get("sliders") if isinstance(c.get("sliders"), dict) else None
            try:
                prob_factor = float(c.get("prob_factor")) if c.get("prob_factor") is not None else None
            except Exception:
                prob_factor = None

            # Backward-compatible fallback if an older caller still passes search_query only.
            if (not title or not artist) and (c.get("search_query") or "").strip():
                t2, a2 = parse_search_query(str(c.get("search_query")))
                title = title or t2
                artist = artist or a2

            if not title:
                continue
            if not artist:
                artist = "Unknown"

            query = build_search_query(title, artist)

            score = 1.0
            if score_song_candidate:
                try:
                    score = float(score_song_candidate(title, artist))
                except Exception:
                    score = 1.0

            scored.append(
                ScoredCandidate(
                    search_query=query,
                    reasoning=reasoning,
                    title=title,
                    artist=artist,
                    score=score,
                    track_id=track_id,
                    genre=str(genre).strip() if isinstance(genre, str) and genre.strip() else None,
                    sliders=sliders,
                    prob_factor=prob_factor,
                )
            )

        if not scored:
            return None

        scores = [max(0.0, s.score) for s in scored]
        total = sum(scores)

        if total > 0:
            for s in scored:
                s.probability = (max(0.0, s.score) / total) * 100.0

        for i, s in enumerate(scored):
            logger.info(
                f"Candidate [{i+1}] {s.title[:35]:<35} by {s.artist[:20]:<20} "
                f"| score={s.score:.3f} | prob={s.probability:5.1f}%"
            )

        if total <= 0:
            chosen = scored[0]
        else:
            chosen = random.choices(scored, weights=scores, k=1)[0]

        remaining = [s for s in scored if s is not chosen]
        remaining.sort(key=lambda x: x.score, reverse=True)
        self._backups = remaining

        logger.info(
            f"Selected: '{chosen.title}' by {chosen.artist} (prob was {chosen.probability:.1f}%)"
        )

        return {
            "chosen": {
                "track_id": chosen.track_id or "",
                "title": chosen.title,
                "artist": chosen.artist,
                "search_query": chosen.search_query,
                "reasoning": chosen.reasoning,
                "genre": chosen.genre,
                "sliders": chosen.sliders,
                "prob_factor": chosen.prob_factor,
            },
            "backups_count": len(remaining),
        }

    def choose_uniform(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Choose a candidate uniformly at random (no LLM involvement).

        Still computes cooldown/history scores so we can order backups sanely and
        avoid repeats when falling back.
        """
        if not candidates:
            return None

        try:
            import random
            from app.models.played_songs import score_song_candidate
        except Exception as e:
            logger.error("Candidate scoring unavailable: %s", e)
            logger.debug("candidate scoring unavailable exception details", exc_info=True)
            score_song_candidate = None
            import random  # type: ignore

        scored: List[ScoredCandidate] = []
        for c in candidates:
            reasoning = c.get("reasoning", "") or "Random pick from finalist pool"

            title = (c.get("title") or "").strip()
            artist = (c.get("artist") or "").strip()
            track_id = str(c.get("track_id") or "").strip() or None
            genre = (c.get("genre") or None)
            sliders = c.get("sliders") if isinstance(c.get("sliders"), dict) else None
            try:
                prob_factor = float(c.get("prob_factor")) if c.get("prob_factor") is not None else None
            except Exception:
                prob_factor = None

            if (not title or not artist) and (c.get("search_query") or "").strip():
                t2, a2 = parse_search_query(str(c.get("search_query")))
                title = title or t2
                artist = artist or a2

            if not title:
                continue
            if not artist:
                artist = "Unknown"

            query = build_search_query(title, artist)

            score = 1.0
            if score_song_candidate:
                try:
                    score = float(score_song_candidate(title, artist))
                except Exception:
                    score = 1.0

            scored.append(
                ScoredCandidate(
                    search_query=query,
                    reasoning=reasoning,
                    title=title,
                    artist=artist,
                    score=score,
                    track_id=track_id,
                    genre=str(genre).strip() if isinstance(genre, str) and genre.strip() else None,
                    sliders=sliders,
                    prob_factor=prob_factor,
                )
            )

        if not scored:
            return None

        chosen = random.choice(scored)
        remaining = [s for s in scored if s is not chosen]
        remaining.sort(key=lambda x: x.score, reverse=True)
        self._backups = remaining

        logger.info(f"Selected (uniform): '{chosen.title}' by {chosen.artist}")

        return {
            "chosen": {
                "track_id": chosen.track_id or "",
                "title": chosen.title,
                "artist": chosen.artist,
                "search_query": chosen.search_query,
                "reasoning": chosen.reasoning,
                "genre": chosen.genre,
                "sliders": chosen.sliders,
                "prob_factor": chosen.prob_factor,
            },
            "backups_count": len(remaining),
        }
