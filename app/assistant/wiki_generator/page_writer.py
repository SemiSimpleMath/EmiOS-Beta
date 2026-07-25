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
    EntityNeighborhood,
    SECTIONS_RESOURCE as WIKI_SECTIONS_RESOURCE,
    bullet_key as _bullet_key,
    get_entity_neighborhood,
    group_bullets_by_section as _group_bullets_by_section_raw,
    load_bullet_index as _load_bullet_index_sidecar,
    load_tags as _load_tag_sidecar,
    load_taxonomy,
    render_bullets,
    save_bullet_index as _save_bullet_index_sidecar,
    save_tags as _save_tag_sidecar,
    sections_as_prompt_list,
    tag_bullets,
)
from app.assistant.utils.filename_safety import safe_filename
from app.assistant.utils.logging_config import get_logger
from app.assistant.wiki_generator.references import apply_references
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.identity_names import PRINCIPAL_USER
from app.assistant.utils.pydantic_classes import Message

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


def collect_entity_window_ids(
    entity_label: Optional[str] = None,
    limit: int = MAX_WINDOWS_FOR_EXCERPTS,
    *,
    entity_id: Optional[str] = None,
) -> List[str]:
    """Distinct window_ids associated with any edge OR node touching the entity.

    Pass ``entity_id`` when known — skips the label lookup entirely.
    """
    session = get_session()
    try:
        eid = entity_id
        if not eid:
            entity = (
                session.query(Node)
                .filter(Node.label == entity_label, Node.node_type == "Entity")
                .first()
            )
            if entity is None:
                return []
            eid = entity.id
        # window_id was dropped from kg_node_metadata + kg_edge_metadata on
        # 2026-05-04 (see 09_KG_PIPELINE.md schema migration history). The
        # data lives on the per-observation evidence rows. Edge side needs
        # an extra JOIN to kg_edge_metadata because kg_edge_evidence stores
        # edge_id rather than the source_id/target_id we need to filter on.
        rows = session.execute(
            text(
                """
                SELECT DISTINCT window_id FROM (
                    SELECT ee.window_id
                    FROM kg_edge_evidence ee
                    JOIN kg_edge_metadata em ON em.id = ee.edge_id
                    WHERE ee.window_id IS NOT NULL
                      AND (em.source_id = :eid OR em.target_id = :eid)
                    UNION
                    SELECT ne.window_id
                    FROM kg_node_evidence ne
                    WHERE ne.window_id IS NOT NULL
                      AND ne.node_id = :eid
                )
                LIMIT :lim
                """
            ),
            {"eid": eid, "lim": limit},
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
    """Load the rough markdown page for the entity from the vault.

    Must use the same filename sanitization as the writer (``safe_filename``)
    or entities with characters like ``,``, ``&``, ``@`` (e.g. "Irvine,
    California", "AT&T") will write fine but read back as None.
    """
    candidate = vault_path / f"{safe_filename(entity_label)}.md"
    if not candidate.exists():
        return None
    return candidate.read_text(encoding="utf-8")


def write_prose_page(vault_path: Path, entity_label: str, markdown: str) -> Path:
    """Write the finished prose page under ``<vault>/prose/<safe>.md``.
    Filename sanitization mirrors the rough writer (safe_filename) so the
    rough/prose pair always agree on naming."""
    out_dir = vault_path / "prose"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{safe_filename(entity_label)}.md"
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

_SCOPE_SECTION_WRITER = load_scope_for_source(
    kind="subsystem",
    source_id="wiki_generator",
    actor_id="wiki_section_writer",
    identity_overrides={"owner_id": PRINCIPAL_USER, "actor_id": "wiki_section_writer"},
)


def _load_wiki_sections_resource() -> list:
    """Wiki-scoped convenience wrapper around kg_projection.load_taxonomy.

    Returns ``List[SectionSpec]``. Callers access ``.key`` / ``.title`` /
    ``.description`` attributes (no longer ``s.get("key")`` dict lookups).
    """
    return load_taxonomy(scope_context=_SCOPE_SECTION_WRITER)


def _section_outputs_path(vault_path: Path, entity_label: str) -> Path:
    return vault_path / "section_outputs" / f"{safe_filename(entity_label)}.json"


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
    entity_label: Optional[str] = None,
    entity_node_id: Optional[str] = None,
    vault_path: Path,
    neighborhood: Optional[EntityNeighborhood] = None,
) -> Optional[Path]:
    """Tag-based sectioned generation. Each rough bullet is classified into
    zero-or-more biographical sections (cached in ``<vault>/tags/<entity>.json``),
    then each section is written from its dedicated bullet slice. Section
    slices are built by tag, not by rough-renderer bucket, so themes don't
    overlap and no section gets rewritten twice.

    Pass ``entity_node_id`` when known — survives renames + filename
    sanitization edge cases. Pass ``neighborhood`` when the caller already
    loaded it (refresh paths load once and thread it through the rough
    renderer and this function). The canonical label is taken from the loaded
    neighborhood, so downstream filenames, prompts, and sidecars all track
    the live KG label.
    """
    if neighborhood is None and not entity_node_id and not entity_label:
        raise ValueError(
            "generate_prose_page_tagged requires entity_node_id, entity_label, or neighborhood."
        )

    # Load bullets directly from the KG neighborhood — structured, with
    # provenance. No more regex round-trip through the rough markdown.
    if neighborhood is None:
        neighborhood = get_entity_neighborhood(entity_label, node_id=entity_node_id)
    entity_label = neighborhood.entity.label

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

    window_ids = collect_entity_window_ids(entity_id=neighborhood.entity.id)
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
                # Revision-mode inputs — always empty on full generation.
                "current_section_text": "",
                "added_facts": "",
                "removed_facts": "",
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
        # Expected, not an error: nothing in this entity tagged into a biographical section — it just isn't
        # biographical enough (a place / org / concept, e.g. "Espoo"). The caller treats None as `empty` /
        # SKIP and marks the entity so it isn't re-attempted; see growth.build_one_page.
        logger.info("No biographical sections for %s — not biographical enough (place/org/concept); skipping.",
                    entity_label)
        return None

    # Persist per-section outputs so incremental regen can replace only the
    # sections affected by a node edit without re-running the others.
    _save_section_outputs(
        vault_path, entity_label,
        {k: v for k, v in section_outputs},
    )

    # Snapshot the bullets we just wrote prose from ({key: text}). The next
    # refresh diffs keys against this to detect added/removed bullets — the
    # dirty signal that decides which sections actually need rewriting — and
    # uses the stored text to name removed facts in revision prompts.
    _save_bullet_index_sidecar(
        vault_path, entity_label, {b.key: b.text for b in structured_bullets},
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

    _sync_lead_to_node_description(neighborhood.entity.id, lead, entity_label)

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


def _sync_lead_to_node_description(
    node_id: str, lead: str, entity_label: str,
) -> None:
    """Push the wiki lead into kg_node_metadata.description.

    The wiki page is the canonical "About" — Node.description is a downstream
    projection of it, used by the visualizer's ABOUT sidebar. persist_description
    preserves Node.updated_at so this write is invisible to wiki change
    detection (otherwise every lead update would ripple into every neighbor's
    refresh — see the docstring there).
    """
    if not lead:
        return
    try:
        from app.assistant.pipelines.kg_maintenance_pipeline.description_creator import (
            persist_description,
        )
        persist_description(node_id, lead.strip())
    except Exception as e:
        logger.warning(
            "Failed to sync wiki lead → Node.description for %s (%s): %s",
            entity_label, node_id, e,
        )


# ----------------------------------------------------------------------
# Incremental regeneration — only re-write sections affected by node edits.
# ----------------------------------------------------------------------

# A dirty section is REVISED in place (cached prose + delta facts) when the
# change is a small fraction of its slice; otherwise it is rewritten from the
# full bullet slice. Revision keeps prose stable and pays only for the delta;
# the ratio guard re-grounds heavily-churned sections in the full slice so
# edit-on-edit drift can't accumulate through a big reshuffle.
SECTION_REVISION_MAX_DELTA_RATIO = 0.3


def choose_section_refresh_mode(
    *,
    cached_prose: str,
    slice_size: int,
    added_count: int,
    removed_texts: List[Optional[str]],
) -> str:
    """Return ``'revise'`` or ``'rewrite'`` for one dirty section.

    Revision requires cached prose to edit, a known text for every removed
    bullet (legacy key-only sidecars load removals as None — nothing to tell
    the writer to delete), and a delta that is a small fraction of the
    section's current slice (``SECTION_REVISION_MAX_DELTA_RATIO``).
    """
    if not (cached_prose or "").strip():
        return "rewrite"
    if any(t is None for t in removed_texts):
        return "rewrite"
    delta = added_count + len(removed_texts)
    if delta > max(1, slice_size) * SECTION_REVISION_MAX_DELTA_RATIO:
        return "rewrite"
    return "revise"


def regenerate_affected_sections(
    *,
    entity_label: Optional[str] = None,
    entity_node_id: Optional[str] = None,
    vault_path: Path,
    changed_node_ids: list[str],
    neighborhood: Optional[EntityNeighborhood] = None,
) -> Optional[Path]:
    """Re-write only the sections affected by the given node edits.

    Reuses cached per-section outputs from ``<vault>/section_outputs/<Entity>.json``
    for sections NOT touched by the changes. A dirty section is either
    REVISED (the cached prose plus only the added/removed bullet texts —
    the cheap, stable path) or REWRITTEN from its fresh bullet slice when
    revision isn't possible or the delta is too large — see
    ``choose_section_refresh_mode``. Saves a new sidecar, restitches, and
    writes the new prose page.

    Pass ``neighborhood`` when the caller already loaded it.

    Falls back to full ``generate_prose_page_tagged`` if the section_outputs
    sidecar is missing or empty (can't do incremental without a baseline).
    """
    if neighborhood is None and not entity_node_id and not entity_label:
        raise ValueError(
            "regenerate_affected_sections requires entity_node_id, entity_label, or neighborhood."
        )

    changed_set = {str(x).strip() for x in changed_node_ids if str(x).strip()}
    if not changed_set:
        logger.info("regenerate_affected_sections: no changed_node_ids; nothing to do.")
        return None

    # Load bullets from the current KG (structured, with explicit provenance).
    if neighborhood is None:
        neighborhood = get_entity_neighborhood(entity_label, node_id=entity_node_id)
    entity_label = neighborhood.entity.label

    cached_outputs = _load_section_outputs(vault_path, entity_label)
    if not cached_outputs:
        logger.info(
            "No section_outputs sidecar for %s; falling back to full tagged regen.",
            entity_label,
        )
        return generate_prose_page_tagged(
            entity_label=entity_label,
            entity_node_id=neighborhood.entity.id,
            vault_path=vault_path,
            neighborhood=neighborhood,
        )

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

    structured_bullets = render_bullets(neighborhood)
    if not structured_bullets:
        logger.warning("No bullets rendered for %s; falling back to full regen.", entity_label)
        return generate_prose_page_tagged(
            entity_label=entity_label,
            entity_node_id=neighborhood.entity.id,
            vault_path=vault_path,
            neighborhood=neighborhood,
        )

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

    # ---------- structural dirty detection via bullet-key diff ----------
    # A bullet's key is hash(bullet.text), so identical text → identical key.
    # The bullet_index sidecar records ``{key: text}`` for the bullets present
    # at the LAST successful rewrite. Comparing key sets gives us:
    #   added   = current - prev → section gained a bullet (dirty, gate)
    #   removed = prev - current → section lost a bullet (dirty, no gate —
    #                              section text mentions a fact that's gone;
    #                              the stored text names it for the reviser)
    #   unchanged keys imply unchanged TEXT, even if the underlying nodes
    #   were touched for unrelated reasons (importance recalc, description
    #   refresh, new edges that don't touch this neighborhood). Those changes
    #   leave bullet text identical → no rewrite needed.
    #
    # changed_node_ids is no longer the dirty trigger — it survives only as
    # the cheap pre-filter at the caller level (refresh_one_page /
    # build_one_page) that decides whether to even open this page's
    # neighborhood.
    prev_index = _load_bullet_index_sidecar(vault_path, entity_label)
    curr_keys = {b.key for b in structured_bullets}
    added_keys = curr_keys - set(prev_index)
    removed_keys = set(prev_index) - curr_keys

    # Per-section deltas: which added bullets landed in each section, and the
    # last-known text of each removed bullet that was tagged into it. These
    # drive the critic gate, the revise-vs-rewrite decision, and the
    # revision prompt itself.
    added_by_section: dict[str, List[Bullet]] = {}
    _seen_added: set[tuple[str, str]] = set()
    for b in structured_bullets:
        if b.key not in added_keys:
            continue
        for sec_key in tags.get(b.key, []):
            if (sec_key, b.key) in _seen_added:
                continue
            _seen_added.add((sec_key, b.key))
            added_by_section.setdefault(sec_key, []).append(b)
    removed_texts_by_section: dict[str, List[Optional[str]]] = {}
    for k in removed_keys:
        for sec_key in cached_tags.get(k, []):
            removed_texts_by_section.setdefault(sec_key, []).append(prev_index.get(k))

    addition_sections: set[str] = set(added_by_section)
    removal_sections: set[str] = set(removed_texts_by_section)
    dirty_sections: set[str] = addition_sections | removal_sections

    if not dirty_sections:
        logger.info(
            "regenerate_affected_sections: %s — %d added / %d removed bullets, "
            "no sections touched (text-diff clean).",
            entity_label, len(added_keys), len(removed_keys),
        )
        return None

    # ---------- nano critic gate ----------
    # Per dirty section, ask wiki_inclusion_critic whether ADDED bullets are
    # worth incorporating. Sections dirty due to REMOVAL skip the gate
    # entirely — there's no bullet to evaluate, and the section's current
    # prose mentions a fact that no longer exists, so it must be rewritten.
    # Most chat-extracted ephemera fails this gate, dropping the section
    # back to clean before we pay for the expensive prose writer.
    sec_meta_by_key_for_gate = {s.key: s for s in sections_meta}
    critic = DI.agent_factory.create_agent("wiki_inclusion_critic")
    if critic is None:
        logger.warning(
            "wiki_inclusion_critic agent unavailable; falling back to ungated dirty_sections."
        )
    else:
        approved_sections: set[str] = set(removal_sections)  # always include removals
        gate_candidates = addition_sections - removal_sections  # remaining for critic
        for sec_key in sorted(gate_candidates):
            sec = sec_meta_by_key_for_gate.get(sec_key)
            if sec is None:
                continue
            current_text = (cached_outputs.get(sec_key) or "").strip()
            triggering_bullets = [b.text for b in added_by_section.get(sec_key, [])]
            if not triggering_bullets:
                continue
            for b_text in triggering_bullets:
                try:
                    msg = Message(
                        agent_input={
                            "task": "",
                            "information": "",
                            "section_title": sec.title,
                            "section_slug": sec_key,
                            "current_section_text": current_text,
                            "new_fact_sentence": b_text,
                        },
                        scope_context=_SCOPE_SECTION_WRITER,
                    )
                    result = critic.action_handler(msg)
                    data = getattr(result, "data", None) or {}
                    if bool(data.get("include")):
                        approved_sections.add(sec_key)
                        break
                except Exception as e:
                    logger.warning(
                        "wiki_inclusion_critic failed on %s/%s: %s — admitting by default",
                        entity_label, sec_key, e,
                    )
                    approved_sections.add(sec_key)
                    break
        gated_out = dirty_sections - approved_sections
        if gated_out:
            logger.info(
                "wiki_inclusion_critic gated out %d section(s) for %s: %s",
                len(gated_out), entity_label, sorted(gated_out),
            )
        dirty_sections = approved_sections
        if not dirty_sections:
            logger.info(
                "All dirty sections gated out by critic for %s — nothing to rewrite.",
                entity_label,
            )
            return None

    logger.info(
        "Regenerating %d dirty section(s) for %s: %s",
        len(dirty_sections), entity_label, sorted(dirty_sections),
    )

    writer = DI.agent_factory.create_agent(WRITER_AGENT)
    if writer is None:
        raise RuntimeError(f"agent_factory returned None for {WRITER_AGENT!r}")

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

        added_bullets = added_by_section.get(key, [])
        removed_texts = removed_texts_by_section.get(key, [])
        mode = choose_section_refresh_mode(
            cached_prose=cached_outputs.get(key) or "",
            slice_size=len(bullets_for_section),
            added_count=len(added_bullets),
            removed_texts=removed_texts,
        )
        logger.info(
            "Section %s of %s: %s (%d added / %d removed, slice=%d)",
            key, entity_label, mode,
            len(added_bullets), len(removed_texts), len(bullets_for_section),
        )

        # Grounding excerpts come from the ADDED bullets' own source windows —
        # the conversations that actually produced the new facts — not an
        # entity-wide sample.
        window_ids: List[str] = []
        for b in added_bullets:
            for wid in b.source_window_ids:
                if wid and wid not in window_ids:
                    window_ids.append(wid)
        excerpts = build_window_excerpts(window_ids[:MAX_WINDOWS_FOR_EXCERPTS])

        if mode == "revise":
            agent_input = {
                "entity_name": entity_label,
                "rough_page": "",
                "window_excerpts": excerpts,
                "section_slug": key,
                "themes_already_covered": "",
                "current_section_text": cached_outputs.get(key) or "",
                "added_facts": "\n".join(b.text for b in added_bullets),
                "removed_facts": "\n".join(t for t in removed_texts if t),
            }
        else:
            mini_rough = f"## {sec.title}\n\n" + "\n".join(bullets_for_section)
            agent_input = {
                "entity_name": entity_label,
                "rough_page": mini_rough,
                "window_excerpts": excerpts,
                "section_slug": key,
                "themes_already_covered": "",
                "current_section_text": "",
                "added_facts": "",
                "removed_facts": "",
            }
        msg = Message(agent_input=agent_input, scope_context=_SCOPE_SECTION_WRITER)
        try:
            result = writer.action_handler(msg)
            data = getattr(result, "data", None) or {}
            prose = data.get("page_markdown")
            if isinstance(prose, str) and prose.strip():
                cleaned = strip_debug_scaffolding(prose.strip()).strip()
                low = cleaned.lower()
                if low.startswith("(no changes needed)"):
                    # Reviser judged the cached prose still correct — keep it.
                    continue
                if low.startswith("(nothing new to add)"):
                    if mode == "revise":
                        # Additions not wiki-worthy — keep the cached prose.
                        continue
                    new_outputs.pop(key, None)
                    continue
                new_outputs[key] = cleaned
        except Exception as e:
            logger.error("wiki_writer failed on section %s of %s: %s", key, entity_label, e)

    if not new_outputs:
        logger.error("All sections empty after incremental regen for %s", entity_label)
        return None

    _save_section_outputs(vault_path, entity_label, new_outputs)

    # Checkpoint the bullets ({key: text}) we just successfully wrote prose
    # from. Saved AFTER section_outputs so a crash here at worst leaves the
    # next run re-detecting everything as dirty — self-healing.
    _save_bullet_index_sidecar(
        vault_path, entity_label, {b.key: b.text for b in structured_bullets},
    )

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

    _sync_lead_to_node_description(neighborhood.entity.id, lead, entity_label)

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
