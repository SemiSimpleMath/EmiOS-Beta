"""
Keyword Resource Index

Scans for ``*.triggers.json`` sidecar files alongside resource files and
builds an in-memory keyword → resource_id map. Any agent that sets
``enable_keyword_resource_injection: true`` in its config gets automatic
resource injection when the user's message matches trigger keywords.

Trigger file format (``resource_foo.triggers.json``)::

    {
        "keywords": ["foo", "bar baz", "how is my foo"],
        "label": "Foo Context",
        "match_mode": "substring",
        "applicable_agents": null
    }

``keywords``          — list of case-insensitive strings to match.
``label``             — optional human-readable heading for prompt rendering.
``match_mode``        — ``"substring"`` (default) or ``"whole_word"``.
                        Whole-word matching compiles a single ``\\b...\\b`` regex.
``applicable_agents`` — optional list of agent names (e.g.
                        ``["knowledge_graph_add::fact_extractor"]``). When set,
                        the resource only matches for those agents. Omit or
                        set to ``null`` for any opted-in agent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir

logger = get_logger(__name__)


_MATCH_MODES = {"substring", "whole_word"}


class _TriggerEntry:
    __slots__ = ("resource_id", "keywords", "label", "match_mode", "applicable_agents", "_regex")

    def __init__(
        self,
        resource_id: str,
        keywords: List[str],
        label: str,
        match_mode: str = "substring",
        applicable_agents: Optional[List[str]] = None,
    ) -> None:
        self.resource_id = resource_id
        self.keywords = keywords
        self.label = label
        self.match_mode = match_mode
        self.applicable_agents = applicable_agents
        self._regex: Optional[re.Pattern[str]] = None
        if match_mode == "whole_word":
            pattern = r"\b(?:" + "|".join(re.escape(kw) for kw in keywords) + r")\b"
            self._regex = re.compile(pattern, re.IGNORECASE)

    def matches(self, text_lower: str) -> bool:
        if self._regex is not None:
            return bool(self._regex.search(text_lower))
        return any(kw in text_lower for kw in self.keywords)


class KeywordResourceIndex:
    """Singleton index built once at startup from ``*.triggers.json`` files."""

    _instance: KeywordResourceIndex | None = None
    _entries: List[_TriggerEntry]

    def __init__(self) -> None:
        self._entries = []

    @classmethod
    def get_instance(cls) -> KeywordResourceIndex:
        if cls._instance is None:
            cls._instance = cls()
            cls._instance.build()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def build(self) -> None:
        self._entries.clear()
        resources_root = get_resources_dir()
        trigger_files = sorted(resources_root.rglob("resource_*.triggers.json"))
        for tf in trigger_files:
            try:
                entry = self._parse_trigger_file(tf)
                if entry is not None:
                    self._entries.append(entry)
            except Exception:
                logger.error(
                    "Failed to parse trigger file '%s'", tf, exc_info=True,
                )
                raise
        logger.info(
            "KeywordResourceIndex built: %d trigger file(s), %d total keywords.",
            len(self._entries),
            sum(len(e.keywords) for e in self._entries),
        )

    @staticmethod
    def _parse_trigger_file(path: Path) -> _TriggerEntry | None:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.error("Trigger file '%s' is not a JSON object — skipping.", path)
            return None
        keywords_raw = data.get("keywords")
        if not isinstance(keywords_raw, list) or not keywords_raw:
            logger.error("Trigger file '%s' has no keywords list — skipping.", path)
            return None
        keywords = [str(k).strip().lower() for k in keywords_raw if isinstance(k, str) and str(k).strip()]
        if not keywords:
            return None

        match_mode = str(data.get("match_mode") or "substring").strip().lower()
        if match_mode not in _MATCH_MODES:
            raise ValueError(
                f"Trigger file '{path}' has invalid match_mode '{match_mode}' "
                f"(allowed: {sorted(_MATCH_MODES)})."
            )

        applicable_raw = data.get("applicable_agents")
        applicable_agents: Optional[List[str]] = None
        if applicable_raw is not None:
            if not isinstance(applicable_raw, list):
                raise ValueError(
                    f"Trigger file '{path}' has non-list applicable_agents."
                )
            applicable_agents = [
                str(a).strip() for a in applicable_raw if isinstance(a, str) and str(a).strip()
            ]
            if not applicable_agents:
                applicable_agents = None

        stem = path.stem
        resource_id = stem.replace(".triggers", "")
        label = str(data.get("label") or "").strip() or resource_id.replace("resource_", "").replace("_", " ").title()
        return _TriggerEntry(
            resource_id=resource_id,
            keywords=keywords,
            label=label,
            match_mode=match_mode,
            applicable_agents=applicable_agents,
        )

    def match(self, text: str, *, agent_name: Optional[str] = None) -> List[Tuple[str, str]]:
        """Return list of ``(resource_id, label)`` whose keywords match *text*.

        When a trigger file declares ``applicable_agents``, the resource only
        matches when *agent_name* is in that list. Each resource appears at most once.
        """
        if not text or not self._entries:
            return []
        text_lower = text.lower()
        results: List[Tuple[str, str]] = []
        for entry in self._entries:
            if entry.applicable_agents is not None:
                if agent_name is None or agent_name not in entry.applicable_agents:
                    continue
            if entry.matches(text_lower):
                results.append((entry.resource_id, entry.label))
        return results
