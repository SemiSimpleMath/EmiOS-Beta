"""SkillRegistry — load + index skills/<name>/SKILL.md at startup.

DI-registered service. Mirrors ResourceManager in shape (load on boot,
in-memory index, thread-safe reads) but simpler: skills are stable, no
auto-reload, no synthetic updates, no global blackboard mirroring.

See ``docs/architecture/21_SKILLS.md`` for the design.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.skill_registry.models import Skill, SkillHeader, ValidationResult
from app.skill_registry.parser import parse_skill_md

logger = get_logger(__name__)


_SKILL_FILE_NAME = "SKILL.md"


class SkillRegistry:
    """Load and serve skills from ``skills/<name>/SKILL.md``.

    Discovery and loading happens once at construction. A malformed skill
    is logged and excluded; it does not block startup. Re-scan is not
    automatic — skills are stable artifacts; an edit requires a restart
    (intentional, mirrors how Claude Code itself loads skills).
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        # Skills live at ``<repo_root>/skills/`` by default.
        repo_root = base_dir or get_repo_root()
        self._skills_dir: Path = (repo_root / "skills").resolve()
        self._lock = threading.RLock()
        self._skills: Dict[str, Skill] = {}
        self._validations: Dict[str, ValidationResult] = {}
        self._load_all()

    # ── public API ──────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-scan skills/ + skills/private/ from disk, replacing the cache.

        Normally skills are stable (loaded once at construction). The /skills
        editor UI calls this after a user edits a skill body so the change takes
        effect without a process restart. The new index is built COMPLETELY
        off to the side and swapped in under the lock in one assignment — a
        prompt built concurrently with a reload sees either the old set or the
        new set, never an empty or partial registry (the old clear-then-load
        had exactly that window). A crash mid-scan leaves the old index
        serving.
        """
        self._load_all()

    def get(self, name: str) -> Optional[Skill]:
        """Return the full skill record, or None if not registered."""
        with self._lock:
            return self._skills.get(name)

    def headers(self) -> List[SkillHeader]:
        """Return metadata-only headers for all registered skills.

        Tier-1 of progressive disclosure — used for discovery without
        paying the body-loading cost.
        """
        with self._lock:
            return [_to_header(s) for s in self._skills.values()]

    def discover(self, query: str, limit: int = 5) -> List[SkillHeader]:
        """Return up to ``limit`` skills whose description best matches ``query``.

        v1 scoring: keyword overlap with stopwords filtered out, name-field
        matches weighted higher than description-only matches. Replace with
        embedding-based ranking if simple keyword scoring picks wrong at
        scale.
        """
        if not query:
            return []
        q_tokens = {t for t in _tokens(query) if len(t) >= 3 and t not in _STOPWORDS}
        if not q_tokens:
            return []
        scored: list[tuple[int, str, SkillHeader]] = []
        with self._lock:
            for s in self._skills.values():
                name_lc = s.name.lower()
                desc_lc = s.description.lower()
                # Name matches are stronger signal than prose hits — a skill
                # whose name contains "slack" is way more likely to be the
                # right pick for a slack-related query than one that just
                # mentions "slack" in passing.
                name_hits = sum(1 for t in q_tokens if t in name_lc)
                desc_hits = sum(1 for t in q_tokens if t in desc_lc)
                score = name_hits * 3 + desc_hits
                if score > 0:
                    scored.append((-score, s.name, _to_header(s)))
        scored.sort()
        return [h for _, _, h in scored[:limit]]

    def validate_path(self, skill_md_path: Path) -> ValidationResult:
        """Run the parser/validator against a single SKILL.md file.

        Tooling surface — the registry already validates everything at
        load time and stashes results, but external callers (CLIs, the
        in-progress skill author) may want to validate before saving.
        """
        _, result = parse_skill_md(skill_md_path)
        return result

    # ── load ────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        """Build the full index off to the side, then swap it in under the
        lock in ONE assignment. Readers concurrent with a (re)load see either
        the previous index or the complete new one — never a partial. A
        missing skills/ dir keeps whatever is currently served."""
        if not self._skills_dir.is_dir():
            logger.info("[skill_registry] no skills/ directory at %s — registry empty", self._skills_dir)
            return

        skills: Dict[str, Skill] = {}
        validations: Dict[str, ValidationResult] = {}

        # Two roots: skills/ (generic, public, tracked) and skills/private/
        # (per-user persona/actas skills — gitignored, never committed). Both
        # are scanned identically; `private` is just where a user's own,
        # non-shareable skills live so they can't leak into the public repo.
        public_loaded, public_skipped = self._load_skills_from(
            self._skills_dir, skills=skills, validations=validations, skip_names={"private"},
        )
        private_dir = (self._skills_dir / "private").resolve()
        private_loaded, private_skipped = (
            self._load_skills_from(private_dir, skills=skills, validations=validations)
            if private_dir.is_dir() else (0, 0)
        )
        loaded = public_loaded + private_loaded
        skipped = public_skipped + private_skipped

        with self._lock:
            self._skills = skills
            self._validations = validations

        logger.info(
            "[skill_registry] loaded %d skill(s) (%d public, %d private), skipped %d",
            loaded, public_loaded, private_loaded, skipped,
        )

    def _load_skills_from(
        self,
        root: Path,
        *,
        skills: Dict[str, Skill],
        validations: Dict[str, ValidationResult],
        skip_names: Optional[set] = None,
    ) -> tuple[int, int]:
        """Scan one directory of ``<name>/SKILL.md`` skill dirs into the
        caller's staging dicts. Returns (loaded, skipped)."""
        skip = skip_names or set()
        loaded = 0
        skipped = 0
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name.startswith("_"):
                # Convention: hidden / template directories are skipped.
                continue
            if child.name in skip:
                # e.g. the `private` subdir is scanned as its own root, not as a skill.
                continue
            skill_md = child / _SKILL_FILE_NAME
            if not skill_md.is_file():
                logger.warning("[skill_registry] %s is missing %s; skipping", child, _SKILL_FILE_NAME)
                skipped += 1
                continue
            skill, result = parse_skill_md(skill_md)
            validations[child.name] = result
            if skill is None:
                logger.warning(
                    "[skill_registry] skill %s failed validation: %s",
                    child.name, "; ".join(result.errors),
                )
                skipped += 1
                continue
            skills[skill.name] = skill
            loaded += 1
            if result.warnings:
                logger.info(
                    "[skill_registry] loaded %s with warnings: %s",
                    skill.name, "; ".join(result.warnings),
                )
        return loaded, skipped


