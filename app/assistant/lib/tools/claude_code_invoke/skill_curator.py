"""Pick which skill files to include with a Claude Code request.

EmiCode's value over a cold-start ``claude`` session is *context curation*:
instead of making the coding agent re-read the architecture every time, we
prepend a small bundle of pre-distilled docs that orient it. This keeps the
first turn focused on the actual question rather than discovery.

v1 strategy (this file):
  - ALWAYS include CLAUDE.md (project root). It's the canonical onboarding
    doc and small enough to always fit.
  - Include docs/architecture/00_OVERVIEW.md by default.
  - Include topic-matched docs/architecture/*.md by simple keyword overlap
    with the user's request. Cap at 3 additional docs to keep the prompt
    bounded.

v2+ ideas (not built):
  - LLM classifier to pick docs (probably not worth the LLM call here).
  - Pull in matching wiki pages from EmiWiki/.
  - Pull in KG facts about entities mentioned in the request.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


# Always-included files (relative to repo root).
ALWAYS_INCLUDE: tuple[str, ...] = (
    "CLAUDE.md",
    "docs/architecture/00_OVERVIEW.md",
)

# Cap on how many topic-matched docs we attach beyond the always-included set.
MAX_TOPIC_DOCS = 3

# Cap on per-file size we read into the bundle. Bigger files get truncated
# with a marker; the agent can Read() the file in full if it cares.
MAX_FILE_CHARS = 12_000


def assemble_skill_bundle(*, request: str, info: str = "") -> str:
    """Return a Markdown-formatted skill bundle for the given request.

    The bundle prepends to the user's coding-agent prompt. Format:

        # Skills

        ## CLAUDE.md
        <file content>

        ## docs/architecture/00_OVERVIEW.md
        <file content>

        ## docs/architecture/05_DAYFLOW.md   (matched: dayflow)
        <file content>
        ...
    """
    repo = get_repo_root()
    keywords = _extract_keywords(f"{request}\n{info}")

    sections: List[str] = []
    seen: set[Path] = set()

    for rel in ALWAYS_INCLUDE:
        p = (repo / rel).resolve()
        if p in seen:
            continue
        seen.add(p)
        body = _read_capped(p)
        if body:
            sections.append(f"## {rel}\n\n{body}")

    matched = _topic_matched_docs(repo, keywords, MAX_TOPIC_DOCS, exclude=seen)
    for path, hits in matched:
        rel = path.relative_to(repo).as_posix()
        body = _read_capped(path)
        if body:
            hit_str = ", ".join(hits[:5])
            sections.append(f"## {rel}   (matched: {hit_str})\n\n{body}")

    if not sections:
        return ""
    return "# Skills\n\n" + "\n\n".join(sections) + "\n\n---\n"


def _read_capped(path: Path) -> str:
    """Read a file, capped at MAX_FILE_CHARS with a truncation marker."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + (
            f"\n\n[... truncated; original was {len(text):,} chars; "
            f"agent can Read() the full file if needed]"
        )
    return text


def _extract_keywords(text: str) -> set[str]:
    """Lowercased word tokens from text, deduped, length filtered.

    Crude but effective for matching against doc filenames + headings:
    drop stopwords and very-short tokens, keep alphanumerics.
    """
    import re

    stop = {
        "the", "and", "for", "with", "this", "that", "you", "your", "from",
        "have", "has", "are", "is", "to", "of", "a", "an", "in", "on", "at",
        "by", "be", "or", "it", "as", "but", "not", "so", "do", "if", "we",
        "i", "me", "my", "can", "will", "would", "should", "could", "may",
        "what", "when", "where", "why", "how", "which", "who", "whom",
        "make", "made", "use", "used", "using", "want", "wants", "need", "needs",
        "add", "build", "create", "fix", "change", "update", "remove", "delete",
        "explain", "show", "tell", "help", "please", "thanks", "thank",
    }
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
    return {t.lower() for t in tokens if t.lower() not in stop}


def _topic_matched_docs(
    repo: Path, keywords: set[str], cap: int, exclude: set[Path],
) -> list[tuple[Path, list[str]]]:
    """Score each docs/architecture/*.md by keyword overlap with title + filename.

    Returns top-``cap`` paths as ``(path, matched_keywords)`` tuples.
    Reading is cheap because we only inspect the filename and the first
    ~80 lines (where headings live).
    """
    if not keywords:
        return []
    arch_dir = repo / "docs" / "architecture"
    if not arch_dir.is_dir():
        return []
    scored: list[tuple[Path, list[str], int]] = []
    for path in sorted(arch_dir.glob("*.md")):
        if path in exclude:
            continue
        try:
            head = path.read_text(encoding="utf-8")[:6000].lower()
        except OSError:
            continue
        name = path.stem.lower()
        hits = [kw for kw in keywords if kw in name or kw in head]
        if hits:
            scored.append((path, hits, len(hits)))
    scored.sort(key=lambda t: (-t[2], t[0].name))
    return [(p, h) for p, h, _ in scored[:cap]]
