"""
Entity detection against the preloaded EntityCatalog.

Detection only: EntityInjector (agent_runtime/services) owns rendering —
it detects entities from the RENDERED prompt via ``detect_entities_in_text``
and injects leveled cards in its second render pass. The legacy
text-injection lane that used to live here (inject_entity_cards_into_text /
inject_into_team_call and the global-blackboard duplicate check) was deleted
2026-07-08 — zero callers.
"""

from dataclasses import dataclass
from typing import Dict, List
from app.assistant.entity_management.entity_catalog import get_entity_catalog
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class EntityMatch:
    """
    Result of entity detection: the canonical entity name plus the alias form
    actually present in the source text (if it differs from the canonical name).
    """
    canonical: str
    alias: str | None = None  # e.g. "BAS" when canonical is "Broadway Art Studio"


class EntityCardInjector:
    """Detects catalog entities in text (single tokens + multi-word phrases,
    case-insensitive, possessive-tolerant, no substring matches)."""

    def _normalize_token(self, raw: str) -> str:
        """
        Normalize a raw token for catalog matching:
        - lower
        - strip leading/trailing non-alnum punctuation
        - strip trailing possessive "'s"
        - strip trailing apostrophe (e.g. "jukka'" -> "jukka")
        """
        if not raw:
            return ""
        token = str(raw).lower()

        # Strip leading punctuation
        while token and not token[0].isalnum():
            token = token[1:]
        # Strip trailing punctuation
        while token and not token[-1].isalnum():
            token = token[:-1]

        if not token:
            return ""

        # Possessive
        if token.endswith("'s"):
            token = token[:-2]
        # Trailing apostrophe
        if token.endswith("'"):
            token = token[:-1]

        return token

    @staticmethod
    def _display_form(raw: str) -> str:
        """
        Strip leading/trailing non-alnum chars and possessives from a raw word,
        preserving original casing. Used to recover the display alias from text.
        """
        s = raw
        while s and not s[0].isalnum():
            s = s[1:]
        while s and not s[-1].isalnum():
            s = s[:-1]
        if s.endswith("'s") or s.endswith("\u2019s"):
            s = s[:-2]
        if s.endswith("'") or s.endswith("\u2019"):
            s = s[:-1]
        return s

    def _detect_entities_with_matches(self, text: str) -> List[EntityMatch]:
        """
        Like detect_entities_in_text but returns EntityMatch objects that also carry
        the alias form actually present in the text (e.g. "BAS" for "Broadway Art Studio").
        The alias is set only when it differs from the canonical name (case-insensitively).
        """
        if not text:
            return []

        catalog = get_entity_catalog()

        # Build parallel lists: normalized tokens + display-form tokens (original casing)
        norm_tokens: List[str] = []
        disp_tokens: List[str] = []
        for raw in text.split():
            norm = self._normalize_token(raw)
            if norm:
                norm_tokens.append(norm)
                disp_tokens.append(self._display_form(raw) or norm)

        if not norm_tokens:
            return []

        # canonical → first display alias encountered
        found: Dict[str, str] = {}

        # Single word matches
        for i, t in enumerate(norm_tokens):
            if t in catalog.single_word_index:
                for canonical in catalog.single_word_index[t]:
                    if canonical not in found:
                        found[canonical] = disp_tokens[i]

        # Multi word phrase matches
        n = len(norm_tokens)
        if catalog.phrase_lengths:
            for i in range(n):
                for length in catalog.phrase_lengths:
                    if i + length > n:
                        continue
                    key = tuple(norm_tokens[i : i + length])
                    entity_map = catalog.multi_word_index.get(length)
                    if not entity_map:
                        continue
                    canonical_names = entity_map.get(key)
                    if canonical_names:
                        alias_form = " ".join(disp_tokens[i : i + length])
                        for canonical in canonical_names:
                            if canonical not in found:
                                found[canonical] = alias_form

        result: List[EntityMatch] = []
        for canonical in sorted(found.keys()):
            alias_used = found[canonical]
            alias = alias_used if alias_used.lower() != canonical.lower() else None
            result.append(EntityMatch(canonical=canonical, alias=alias))

        if result:
            logger.debug("Detected entities: %s", [(m.canonical, m.alias) for m in result])
        return result

    def detect_entities_in_text(self, text: str) -> List[str]:
        """
        Detect entity names present in the given text using the preloaded EntityCatalog.
        Matching rules:
        - Exact match on normalized single tokens or multi word phrases
        - Case insensitive
        - Allow possessive for the last word (Alex's -> Alex)
        - Do not match inside larger words (RAG does not match Ragged)
        """
        return [m.canonical for m in self._detect_entities_with_matches(text)]