# ── helpers ─────────────────────────────────────────────────────────

import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

# Common English stopwords that pollute keyword scoring. Question words
# (how/what/where) hit on every "How to ..." description; pronouns and
# prepositions hit everywhere. Drop them before counting.
_STOPWORDS = frozenset({
    # question words
    "how", "what", "where", "when", "why", "who", "which", "whom",
    # articles / determiners
    "the", "this", "that", "these", "those",
    # auxiliaries
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "doing", "done",
    "has", "have", "had", "having",
    "can", "could", "will", "would", "should", "may", "might", "must",
    # pronouns / possessives
    "i", "you", "we", "they", "he", "she", "it",
    "my", "your", "our", "their", "his", "her", "its",
    "me", "us", "them", "him",
    # prepositions / conjunctions
    "to", "of", "in", "on", "at", "for", "with", "from", "by", "about",
    "as", "into", "through", "over", "under", "out", "off",
    "and", "or", "but", "not", "so", "if", "than", "then", "because",
    # vague verbs/nouns the query commonly contains but the description rarely does usefully
    "send", "get", "make", "find", "help", "want", "need", "use",
    "something", "anything", "please", "thanks", "thank",
})


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _to_header(skill: Skill) -> SkillHeader:
    return SkillHeader(
        name=skill.name,
        description=skill.description,
        license=skill.license,
        compatibility=skill.compatibility,
        metadata=skill.metadata,
        allowed_tools=skill.allowed_tools,
        auto_inject_when=skill.auto_inject_when,
        skill_dir=skill.skill_dir,
    )
