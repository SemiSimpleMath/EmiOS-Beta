from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir
from app.assistant.music_manager import get_music_manager

logger = get_logger(__name__)


DEFAULT_STOREFRONT = "us"
DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _cache_path() -> Path:
    return get_resources_dir() / "status" / "apple_music_related_artists_cache.json"


def _now() -> float:
    return time.time()


def _norm(s: str) -> str:
    return (s or "").strip().lower()


@dataclass(frozen=True)
class RelatedArtistsResult:
    anchor: str
    storefront: str
    related_artists: List[str]
    fetched_at_utc: str
    source: str  # "cache" | "api"


def _load_cache() -> Dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {"schema_version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {"schema_version": 1, "entries": {}}
        data.setdefault("schema_version", 1)
        data.setdefault("entries", {})
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data
    except Exception:
        return {"schema_version": 1, "entries": {}}


def _save_cache(cache: Dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _cache_key(*, anchor: str, storefront: str) -> str:
    return f"{_norm(storefront)}::{_norm(anchor)}"


def _apple_headers(*, origin: Optional[str] = None) -> Dict[str, str]:
    token = get_music_manager().generate_developer_token(origin=origin)
    if not token:
        raise RuntimeError("Apple Music developer token not available")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _search_artist_id(*, anchor: str, storefront: str) -> Optional[str]:
    term = (anchor or "").strip()
    if not term:
        return None

    url = f"https://api.music.apple.com/v1/catalog/{storefront}/search"
    params = {"term": term, "types": "artists", "limit": 5}
    resp = requests.get(url, headers=_apple_headers(), params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json() or {}
    artists = (((data.get("results") or {}).get("artists") or {}).get("data") or [])
    if not isinstance(artists, list) or not artists:
        return None
    # Take top result
    top = artists[0] if isinstance(artists[0], dict) else None
    artist_id = top.get("id") if isinstance(top, dict) else None
    return str(artist_id) if artist_id else None


def _fetch_related_artists(*, artist_id: str, storefront: str) -> List[str]:
    url = f"https://api.music.apple.com/v1/catalog/{storefront}/artists/{artist_id}/relationships/related-artists"
    resp = requests.get(url, headers=_apple_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json() or {}
    items = data.get("data") or []
    out: List[str] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        attrs = it.get("attributes") if isinstance(it.get("attributes"), dict) else {}
        name = attrs.get("name") if isinstance(attrs, dict) else None
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    # De-dupe stable
    seen = set()
    uniq: List[str] = []
    for a in out:
        k = _norm(a)
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return uniq


def get_related_artists_cached(
    *,
    anchor_artist: str,
    storefront: str = DEFAULT_STOREFRONT,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_related: int = 20,
) -> RelatedArtistsResult:
    """
    Return a cached list of Apple Music related artists for an anchor artist name.
    Persistent cache lives under resources/status/.
    """
    anchor = (anchor_artist or "").strip()
    sf = (storefront or DEFAULT_STOREFRONT).strip().lower()
    if not anchor:
        return RelatedArtistsResult(anchor="", storefront=sf, related_artists=[], fetched_at_utc="", source="cache")

    cache = _load_cache()
    entries: Dict[str, Any] = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    key = _cache_key(anchor=anchor, storefront=sf)
    now = _now()

    row = entries.get(key) if isinstance(entries.get(key), dict) else None
    if row:
        try:
            fetched_at = float(row.get("fetched_at_epoch", 0))
        except Exception:
            fetched_at = 0.0
        if fetched_at and (now - fetched_at) < float(ttl_seconds):
            rel = row.get("related_artists") if isinstance(row.get("related_artists"), list) else []
            rel2 = [str(x).strip() for x in rel if isinstance(x, str) and x.strip()]
            return RelatedArtistsResult(
                anchor=anchor,
                storefront=sf,
                related_artists=rel2[: max(0, int(max_related))],
                fetched_at_utc=str(row.get("fetched_at_utc") or ""),
                source="cache",
            )

    # Fetch from API and update cache
    try:
        artist_id = _search_artist_id(anchor=anchor, storefront=sf)
        related = _fetch_related_artists(artist_id=artist_id, storefront=sf) if artist_id else []
    except Exception as e:
        logger.warning("Apple related artists fetch failed for '%s' (storefront=%s): %s", anchor, sf, e)
        related = []

    fetched_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    entries[key] = {
        "anchor": anchor,
        "storefront": sf,
        "artist_id": artist_id if "artist_id" in locals() else None,
        "related_artists": related[: max(0, int(max_related))],
        "fetched_at_epoch": now,
        "fetched_at_utc": fetched_utc,
    }
    cache["entries"] = entries
    try:
        _save_cache(cache)
    except Exception as e:
        logger.debug("Could not persist Apple related artists cache: %s", e, exc_info=True)

    return RelatedArtistsResult(
        anchor=anchor,
        storefront=sf,
        related_artists=related[: max(0, int(max_related))],
        fetched_at_utc=fetched_utc,
        source="api",
    )

