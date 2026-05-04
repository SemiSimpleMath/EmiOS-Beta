"""
Backfill kg_node_metadata.description from existing wiki prose page leads.

Problem
-------
Per the user-stated design ("the first section is supposed to become the
node description and then that is supposed to be used as the about"),
the wiki page's lead paragraph IS the description, which powers the
visualizer's ABOUT panel. page_writer._sync_lead_to_node_description writes
this on every wiki regen. But description_creator (kg_maintenance_pipeline)
also writes descriptions and was overwriting wiki-set values with its own
LLM-generated text from raw KG neighborhood — producing a divergence
between what the wiki shows and what ABOUT shows.

The forward fix (committed alongside this script) gates description_creator
to skip entities with wiki pages. But existing nodes with a wiki page +
divergent description need to be re-synced. This script does that one-time
sync: for every prose page in the vault, parse the lead paragraph and
overwrite the corresponding kg_node_metadata.description.

Lead extraction
---------------
A "lead" is the prose between the H1 title (and optional profile image)
and the first H2 (`## Section`). This matches what page_writer stitches in.

Usage
-----
Dry-run (default) prints planned writes:

    .venv/Scripts/python.exe scripts/backfill_descriptions_from_wiki_leads.py

Commit:

    .venv/Scripts/python.exe scripts/backfill_descriptions_from_wiki_leads.py --commit
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Strip wiki-link brackets, image tags, frontmatter, etc. for a clean
# description. Keep semantic text.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_NODE_REF_RE = re.compile(r"\{node:[^}]+\}")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def _extract_lead(markdown_body: str) -> str:
    """Pull text between the H1 title (with optional profile image) and the
    first H2 heading. Returns "" if no clear lead is present.
    """
    # Find the H1 line.
    m = re.search(r"^#\s+[^\n]+\n", markdown_body, flags=re.MULTILINE)
    if not m:
        return ""
    after_h1 = markdown_body[m.end():]
    # Cut at first H2.
    h2 = re.search(r"^##\s+", after_h1, flags=re.MULTILINE)
    body = after_h1[:h2.start()] if h2 else after_h1
    # Strip image, comments, node refs.
    body = _IMAGE_RE.sub("", body)
    body = _HTML_COMMENT_RE.sub("", body)
    body = _NODE_REF_RE.sub("", body)
    # Convert wiki links + bold to plain text (visualizer doesn't render them).
    body = _WIKILINK_RE.sub(r"\1", body)
    body = _BOLD_RE.sub(r"\1", body)
    # Collapse blank lines.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return "\n\n".join(paragraphs).strip()


def _strip_frontmatter(content: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) from markdown with optional YAML
    frontmatter at the top. frontmatter_dict is empty if none present."""
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---\n", 4)
    if end < 0:
        return {}, content
    raw = content[4:end]
    body = content[end + 5:]
    fm = {}
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip("'\"")
    return fm, body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="Persist description writes.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print first 10 sync details.")
    args = parser.parse_args()

    from app.assistant.wiki_generator.nightly_refresh import _default_vault
    from app.assistant.pipelines.kg_maintenance_pipeline.description_creator import (
        persist_description,
    )
    from app.models.db_manager import get_db_manager
    from sqlalchemy import text

    vault = _default_vault()
    prose_dir = vault / "prose"
    if not prose_dir.exists():
        print(f"No prose dir at {prose_dir}; nothing to do.")
        return 0

    print(f"=== Wiki lead → description backfill — {'COMMIT' if args.commit else 'DRY-RUN'} ===")
    print(f"Vault: {vault}\n")

    counters = {
        "scanned": 0,
        "no_kg_node_id": 0,
        "no_lead": 0,
        "already_matches": 0,
        "would_write": 0,
        "wrote": 0,
        "errors": 0,
    }

    md_files = sorted(prose_dir.glob("*.md"))
    print(f"Found {len(md_files)} prose pages.\n")

    for path in md_files:
        counters["scanned"] += 1
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            counters["errors"] += 1
            print(f"  ERROR reading {path.name}: {exc}")
            continue

        fm, body = _strip_frontmatter(content)
        kg_node_id = fm.get("kg_node_id", "").strip()
        if not kg_node_id:
            counters["no_kg_node_id"] += 1
            continue

        lead = _extract_lead(body)
        if not lead:
            counters["no_lead"] += 1
            continue

        with get_db_manager().read_session() as s:
            current = s.execute(
                text("SELECT description FROM kg_node_metadata WHERE id = :nid"),
                {"nid": kg_node_id},
            ).scalar()

        if current == lead:
            counters["already_matches"] += 1
            continue

        counters["would_write"] += 1
        if args.verbose and counters["would_write"] <= 10:
            label = fm.get("name", path.stem)
            print(f"  [{label}] node={kg_node_id[:8]}")
            print(f"    OLD: {(current or '(empty)')[:120]}")
            print(f"    NEW: {lead[:120]}")

        if args.commit:
            try:
                changed = persist_description(kg_node_id, lead)
                if changed:
                    counters["wrote"] += 1
            except Exception as exc:
                counters["errors"] += 1
                print(f"  ERROR persisting {kg_node_id[:8]}: {exc}")

    print(f"\n=== Counts ===")
    for k, v in counters.items():
        print(f"  {k:22s}: {v}")
    print(f"\n=== {'COMMITTED' if args.commit else 'DRY-RUN COMPLETE'} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
