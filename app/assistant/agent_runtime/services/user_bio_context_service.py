from __future__ import annotations

import math
import re
import json
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from app.assistant.embeddings.embedder import embed_text as _shared_embed_text, embed_texts as _shared_embed_texts
from app.assistant.agent_runtime.services.resource_resolver import ResourceResolver
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class _BioFact:
    text: str
    section: str
    tags: tuple[str, ...]
    subject: str


@dataclass(frozen=True)
class _BioChunk:
    text: str
    section: str
    buckets: tuple[str, ...]


class UserBioContextService:
    """
    Intent-routed, selective user-bio context retrieval.
    """

    _WORD_RE = re.compile(r"[a-z0-9_]+")
    _SECTION_ALLOWLIST = {
        "admin": ("style_persona", "preferences"),
        "technical": ("style_persona", "background", "projects"),
        "research": ("style_persona", "background", "projects"),
        "personal": ("style_persona", "preferences", "background", "health_logistics", "entertainment_hobbies"),
        "worldview": ("style_persona", "values"),
    }
    _MAX_OUTPUT_CHARS = 900
    _BUCKET_MIN_SIMILARITY = 0.315
    _BUCKET_MIN_MARGIN = 0.04
    _BUCKET_HIGH_CONFIDENCE = 0.30
    _BUCKET_ROUTE_TOP_K = 2
    _SECTION_MIN_SIMILARITY = 0.25
    _USER_BIO_RESOURCE_ID = "user_bio"
    _BUCKET_TO_SECTIONS = {
        "admin": ("preferences", "style_persona"),
        "technical": ("background", "projects", "style_persona"),
        "research": ("background", "projects", "style_persona"),
        "personal": ("background", "preferences", "health_logistics", "entertainment_hobbies", "style_persona"),
        "worldview": ("values", "style_persona"),
    }
    _SECTION_NAME_ALIASES = {
        "health & logistics": "health_logistics",
        "entertainment & hobbies": "entertainment_hobbies",
    }
    _chunk_cache_key = None
    _chunk_cache_chunks: list[_BioChunk] = []
    _chunk_cache_vectors: list[list[float]] = []

    @classmethod
    def build_context(cls, *, agent: Any, incoming_text: str) -> str:
        agent_name = str(getattr(agent, "name", "agent") or "agent")
        query = str(incoming_text or "").strip()
        if not query:
            logger.debug("[%s] user_bio_context skipped: empty incoming_text.", agent_name)
            return ""
        tokens = cls._tokenize(query)
        if not tokens:
            logger.debug("[%s] user_bio_context skipped: tokenization produced no tokens.", agent_name)
            return ""

        payload = cls._resolve_user_bio_payload(agent=agent)
        if not payload:
            logger.debug("[%s] user_bio_context: _resolve_user_bio_payload returned empty/None.", agent_name)
            return ""
        logger.debug("[%s] user_bio_context: payload sections=%s, query=%r", agent_name, list(payload.keys()), query[:100])
        bucket = cls._route_bucket(query=query, payload=payload)
        logger.debug("[%s] user_bio_context: _route_bucket returned %r", agent_name, bucket)
        if not bucket:
            logger.debug("[%s] user_bio_context skipped: no bucket passed semantic routing threshold.", agent_name)
            return ""
        sections = cls._SECTION_ALLOWLIST.get(bucket, ())
        if not sections:
            raise ValueError(f"Unsupported bio routing bucket: {bucket}")
        facts = cls._collect_facts(payload=payload, bucket=bucket)
        if not facts:
            logger.debug("[%s] user_bio_context skipped: no facts available for bucket '%s'.", agent_name, bucket)
            return ""

        matched_sections = cls._route_matching_sections(
            query_text=query, facts=facts, allowed_sections=set(sections),
        )

        if matched_sections:
            selected = [f for f in facts if f.section in matched_sections]
            logger.debug(
                "[%s] user_bio_context: matched sections=%s (%d facts).",
                agent_name, matched_sections, len(selected),
            )
        else:
            selected = []

        if not selected:
            logger.debug(
                "[%s] user_bio_context skipped: no facts passed thresholds (bucket=%s).",
                agent_name, bucket,
            )
            return ""

        section_order = [s for s in sections if s in matched_sections]
        grouped: dict[str, list[str]] = {}
        for f in selected:
            grouped.setdefault(f.section, []).append(f"- {f.text}")
        parts: list[str] = ["Relevant user bio context:"]
        for sec in section_order:
            if sec not in grouped:
                continue
            parts.append(cls._section_display_name(sec) + ":")
            parts.extend(grouped[sec])
        out = "\n".join(parts)
        final = out[: cls._MAX_OUTPUT_CHARS].strip()
        logger.debug(
            "[%s] user_bio_context injected: bucket=%s facts_selected=%d chars=%d.",
            agent_name, bucket, len(selected), len(final),
        )
        return final

    @classmethod
    def _tokenize(cls, text: str) -> set[str]:
        return {m.group(0) for m in cls._WORD_RE.finditer(str(text or "").lower())}

    @classmethod
    def _route_bucket(cls, *, query: str, payload: dict) -> str:
        sim_by_bucket = cls._semantic_route_bucket(query=query, payload=payload)
        if not sim_by_bucket:
            return ""
        ranked = sorted(sim_by_bucket.items(), key=lambda x: x[1], reverse=True)
        best_bucket, best_score = ranked[0]
        if float(best_score) < cls._BUCKET_MIN_SIMILARITY:
            return ""
        if len(ranked) > 1:
            second_score = float(ranked[1][1])
            margin = float(best_score) - second_score
            if margin < cls._BUCKET_MIN_MARGIN and float(best_score) < cls._BUCKET_HIGH_CONFIDENCE:
                return ""
        return str(best_bucket)

    @classmethod
    def _route_matching_sections(
        cls,
        *,
        query_text: str,
        facts: List[_BioFact],
        allowed_sections: set[str],
    ) -> set[str]:
        """Score each section by average similarity of its top facts to the query.

        Returns the set of section names that pass the similarity threshold.
        """
        section_facts: Dict[str, List[_BioFact]] = {}
        for f in facts:
            if f.section in allowed_sections:
                section_facts.setdefault(f.section, []).append(f)
        if not section_facts:
            return set()

        query_vec = cls._embed_text(query_text)
        section_scores: Dict[str, float] = {}
        for section, sf in section_facts.items():
            vecs = cls._embed_texts([f.text for f in sf])
            sims = [cls._cosine_similarity(query_vec, v) for v in vecs]
            sims.sort(reverse=True)
            top = sims[:3]
            section_scores[section] = sum(top) / len(top)

        matched: set[str] = set()
        for section, score in section_scores.items():
            if score >= cls._SECTION_MIN_SIMILARITY:
                matched.add(section)
        return matched

    @classmethod
    def _semantic_route_bucket(cls, *, query: str, payload: dict) -> Dict[str, float]:
        query_vec = cls._embed_text(str(query or "").strip())
        chunks, chunk_vecs = cls._build_chunk_index(payload=payload)
        bucket_sims: Dict[str, list[float]] = {bucket: [] for bucket in cls._BUCKET_TO_SECTIONS.keys()}
        for chunk, chunk_vec in zip(chunks, chunk_vecs):
            sim = cls._cosine_similarity(query_vec, chunk_vec)
            for bucket in chunk.buckets:
                bucket_sims.setdefault(bucket, []).append(sim)
        out: Dict[str, float] = {bucket: cls._score_bucket_similarities(sims) for bucket, sims in bucket_sims.items()}
        return out

    @classmethod
    def _score_bucket_similarities(cls, sims: List[float]) -> float:
        if not sims:
            return 0.0
        ordered = sorted((float(x) for x in sims), reverse=True)
        top_k = ordered[: cls._BUCKET_ROUTE_TOP_K]
        max_sim = top_k[0]
        mean_top = sum(top_k) / float(len(top_k))
        return (0.80 * max_sim) + (0.20 * mean_top)

    @classmethod
    def _payload_cache_key(cls, *, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _build_chunk_index(cls, *, payload: dict) -> Tuple[list[_BioChunk], list[list[float]]]:
        cache_key = cls._payload_cache_key(payload=payload)
        if cls._chunk_cache_key == cache_key:
            return list(cls._chunk_cache_chunks), list(cls._chunk_cache_vectors)

        section_to_buckets: Dict[str, list[str]] = {}
        for bucket, sections in cls._BUCKET_TO_SECTIONS.items():
            for section in sections:
                normalized = cls._normalize_section_name(section)
                section_to_buckets.setdefault(normalized, [])
                if bucket not in section_to_buckets[normalized]:
                    section_to_buckets[normalized].append(bucket)

        chunks: list[_BioChunk] = []
        seen_texts: set[str] = set()
        for section, buckets in section_to_buckets.items():
            texts = cls._extract_section_texts(payload=payload, section_name=section)
            for text in texts:
                norm = " ".join(str(text or "").strip().lower().split())
                if not norm or norm in seen_texts:
                    continue
                seen_texts.add(norm)
                chunks.append(_BioChunk(text=text, section=section, buckets=tuple(buckets)))

        if not chunks:
            cls._chunk_cache_key = cache_key
            cls._chunk_cache_chunks = []
            cls._chunk_cache_vectors = []
            return [], []

        vectors = cls._embed_texts([c.text for c in chunks])
        cls._chunk_cache_key = cache_key
        cls._chunk_cache_chunks = list(chunks)
        cls._chunk_cache_vectors = list(vectors)
        return list(chunks), list(vectors)

    @classmethod
    def _extract_section_texts(cls, *, payload: dict, section_name: str) -> List[str]:
        normalized = cls._normalize_section_name(section_name)
        items = payload.get(normalized)
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for item in items:
            if not isinstance(item, str):
                continue
            text = str(item).strip()
            if text:
                out.append(text)
        return out

    @classmethod
    def _embed_text(cls, text: str) -> List[float]:
        return _shared_embed_text(str(text or "").strip())

    @classmethod
    def _embed_texts(cls, texts: List[str]) -> List[List[float]]:
        return _shared_embed_texts([str(t or "").strip() for t in texts])

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if len(a) != len(b):
            raise ValueError("Cosine vectors must have same dimension")
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            fx = float(x)
            fy = float(y)
            dot += fx * fy
            na += fx * fx
            nb += fy * fy
        if na <= 0.0 or nb <= 0.0:
            raise ValueError("Cosine vectors must have non-zero norm")
        return float(dot / (math.sqrt(na) * math.sqrt(nb)))

    @classmethod
    def _resolve_user_bio_payload(cls, *, agent: Any) -> dict:
        bb = getattr(agent, "blackboard", None)
        if bb is None:
            raise ValueError("Agent blackboard is not available for user_bio routing")
        scope_context = bb.get_state_value("scope_context", None)
        if scope_context is None:
            raise ValueError("scope_context is required for user_bio routing")
        try:
            payload = ResourceResolver.get_global_resource(
                cls._USER_BIO_RESOURCE_ID,
                required=False,
                scope_context=scope_context,
            )
        except PermissionError as e:
            logger.info(
                "[%s] user_bio scope denied for resource '%s'; skipping user_bio_context injection: %s",
                getattr(agent, "name", "agent"),
                cls._USER_BIO_RESOURCE_ID,
                e,
            )
            logger.debug(
                "[%s] user_bio scope denial details",
                getattr(agent, "name", "agent"),
            )
            return {}
        except Exception as e:
            logger.error("[%s] Failed reading canonical user_bio resource: %s", getattr(agent, "name", "agent"), e)
            logger.debug("[%s] canonical user_bio read exception details", getattr(agent, "name", "agent"), exc_info=True)
            raise

        if payload is None:
            logger.info("[%s] user_bio resource missing; skipping bio injection.", getattr(agent, "name", "agent"))
            return {}
        if not isinstance(payload, dict):
            raise ValueError("user_bio must be a JSON object")
        return payload

    @classmethod
    def _collect_facts(cls, *, payload: dict, bucket: str) -> List[_BioFact]:
        facts: List[_BioFact] = []
        sections = cls._BUCKET_TO_SECTIONS.get(bucket, ())
        if not sections:
            return []
        for section_name in sections:
            facts.extend(cls._facts_from_user_bio_section(payload=payload, section_name=section_name))
        return facts

    @classmethod
    def _facts_from_user_bio_section(cls, *, payload: dict, section_name: str) -> List[_BioFact]:
        normalized = cls._normalize_section_name(section_name)
        items = payload.get(normalized)
        if not isinstance(items, list):
            return []
        out: List[_BioFact] = []
        for item in items:
            if not isinstance(item, str):
                continue
            text = str(item or "").strip()
            if not text:
                continue
            out.append(
                _BioFact(
                    text=text,
                    section=normalized,
                    tags=(normalized,),
                    subject=f"user_bio.{normalized}",
                )
            )
        return out

    _SECTION_DISPLAY_NAMES = {
        "style_persona": "Style & Persona",
        "background": "Background",
        "projects": "Projects",
        "values": "Values",
        "health_logistics": "Health & Logistics",
        "entertainment_hobbies": "Entertainment & Hobbies",
        "preferences": "Preferences",
    }

    @classmethod
    def _section_display_name(cls, section_key: str) -> str:
        return cls._SECTION_DISPLAY_NAMES.get(section_key, section_key.replace("_", " ").title())

    @staticmethod
    def _normalize_section_name(section_name: str) -> str:
        raw = str(section_name or "").strip().lower()
        if not raw:
            return ""
        return UserBioContextService._SECTION_NAME_ALIASES.get(raw, raw.replace(" ", "_"))

