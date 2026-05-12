"""
Entity Catalog - Fast in-memory index for entity detection.

Reads from entity_card_v2 directly. Each active card contributes:
  - card_title  → the canonical entity name (the catalog key)
  - card_aliases → additional match strings

This serves both kg-backed cards (where the builder populates
card_title/card_aliases from the underlying KG node) and non-kg
cards (where the user provides them directly). No KG join.
"""

import threading
from collections import defaultdict
from typing import Dict, Set, Tuple, List
from app.models.base import get_session
from app.assistant.entity_management.entity_card_v2 import EntityCardV2
from app.assistant.utils.logging_config import get_logger
import json

logger = get_logger(__name__)


def _ensure_list(value) -> List[str]:
    """Helper to normalize JSONEncodedList or similar fields into a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except Exception:
            # Fall back, treat the string itself as a single alias
            return [value]
    return [str(value)]


def _normalize_name_to_tokens(name: str) -> List[str]:
    """
    Normalize an entity name or alias into tokens for matching.
    Lowercase, strip surrounding punctuation, drop possessive 's on the last token.
    """
    if not name:
        return []
    # Collapse whitespace and lowercase
    name = " ".join(name.split()).lower()
    if not name:
        return []
    raw_tokens = name.split()
    tokens: List[str] = []
    for idx, raw in enumerate(raw_tokens):
        token = raw
        # Strip leading punctuation
        while token and not token[0].isalnum():
            token = token[1:]
        # Strip trailing punctuation except apostrophe
        while token and not token[-1].isalnum() and token[-1] not in ("'", "'"):
            token = token[:-1]
        if not token:
            continue
        # Only strip possessive 's or 's at the end of the last token
        if idx == len(raw_tokens) - 1:
            if token.endswith("'s") or token.endswith("'s"):
                token = token[:-2]
        if token:
            tokens.append(token)
    return tokens


class EntityCatalog:
    """
    Singleton style catalog that preloads active EntityCards and builds indices for fast lookup.
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # normalized single token -> set of canonical entity names
        self.single_word_index: Dict[str, Set[str]] = defaultdict(set)
        # phrase length -> (tuple of tokens -> set of canonical names)
        self.multi_word_index: Dict[int, Dict[Tuple[str, ...], Set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.phrase_lengths: Set[int] = set()
        self.all_canonical_entities: Set[str] = set()
        self._loaded = False
        self._load_from_db()

    @classmethod
    def instance(cls) -> "EntityCatalog":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def reload_from_db(self) -> None:
        """Force reload of catalog from DB. Useful in tests or after bulk updates."""
        with self._lock:
            self.single_word_index.clear()
            self.multi_word_index.clear()
            self.phrase_lengths.clear()
            self.all_canonical_entities.clear()
            self._loaded = False
            self._load_from_db()

    def _load_from_db(self) -> None:
        """Internal loader. Assumes caller holds lock when needed.

        Scans entity_card_v2 directly. card_title is the canonical name;
        card_aliases is a JSON list of additional match strings. Both kg
        and non-kg cards index the same way.
        """
        if self._loaded:
            return

        logger.info("Loading Entity Catalog from database...")
        session = get_session()
        try:
            rows = (
                session.query(EntityCardV2.card_title, EntityCardV2.card_aliases)
                .filter(EntityCardV2.is_active == True)  # noqa: E712
                .filter(EntityCardV2.card_title.isnot(None))
                .all()
            )
        except Exception as e:
            logger.debug(
                "EntityCatalog: could not load entity cards (tables may not exist yet on fresh install): %s", e
            )
            rows = []
            self._loaded = True
            return
        finally:
            session.close()

        entity_count = 0
        single_word_count = 0
        multi_word_count = 0

        for card_title, aliases in rows:
            canonical = card_title
            if not canonical:
                continue

            self.all_canonical_entities.add(canonical)
            entity_count += 1

            names: List[str] = [canonical]
            names.extend(_ensure_list(aliases))

            for raw_name in names:
                tokens = _normalize_name_to_tokens(raw_name)
                if not tokens:
                    continue

                if len(tokens) == 1:
                    t = tokens[0]
                    self.single_word_index[t].add(canonical)
                    single_word_count += 1
                else:
                    length = len(tokens)
                    key = tuple(tokens)
                    self.multi_word_index[length][key].add(canonical)
                    self.phrase_lengths.add(length)
                    multi_word_count += 1

        self._loaded = True
        logger.info(f"✅ Entity Catalog loaded: {entity_count} entities, {single_word_count} single-word terms, {multi_word_count} multi-word terms")
        logger.info(f"   Phrase lengths: {sorted(self.phrase_lengths) if self.phrase_lengths else 'none'}")


# Convenience function if you prefer a functional style
def get_entity_catalog() -> EntityCatalog:
    return EntityCatalog.instance()

