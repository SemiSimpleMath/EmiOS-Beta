"""Step 2 of the wiki pipeline: transform a rough KG-projected page into
natural prose using the wiki_writer agent.

Inputs:
- A rough markdown page produced by wiki_renderer.py (already in the vault).
- A sample of source conversation excerpts pulled via the entity's
  backfilled window_ids.

Outputs:
- A prose markdown page saved to the vault's ``prose/`` subdirectory.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.kg.db.knowledge_graph_db import Node, Edge, get_session
from app.assistant.kg_projection import (
    Bullet,
    SECTIONS_RESOURCE as WIKI_SECTIONS_RESOURCE,
    bullet_key as _bullet_key,
    get_entity_neighborhood,
    group_bullets_by_section as _group_bullets_by_section_raw,
    load_tags as _load_tag_sidecar,
    load_taxonomy,
    render_bullets,
    save_tags as _save_tag_sidecar,
    sections_as_prompt_list,
    tag_bullets,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.wiki_generator.references import apply_references
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)

WRITER_AGENT = "wiki_writer"
TAGGER_AGENT = "wiki_section_tagger"
MAX_WINDOWS_FOR_EXCERPTS = 10
MAX_CHARS_PER_MESSAGE = 200


# HTML comments and debug markers that belong to the rough-page pipeline
# and should never appear in the prose output.
_DEBUG_COMMENT_RE = re.compile(r"<!--\s*KG GAP.*?-->", re.IGNORECASE | re.DOTALL)
_WIKI_MARKER_RE = re.compile(r"<!--\s*/?\s*WIKI:(DET|AUTO)\b[^>]*-->", re.IGNORECASE)
# Whole-section wrappers whose contents are pure meta and should be removed
# entirely (not just their markers).
_META_SECTION_RE = re.compile(
    r"<!--\s*WIKI:DET\s+kg_gaps\s*-->.*?<!--\s*/WIKI:DET\s*-->",
    re.IGNORECASE | re.DOTALL,
)


_LEADING_DUPE_FRONTMATTER_RE = re.compile(r"\A(?:\s*---\s*\n){2,}")


def strip_debug_scaffolding(markdown: str) -> str:
    """Remove KG GAP comments, WIKI:DET/AUTO markers, and the kg_gaps section.

    Also collapses a leading sequence of bare ``---`` lines down to a single
    one. The full-page wiki_writer agent sometimes echoes both the rough
    page's frontmatter delimiter AND the user-prompt's visual ``---`` separator,
    producing ``---\\n---\\nname: ...\\n---`` which causes frontmatter parsers
    to see an empty YAML block and render the real frontmatter as body text.
    """
    out = _META_SECTION_RE.sub("", markdown)
    out = _DEBUG_COMMENT_RE.sub("", out)
    out = _WIKI_MARKER_RE.sub("", out)
    # Collapse any ``---\n---\n...`` prefix down to a single ``---\n`` so the
    # frontmatter block is well-formed.
    out = _LEADING_DUPE_FRONTMATTER_RE.sub("---\n", out)
    # Collapse runs of blank lines that the strips leave behind
    out = re.sub(r"\n{3,}", "\n\n", out).rstrip() + "\n"
    return out


def _scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::wiki::page_writer",
        owner_id="jukka",
        actor_id="wiki_page_writer",
        surface="ui",
        room_id="master_room",
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )


def collect_entity_window_ids(entity_label: str, limit: int = MAX_WINDOWS_FOR_EXCERPTS) -> List[str]:
    """Distinct window_ids associated with any edge OR node touching the entity."""
    session = get_session()
    try:
        entity = (
            session.query(Node)
            .filter(Node.label == entity_label, Node.node_type == "Entity")
            .first()
        )
        if entity is None:
            return []
        rows = session.execute(
            text(
                """
                SELECT DISTINCT window_id FROM (
                    SELECT window_id FROM kg_edge_metadata
                    WHERE window_id IS NOT NULL
                      AND (source_id = :eid OR target_id = :eid)
                    UNION
                    SELECT window_id FROM kg_node_metadata
                    WHERE window_id IS NOT NULL
                      AND id = :eid
                )
                LIMIT :lim
                """
            ),
            {"eid": entity.id, "lim": limit},
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    finally:
        session.close()


def build_window_excerpts(window_ids: List[str], max_per_msg: int = MAX_CHARS_PER_MESSAGE) -> str:
    """Assemble a compact excerpt block from user-role messages across these windows.

    Walks the unified pipeline tables: ``kg_window_message`` for membership,
    ``unified_log_2026`` for verbatim text + speaker, ``kg_resolved_message``
    for the entity-resolved version when present.
    """
    if not window_ids:
        return ""
    session = get_session()
    try:
        lines: List[str] = []
        for wid in window_ids:
            items = session.execute(
                text(
                    "SELECT wm.item_order, ul.speaker_name, "
                    "       COALESCE(rm.resolved_text, ul.message) AS text "
                    "FROM kg_window_message wm "
                    "JOIN unified_log_2026 ul        ON ul.id = wm.unified_log_id "
                    "LEFT JOIN kg_resolved_message rm ON rm.unified_log_id = wm.unified_log_id "
                    "WHERE wm.window_id = :w AND ul.role = 'user' "
                    "ORDER BY wm.item_order"
                ),
                {"w": wid},
            ).fetchall()
            if not items:
                continue
            lines.append(f"[window {wid[:8]}]")
            for _, speaker, txt in items:
                t = (txt or "").strip()
                if not t:
                    continue
                if len(t) > max_per_msg:
                    t = t[:max_per_msg].rstrip() + "..."
                lines.append(f"  {speaker or 'user'}: {t}")
            lines.append("")
        return "\n".join(lines).strip()
    finally:
        session.close()


def read_rough_page(vault_path: Path, entity_label: str) -> Optional[str]:
    """Load the rough markdown page for the entity from the vault."""
    candidate = vault_path / f"{entity_label}.md"
    if not candidate.exists():
        return None
    return candidate.read_text(encoding="utf-8")


def write_prose_page(vault_path: Path, entity_label: str, markdown: str) -> Path:
    out_dir = vault_path / "prose"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{entity_label}.md"
    target.write_text(markdown, encoding="utf-8")
    return target


# ----------------------------------------------------------------------
# Rough-page parsing helpers (shared by tag-based generation and the
# incremental refresh path).
# ----------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_SECTION_RE = re.compile(
    r"<!--\s*WIKI:DET\s+(\S+?)\s*-->(.*?)<!--\s*/WIKI:DET\s*-->",
    re.DOTALL | re.IGNORECASE,
)


def _extract_frontmatter(rough: str) -> str:
    m = _FRONTMATTER_RE.match(rough.lstrip("\ufeff"))
    if m is None:
        return ""
    return m.group(0)  # full "---\n...\n---\n"


def _extract_title(rough: str) -> str:
    m = _TITLE_RE.search(rough)
    if m is None:
        return ""
    return f"# {m.group(1).strip()}\n"


def parse_rough_sections(rough: str) -> dict[str, str]:
    """Return ``{slug: section_inner_markdown}`` extracted from the rough page.
    Strips debug scaffolding from each section's contents."""
    out: dict[str, str] = {}
    for m in _SECTION_RE.finditer(rough):
        slug = m.group(1).strip().lower()
        body = m.group(2).strip()
        body = strip_debug_scaffolding(body).strip()
        if not body:
            continue
        out[slug] = body
    return out


