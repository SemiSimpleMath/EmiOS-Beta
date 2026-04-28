"""Vault writer — renders entity pages to an Obsidian-compatible folder and
maintains ``log.md`` (chronological) and ``index.md`` (catalog).

The vault is just a folder of markdown files. Obsidian opens it directly.
Every regeneration:
  - Overwrites the entity's page.
  - Appends one line to ``log.md``.
  - Rebuilds ``index.md`` from the current set of pages in the vault.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from app.assistant.kg_projection import EntityNeighborhood, get_entity_neighborhood
from app.assistant.wiki_generator.wiki_renderer import render_page
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 ._'\u2019\-]")


def _safe_filename(label: str) -> str:
    """Convert an entity label to a filename-safe stem. Keep apostrophes and
    spaces so Obsidian's wiki-link target ([[Katy]]) resolves to ``Katy.md``."""
    stem = _SAFE_FILENAME_RE.sub("_", label.strip())
    return stem.strip(". ")


def regenerate_entity_page(
    *,
    label: str,
    vault_path: Path,
    neighborhood: Optional[EntityNeighborhood] = None,
) -> Path:
    """Render and write the page for ``label`` to the vault. Returns the path."""
    vault_path.mkdir(parents=True, exist_ok=True)
    if neighborhood is None:
        neighborhood = get_entity_neighborhood(label)
    markdown = render_page(neighborhood)
    filename = _safe_filename(label) + ".md"
    target = vault_path / filename
    target.write_text(markdown, encoding="utf-8")
    _append_log(
        vault_path=vault_path,
        kind="regen",
        label=label,
        gap_count=len(neighborhood.kg_gaps),
        relationship_count=len(neighborhood.relationships),
        event_count=len(neighborhood.events),
    )
    return target


def regenerate_many(*, labels: Iterable[str], vault_path: Path) -> List[Path]:
    written: List[Path] = []
    for label in labels:
        try:
            p = regenerate_entity_page(label=label, vault_path=vault_path)
            written.append(p)
            logger.info("Wiki: wrote %s", p.name)
        except LookupError as e:
            logger.warning("Wiki: skipping %s — %s", label, e)
            _append_log(vault_path=vault_path, kind="skip", label=label, reason=str(e))
    rebuild_index(vault_path=vault_path)
    return written


def rebuild_index(*, vault_path: Path) -> Path:
    """Rebuild ``index.md`` by listing every entity page in the vault with a one-line pointer."""
    vault_path.mkdir(parents=True, exist_ok=True)
    entries: List[tuple[str, str, Optional[str]]] = []
    for md in sorted(vault_path.glob("*.md")):
        if md.name.lower() in {"index.md", "log.md"}:
            continue
        stem = md.stem
        try:
            frontmatter = _read_frontmatter(md)
        except Exception as e:
            logger.debug("Could not read frontmatter from %s: %s", md, e)
            frontmatter = {}
        category = frontmatter.get("category")
        entries.append((stem, category or "uncategorized", frontmatter.get("relationship_count")))

    lines: List[str] = [
        "# Wiki Index",
        "",
        f"_auto-generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    if not entries:
        lines.append("_(empty — no pages yet)_")
    else:
        # Group by category
        by_cat: dict[str, list[tuple[str, Optional[str]]]] = {}
        for stem, cat, rel_count in entries:
            by_cat.setdefault(cat, []).append((stem, rel_count))
        for cat in sorted(by_cat.keys()):
            lines.append(f"## {cat}")
            lines.append("")
            for stem, rel_count in sorted(by_cat[cat]):
                suffix = f" — {rel_count} relationships" if rel_count is not None else ""
                lines.append(f"- [[{stem}]]{suffix}")
            lines.append("")

    index_path = vault_path / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


def _append_log(
    *,
    vault_path: Path,
    kind: str,
    label: str,
    **extras,
) -> None:
    vault_path.mkdir(parents=True, exist_ok=True)
    log_path = vault_path / "log.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    extra_bits = " ".join(f"{k}={v}" for k, v in extras.items())
    line = f"## [{ts}] {kind} | {label} | {extra_bits}\n"
    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else "# Wiki Log\n\n"
    log_path.write_text(existing + line, encoding="utf-8")


def _read_frontmatter(md_path: Path) -> dict:
    """Minimal YAML-ish frontmatter reader. Only handles ``key: value`` pairs
    between leading ``---`` markers. No dependency on PyYAML."""
    out: dict = {}
    if not md_path.exists():
        return out
    content = md_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return out
    lines = content.splitlines()
    if len(lines) < 3:
        return out
    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # strip trailing quotes / list syntax
        if value.isdigit():
            out[key] = int(value)
        elif value.lower() in {"true", "false"}:
            out[key] = value.lower() == "true"
        else:
            out[key] = value.strip("'\"")
    return out
