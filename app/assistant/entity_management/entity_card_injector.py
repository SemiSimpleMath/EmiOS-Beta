"""
Entity Card Injection Service
Handles intelligent injection of entity cards into chat and team calls with duplicate detection
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set
from app.models.base import get_session
from app.assistant.entity_management.entity_cards import (
    get_entity_card_for_prompt_injection_level,
    track_entity_card_usage_best_effort,
)
from app.assistant.entity_management.entity_catalog import get_entity_catalog
from app.assistant.ServiceLocator.service_locator import DI
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
    """
    Service for injecting entity cards into chat and team calls.

    Ownership model:
    - This class owns *rendering* and *injection* behavior.
    - For chat, this class performs best-effort duplicate prevention by checking the global blackboard.
    - For team calls, reinjection is allowed by default (but still capped).
    """


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

    def _tokenize_text(self, text: str) -> List[str]:
        """
        Tokenize and normalize user text into a list of tokens used for matching.
        Rules:
        - Lowercase
        - Strip leading and trailing punctuation
        - Strip possessive "'s" and trailing apostrophes
        """
        if not text:
            return []
        tokens: List[str] = []
        for raw in text.split():
            token = self._normalize_token(raw)
            if token:
                tokens.append(token)
        return tokens

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

    def get_entity_card_content(
        self,
        entity_name: str,
        *,
        track_usage: bool = True,
        agent_name: str | None = None,
        session_id: str | None = None,
        query_text: str | None = None,
    ) -> Optional[str]:
        """
        Get entity card content for injection if it exists
        """
        session = None
        try:
            session = get_session()
            card_content = get_entity_card_for_prompt_injection_level(session, entity_name, level=1)

            if track_usage and card_content:
                track_entity_card_usage_best_effort(
                    entity_name=entity_name,
                    agent_name=agent_name,
                    session_id=session_id,
                    query_text=query_text,
                )

            return card_content
        except Exception as e:
            logger.error("Error getting entity card for '%s': %s", entity_name, e)
            return None
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    logger.debug("entity_card_injector: session.close() failed", exc_info=True)
    
    def check_if_entity_injected_in_history(self, entity_name: str) -> bool:
        """
        Check if entity card has been injected in the current chat history
        Uses global blackboard to check message history
        """
        try:
            # Get all messages from global blackboard
            messages = DI.global_blackboard.get_messages()
            
            # Check for entity card injection messages using sub_data_type and metadata
            for msg in messages:
                try:
                    rid = getattr(msg, "room_id", None)
                    if isinstance(rid, str) and rid.strip():
                        continue
                    surface = getattr(msg, "room_surface", None)
                    if isinstance(surface, str) and surface.strip():
                        continue
                    data = getattr(msg, "data", None)
                    if isinstance(data, dict):
                        rid2 = data.get("room_id")
                        if isinstance(rid2, str) and rid2.strip():
                            continue
                        surface2 = data.get("room_surface")
                        if isinstance(surface2, str) and surface2.strip():
                            continue
                    meta = getattr(msg, "metadata", None)
                    if isinstance(meta, dict):
                        reply_to = meta.get("reply_to")
                        if isinstance(reply_to, dict):
                            rt = str(reply_to.get("type") or "").strip().lower()
                            if rt in {"slack", "twilio_sms", "telegram"}:
                                continue
                except Exception:
                    logger.debug("entity_card_injector: could not read reply_to metadata from message", exc_info=True)
                if (
                    "entity_card_injection" in (getattr(msg, "sub_data_type", []) or [])
                    and hasattr(msg, 'metadata') and msg.metadata
                    and 'entity_name' in msg.metadata
                    and msg.metadata['entity_name'] == entity_name
                ):
                    return True
                
                # Fallback: check content for entity context format.
                # Header may be "[Entity Context - Name]" or "[Entity Context - Alias (Name)]".
                if msg.content and (
                    f"[Entity Context - {entity_name}]" in msg.content
                    or f"({entity_name})]" in msg.content
                ):
                    return True
            
            return False
        except Exception as e:
            logger.error("Error checking entity injection history for '%s': %s", entity_name, e)
            return False
    
    def _rank_entities_for_injection(self, text: str, entities: List[EntityMatch]) -> List[EntityMatch]:
        """
        Rank entities for injection.
        Prefer entities that appear earlier in the text, then longer phrases, then more mentions.
        Searches for the alias form when available so ranking reflects where the user's word appears.
        """
        lowered = (text or "").lower()
        scored: List[Tuple] = []
        for match in entities:
            if not match.canonical:
                continue
            # Search for the alias the user actually typed, fall back to canonical
            search = (match.alias or match.canonical).lower()
            canonical_low = match.canonical.lower()
            if " " in search:
                first = lowered.find(search)
                if first < 0 and search != canonical_low:
                    first = lowered.find(canonical_low)
                count = lowered.count(search)
            else:
                m_list = list(re.finditer(rf"(?<!\w){re.escape(search)}(?!\w)", lowered))
                if not m_list and search != canonical_low:
                    m_list = list(re.finditer(rf"(?<!\w){re.escape(canonical_low)}(?!\w)", lowered))
                first = m_list[0].start() if m_list else 10**9
                count = len(m_list)
            scored.append((first, -len(canonical_low.split()), -count, match))
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        return [x[-1] for x in scored]

    def inject_entity_cards_into_text(
        self,
        text: str,
        context_type: str = "chat",
        *,
        max_entities: Optional[int] = None,
        track_usage: bool = True,
        agent_name: str | None = None,
        session_id: str | None = None,
        query_text: str | None = None,
    ) -> Tuple[str, List[str]]:
        """
        Inject entity cards into text and return enhanced text + list of injected entities
        
        Args:
            text: Original text to enhance
            context_type: Type of context ("chat" or "team_call")
        
        Returns:
            Tuple of (enhanced_text, list_of_injected_entities)
        """
        if not text:
            return text, []
        
        # Detect potential entities in the text
        detected_entities = self._detect_entities_with_matches(text)

        if not detected_entities:
            return text, []

        enhanced_text = text
        injected_entities = []

        ranked_entities = self._rank_entities_for_injection(text, detected_entities)

        for match in ranked_entities:
            if not self.should_inject_entity(match.canonical, context_type):
                continue

            card_content = self.get_entity_card_content(
                match.canonical,
                track_usage=track_usage,
                agent_name=agent_name,
                session_id=session_id,
                query_text=query_text,
            )
            if not card_content:
                continue

            enhanced_text = self._inject_entity_card(enhanced_text, match, card_content, context_type)
            injected_entities.append(match.canonical)
            logger.debug("Injected entity card for '%s' (alias=%s) in %s context", match.canonical, match.alias, context_type)
        
        return enhanced_text, injected_entities
    
    def should_inject_entity(self, entity_name: str, context_type: str) -> bool:
        """
        Determine if an entity should be injected based on policy (no DB I/O).
        """
        if context_type == "chat":
            # Best-effort dedupe in chat history.
            if self.check_if_entity_injected_in_history(entity_name):
                return False
        return True
    
    def _inject_entity_card(self, text: str, match: EntityMatch, card_content: str, context_type: str) -> str:
        """
        Inject entity card content into the text.
        When an alias was used (e.g. "BAS"), prepends it: "BAS (Broadway Art Studio)".
        """
        if match.alias:
            display_name = f"{match.alias} ({match.canonical})"
        else:
            display_name = match.canonical

        if context_type == "chat":
            injection = f"\n\n[Entity Context - {display_name}]:\n{card_content}\n\n"
            return injection + text
        else:
            injection = f"\n[Entity: {display_name}]\n{card_content}\n"
            return text + injection
    
    def inject_into_team_call(self, message: 'Message') -> 'Message':
        """
        Inject entity cards into a team call message
        """
        if not message.content:
            return message
        
        enhanced_content, injected_entities = self.inject_entity_cards_into_text(
            message.content, context_type="team_call"
        )
        
        if injected_entities:
            # Create enhanced message
            enhanced_message = message.copy()
            enhanced_message.content = enhanced_content
            
            # Add entity information to the information field
            if hasattr(enhanced_message, 'information') and enhanced_message.information:
                enhanced_message.information += f"\n\n[Injected Entity Cards: {', '.join(injected_entities)}]"
            else:
                enhanced_message.information = f"[Injected Entity Cards: {', '.join(injected_entities)}]"
            
            logger.debug("Injected %d entity cards into team call: %s", len(injected_entities), injected_entities)
            return enhanced_message
        
        return message


# Global instance
entity_card_injector = EntityCardInjector()