# ----------------------------------------------------------------------
# Tag-based generation — classify each rough bullet into wiki sections,
# then write each section from its dedicated slice. Avoids cross-slice
# theme overlap at the source.
# ----------------------------------------------------------------------

# A bullet starts with "- " at column 0. Its continuation lines are either
# indented (starting with space/tab) or start with ">" (quoted source
# sentence). A blank line or a new "- " at column 0 ends the current bullet.

_SCOPE_SECTION_WRITER = ScopeContext(
    scope_id="scope::wiki::section_writer",
    owner_id="jukka",
    actor_id="wiki_section_writer",
    surface="ui",
    room_id="master_room",
    approval=ScopeApprovalPolicy(authority_level=100),
    resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
)


def _load_wiki_sections_resource() -> list:
    """Wiki-scoped convenience wrapper around kg_projection.load_taxonomy.

    Returns ``List[SectionSpec]``. Callers access ``.key`` / ``.title`` /
    ``.description`` attributes (no longer ``s.get("key")`` dict lookups).
    """
    return load_taxonomy(scope_context=_SCOPE_SECTION_WRITER)


def _section_outputs_path(vault_path: Path, entity_label: str) -> Path:
    return vault_path / "section_outputs" / f"{entity_label}.json"


def _load_section_outputs(vault_path: Path, entity_label: str) -> Dict[str, str]:
    path = _section_outputs_path(vault_path, entity_label)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if isinstance(v, str)}
    except Exception as e:
        logger.warning("Failed reading section_outputs sidecar %s: %s", path, e)
    return {}


def _save_section_outputs(vault_path: Path, entity_label: str, outputs: Dict[str, str]) -> None:
    out_dir = vault_path / "section_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _section_outputs_path(vault_path, entity_label)
    path.write_text(json.dumps(outputs, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_prose_page_tagged(
    *,
    entity_label: str,
    vault_path: Path,
) -> Optional[Path]:
    """Tag-based sectioned generation. Each rough bullet is classified into
    zero-or-more biographical sections (cached in ``<vault>/tags/<entity>.json``),
    then each section is written from its dedicated bullet slice. Section
    slices are built by tag, not by rough-renderer bucket, so themes don't
    overlap and no section gets rewritten twice.
    """
    rough = read_rough_page(vault_path, entity_label)
    if not rough:
        logger.warning("No rough page for %s at %s", entity_label, vault_path)
        return None

    frontmatter = _extract_frontmatter(rough)
    title = _extract_title(rough)
    sections_meta = _load_wiki_sections_resource()
    if not sections_meta:
        logger.error("No wiki sections taxonomy found (%s); cannot tag.", WIKI_SECTIONS_RESOURCE)
        return None
    allowed_keys = {s.key for s in sections_meta}
    allowed_block = sections_as_prompt_list(sections_meta)

    # Load bullets directly from the KG neighborhood — structured, with
    # provenance. No more regex round-trip through the rough markdown.
    neighborhood = get_entity_neighborhood(entity_label)
    structured_bullets = render_bullets(neighborhood)
    if not structured_bullets:
        logger.warning("No biographical bullets rendered for %s", entity_label)
        return None

    bullet_texts = [b.text for b in structured_bullets]

    # rough is still parsed for the see_also section (not produced by render_bullets).
    parsed = parse_rough_sections(rough)
    entity_type = neighborhood.entity.node_type or "Entity"

    tagger = DI.agent_factory.create_agent(TAGGER_AGENT)
    if tagger is None:
        raise RuntimeError(f"agent_factory returned None for {TAGGER_AGENT!r}")

    cached = _load_tag_sidecar(vault_path, entity_label)
    tags = tag_bullets(
        tagger,
        entity_label=entity_label,
        entity_type=entity_type,
        bullets=bullet_texts,
        allowed_sections_block=allowed_block,
        allowed_keys=allowed_keys,
        cached_tags=cached,
        scope_context=_SCOPE_SECTION_WRITER,
    )
    _save_tag_sidecar(vault_path, entity_label, tags)

    grouped = _group_bullets_by_section_raw(bullet_texts, tags)

    writer = DI.agent_factory.create_agent(WRITER_AGENT)
    if writer is None:
        raise RuntimeError(f"agent_factory returned None for {WRITER_AGENT!r}")

    window_ids = collect_entity_window_ids(entity_label)
    excerpts = build_window_excerpts(window_ids)

    section_outputs: list[tuple[str, str]] = []
    for sec in sections_meta:
        key = sec.key
        bullets_for_section = grouped.get(key, [])
        if not bullets_for_section:
            continue
        # Build a mini-rough: a fake "## {title}" header + bullets for this section.
        mini_rough = f"## {sec.title}\n\n" + "\n".join(bullets_for_section)
        msg = Message(
            agent_input={
                "entity_name": entity_label,
                "rough_page": mini_rough,
                "window_excerpts": excerpts,
                "section_slug": key,
                # No themes_already_covered — tag-based slices don't overlap at source.
                "themes_already_covered": "",
            },
            scope_context=_SCOPE_SECTION_WRITER,
        )
        try:
            result = writer.action_handler(msg)
            data = getattr(result, "data", None) or {}
            prose = data.get("page_markdown")
            if isinstance(prose, str) and prose.strip():
                cleaned = strip_debug_scaffolding(prose.strip()).strip()
                if cleaned.lower().startswith("(nothing new to add)"):
                    continue
                section_outputs.append((key, cleaned))
        except Exception as e:
            logger.error("wiki_writer failed on section %s of %s: %s", key, entity_label, e)

    if not section_outputs:
        logger.error("All tag-based section calls returned empty for %s", entity_label)
        return None

    # Persist per-section outputs so incremental regen can replace only the
    # sections affected by a node edit without re-running the others.
    _save_section_outputs(
        vault_path, entity_label,
        {k: v for k, v in section_outputs},
    )

    see_also_rough = parsed.get("see_also", "").strip()

    # Build the body first (sections + see_also) so the lead writer has the
    # full picture to summarize.
    body_parts: list[str] = []
    for _key, prose in section_outputs:
        body_parts.append(prose.strip() + "\n")
    if see_also_rough:
        body_parts.append(see_also_rough.strip() + "\n")
    article_body = "\n".join(body_parts).strip()

    # Lead writer — best-effort. Returns "" if the agent fails or yields
    # nothing usable; the page is still written without a lead in that case.
    from app.assistant.wiki_generator.lead_writer import generate_lead
    entity_type = (parsed.get("entity_type") or "").strip() or "Entity"
    lead = generate_lead(
        entity_name=entity_label,
        entity_type=entity_type,
        article_body=article_body,
    )

    from app.assistant.wiki_generator.profile_image import materialize_profile_image_for_vault
    profile_image_rel = materialize_profile_image_for_vault(entity_label, vault_path)

    stitched_parts: list[str] = []
    if frontmatter:
        stitched_parts.append(frontmatter.rstrip() + "\n")
    if title:
        stitched_parts.append(title)
    if profile_image_rel:
        stitched_parts.append(f"![{entity_label}]({profile_image_rel})\n")
    if lead:
        stitched_parts.append(lead.strip() + "\n")
    if article_body:
        stitched_parts.append(article_body + "\n")
    stitched = "\n".join(stitched_parts).strip() + "\n"

    final = apply_references(stitched)
    return write_prose_page(vault_path, entity_label, final)


# ----------------------------------------------------------------------
# Incremental regeneration — only re-write sections affected by node edits.
# ----------------------------------------------------------------------


def regenerate_affected_sections(
    *,
    entity_label: str,
    vault_path: Path,
    changed_node_ids: list[str],
) -> Optional[Path]:
    """Re-write only the sections affected by the given node edits.

    Reuses cached per-section outputs from ``<vault>/section_outputs/<Entity>.json``
    for sections NOT touched by the changes. For sections that contain a
    bullet referencing any of ``changed_node_ids``, calls ``wiki_writer``
    again with the fresh bullet slice. Saves a new sidecar, restitches, and
    writes the new prose page.

    Falls back to full ``generate_prose_page_tagged`` if the section_outputs
    sidecar is missing or empty (can't do incremental without a baseline).
    """
    changed_set = {str(x).strip() for x in changed_node_ids if str(x).strip()}
    if not changed_set:
        logger.info("regenerate_affected_sections: no changed_node_ids; nothing to do.")
        return None

    cached_outputs = _load_section_outputs(vault_path, entity_label)
    if not cached_outputs:
        logger.info(
            "No section_outputs sidecar for %s; falling back to full tagged regen.",
            entity_label,
        )
        return generate_prose_page_tagged(entity_label=entity_label, vault_path=vault_path)

    rough = read_rough_page(vault_path, entity_label)
    if not rough:
        logger.warning("No rough page for %s at %s", entity_label, vault_path)
        return None

    frontmatter = _extract_frontmatter(rough)
    title = _extract_title(rough)
    sections_meta = _load_wiki_sections_resource()
    if not sections_meta:
        logger.error("No wiki sections taxonomy found (%s); cannot regen.", WIKI_SECTIONS_RESOURCE)
        return None
    allowed_keys = {s.key for s in sections_meta}
    allowed_block = sections_as_prompt_list(sections_meta)

    # Load bullets from the current KG (structured, with explicit provenance).
    neighborhood = get_entity_neighborhood(entity_label)
    structured_bullets = render_bullets(neighborhood)
    if not structured_bullets:
        logger.warning("No bullets rendered for %s; falling back to full regen.", entity_label)
        return generate_prose_page_tagged(entity_label=entity_label, vault_path=vault_path)

    bullet_texts = [b.text for b in structured_bullets]
    parsed = parse_rough_sections(rough)
    entity_type = neighborhood.entity.node_type or "Entity"

    # Tag any new/changed bullets (cached lookups skipped).
    tagger = DI.agent_factory.create_agent(TAGGER_AGENT)
    if tagger is None:
        raise RuntimeError(f"agent_factory returned None for {TAGGER_AGENT!r}")
    cached_tags = _load_tag_sidecar(vault_path, entity_label)
    tags = tag_bullets(
        tagger,
        entity_label=entity_label,
        entity_type=entity_type,
        bullets=bullet_texts,
        allowed_sections_block=allowed_block,
        allowed_keys=allowed_keys,
        cached_tags=cached_tags,
        scope_context=_SCOPE_SECTION_WRITER,
    )
    _save_tag_sidecar(vault_path, entity_label, tags)

    grouped = _group_bullets_by_section_raw(bullet_texts, tags)

    # Dirty detection from structured provenance — a bullet is dirty iff its
    # source_node_ids intersect changed_set. No markdown regex needed.
    dirty_bullet_texts: set[str] = set()
    dirty_sections: set[str] = set()
    for b in structured_bullets:
        if set(b.source_node_ids) & changed_set:
            dirty_bullet_texts.add(b.text)
            for sec in tags.get(b.key, []):
                dirty_sections.add(sec)
    # New-section case: a section that didn't exist before but now has
    # bullets (e.g. a retargeted edge brought content in).
    for sec_key, bullets_for_sec in grouped.items():
        if sec_key not in cached_outputs and bullets_for_sec:
            if any(text in dirty_bullet_texts for text in bullets_for_sec):
                dirty_sections.add(sec_key)

    if not dirty_sections:
        logger.info(
            "regenerate_affected_sections: no sections touched by %d changed node(s) for %s.",
            len(changed_set), entity_label,
        )
        return None

    logger.info(
        "Regenerating %d dirty section(s) for %s: %s",
        len(dirty_sections), entity_label, sorted(dirty_sections),
    )

    writer = DI.agent_factory.create_agent(WRITER_AGENT)
    if writer is None:
        raise RuntimeError(f"agent_factory returned None for {WRITER_AGENT!r}")

    window_ids = collect_entity_window_ids(entity_label)
    excerpts = build_window_excerpts(window_ids)

    new_outputs: Dict[str, str] = dict(cached_outputs)  # start with cached

    sec_meta_by_key = {s.key: s for s in sections_meta}

    for key in dirty_sections:
        sec = sec_meta_by_key.get(key)
        if sec is None:
            # Section taxonomy changed? Drop stale cached output.
            new_outputs.pop(key, None)
            continue
        bullets_for_section = grouped.get(key, [])
        if not bullets_for_section:
            # Section emptied by the edit — drop it.
            new_outputs.pop(key, None)
            continue
        mini_rough = f"## {sec.title}\n\n" + "\n".join(bullets_for_section)
        msg = Message(
            agent_input={
                "entity_name": entity_label,
                "rough_page": mini_rough,
                "window_excerpts": excerpts,
                "section_slug": key,
                "themes_already_covered": "",
            },
            scope_context=_SCOPE_SECTION_WRITER,
        )
        try:
            result = writer.action_handler(msg)
            data = getattr(result, "data", None) or {}
            prose = data.get("page_markdown")
            if isinstance(prose, str) and prose.strip():
                cleaned = strip_debug_scaffolding(prose.strip()).strip()
                if cleaned.lower().startswith("(nothing new to add)"):
                    new_outputs.pop(key, None)
                    continue
                new_outputs[key] = cleaned
        except Exception as e:
            logger.error("wiki_writer failed on section %s of %s: %s", key, entity_label, e)

    if not new_outputs:
        logger.error("All sections empty after incremental regen for %s", entity_label)
        return None

    _save_section_outputs(vault_path, entity_label, new_outputs)

    # Restitch in the taxonomy's preferred order.
    see_also_rough = parsed.get("see_also", "").strip()

    body_parts: list[str] = []
    for sec in sections_meta:
        if sec.key in new_outputs:
            body_parts.append(new_outputs[sec.key].strip() + "\n")
    if see_also_rough:
        body_parts.append(see_also_rough.strip() + "\n")
    article_body = "\n".join(body_parts).strip()

    # Lead writer — best-effort. Refreshing affected sections may have
    # changed material the lead summarizes, so re-run the lead too.
    from app.assistant.wiki_generator.lead_writer import generate_lead
    entity_type = (parsed.get("entity_type") or "").strip() or "Entity"
    lead = generate_lead(
        entity_name=entity_label,
        entity_type=entity_type,
        article_body=article_body,
    )

    from app.assistant.wiki_generator.profile_image import materialize_profile_image_for_vault
    profile_image_rel = materialize_profile_image_for_vault(entity_label, vault_path)

    stitched_parts: list[str] = []
    if frontmatter:
        stitched_parts.append(frontmatter.rstrip() + "\n")
    if title:
        stitched_parts.append(title)
    if profile_image_rel:
        stitched_parts.append(f"![{entity_label}]({profile_image_rel})\n")
    if lead:
        stitched_parts.append(lead.strip() + "\n")
    if article_body:
        stitched_parts.append(article_body + "\n")
    stitched = "\n".join(stitched_parts).strip() + "\n"

    final = apply_references(stitched)
    return write_prose_page(vault_path, entity_label, final)
