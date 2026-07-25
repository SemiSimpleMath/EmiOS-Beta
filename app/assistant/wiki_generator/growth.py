"""Wiki growth runner.

Picks the highest-degree Entity nodes that don't yet have a prose page in
the vault, and builds prose pages for them (rough render → tagged prose →
consistency critic). Designed to be:

  - called as a routine function (see configs/routines.json) with a small
    daily increment so the wiki grows organically over time, and
  - called from a scratch script with a large limit for one-off catch-up
    batches.

Edge-count is a cheap proxy for "important enough to write about." We
filter to entities with at least ``min_degree`` total incident edges
(default 4) so we don't waste LLM calls on hapax nodes the extractor
mentioned once.

When an entity's prose generation returns empty (the writer LLM produced
no content for any section — typical for tools, generic nouns, abstract
concepts), we drop a sidecar marker at ``<vault>/empty/<stem>.json``
recording the entity's edge count at the time. Subsequent ticks skip the
entity unless its edge count has grown since the marker was written, so
we don't re-pay LLM cost on the same dead-ends every night, but new KG
content for an entity does automatically retry.

Each page is independent — interruptions don't corrupt anything; the next
run picks up wherever the last one stopped (existing pages get skipped).
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text as sql_text

from app.assistant.kg.db.knowledge_graph_db import get_session
from app.assistant.utils.filename_safety import safe_filename
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


DEFAULT_VAULT = Path.home() / "EmiWiki"


def _existing_prose_stems(vault_path: Path) -> set[str]:
    """Set of sanitized filename stems already present in the prose dir.

    Use sanitized stems (matching ``utils.filename_safety.safe_filename``) because
    that's how files are named on disk — entities like "AT&T" land at
    ``prose/AT_T.md`` so the raw label "AT&T" wouldn't match a stem-based
    set. ``pick_growth_targets`` sanitizes each candidate before lookup.
    """
    prose_dir = vault_path / "prose"
    if not prose_dir.exists():
        return set()
    return {
        p.stem for p in prose_dir.glob("*.md")
        if p.stem.lower() not in {"log", "index"}
    }


def _empty_dir(vault_path: Path) -> Path:
    return vault_path / "empty"


def _existing_empty_stems_with_degree(vault_path: Path) -> Dict[str, int]:
    """Map sanitized stem → edge_count_at_marker for every empty marker.

    The marker file ``<vault>/empty/<stem>.json`` records the entity's
    edge count at the moment we gave up. ``pick_growth_targets`` uses it
    to retry an entity only if its edge count has grown — so genuinely
    stuck entities don't burn LLM tokens nightly, but new KG content
    automatically promotes them back into the queue.
    """
    out: Dict[str, int] = {}
    edir = _empty_dir(vault_path)
    if not edir.exists():
        return out
    for p in edir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            deg = data.get("edge_count_at_marker")
            if isinstance(deg, int):
                out[p.stem] = deg
        except Exception:
            # Corrupt marker — be conservative and treat as "skip forever."
            out[p.stem] = 1 << 30
    return out


def mark_entity_empty(vault_path: Path, entity_label: str, edge_count: int) -> None:
    """Drop a sidecar marker so future ticks skip this entity until its
    KG edge count grows past ``edge_count``."""
    edir = _empty_dir(vault_path)
    edir.mkdir(parents=True, exist_ok=True)
    target = edir / f"{safe_filename(entity_label)}.json"
    target.write_text(
        json.dumps(
            {
                "entity_label": entity_label,
                "edge_count_at_marker": int(edge_count),
                "marked_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def pick_growth_targets(
    *,
    vault_path: Path,
    limit: int,
    min_degree: int = 4,
) -> List[str]:
    """Top-degree Entity nodes that pass `is_wiki_growth_candidate` and
    don't yet have a prose page.

    Returns labels in degree-descending order, capped at ``limit``. Skips:
      - entities below ``min_degree`` OR below `WIKI_GROWTH_IMPORTANCE_FLOOR`
        — both gates apply via `is_wiki_growth_candidate`; the SQL pre-
        filters on degree (cheap), then the Python loop confirms
        importance per surviving candidate;
      - entities whose sanitized stem already has a prose file;
      - entities marked empty whose edge count hasn't grown past the
        marker's recorded count (so new KG content auto-retries).
    """
    from app.assistant.importance.consumers import is_wiki_growth_candidate

    skip_prose = _existing_prose_stems(vault_path)
    skip_empty = _existing_empty_stems_with_degree(vault_path)
    session = get_session()
    try:
        # SQL pre-filter on degree (cheap); apply the importance check
        # below per row so we don't have to thread effective_importance
        # into raw SQL. The pre-filter cuts the candidate population
        # enough that per-row Python work is fine.
        rows = session.execute(sql_text(
            """
            SELECT n.id, n.label, COUNT(e.id) AS deg
            FROM kg_node_metadata n
            JOIN kg_edge_metadata e
              ON e.source_id = n.id OR e.target_id = n.id
            WHERE n.node_type = 'Entity'
              AND n.label IS NOT NULL
              AND n.label != ''
            GROUP BY n.id, n.label
            HAVING deg >= :min_deg
            ORDER BY deg DESC
            """
        ), {"min_deg": int(min_degree)}).fetchall()

        # Resolve each surviving row to a Node so is_wiki_growth_candidate
        # can apply the importance floor. Done in a single batch query
        # for efficiency rather than one round-trip per node.
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        node_ids = [r[0] for r in rows]
        node_by_id = {
            str(n.id): n
            for n in session.query(Node).filter(Node.id.in_(node_ids)).all()
        }
    finally:
        session.close()

    targets: List[str] = []
    for nid, label, deg in rows:
        stem = safe_filename(label)
        if stem in skip_prose:
            continue
        prior_deg = skip_empty.get(stem)
        if prior_deg is not None and int(deg) <= prior_deg:
            # Marked empty before, no new KG content since — don't retry.
            continue
        node = node_by_id.get(str(nid))
        if node is None:
            continue
        if not is_wiki_growth_candidate(node, degree=int(deg)):
            continue
        targets.append(label)
        if len(targets) >= limit:
            break
    return targets


def _entity_edge_count(label: str) -> int:
    """Lookup the entity's current incident-edge count in the KG."""
    session = get_session()
    try:
        row = session.execute(sql_text(
            """
            SELECT COUNT(e.id) AS deg
            FROM kg_node_metadata n
            JOIN kg_edge_metadata e
              ON e.source_id = n.id OR e.target_id = n.id
            WHERE n.node_type = 'Entity'
              AND n.label = :label
            """
        ), {"label": label}).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        session.close()


def _refresh_existing_page(
    label: str, vault_path: Path, run_critic: bool,
) -> Optional[Dict[str, Any]]:
    """Incremental path for an entity whose prose page already exists.

    Returns None when there is no usable baseline (no prose page, no
    ``kg_node_id``/``generated_at`` frontmatter, or no section_outputs
    sidecar) — the caller falls back to full generation. Otherwise diffs
    the KG neighborhood against ``generated_at`` and rewrites only the
    sections the changes actually touched.

    Unlike the nightly refresh, no cooldown and no importance floor apply:
    this path serves deliberate refreshes (the ``refresh_wiki_page`` tool)
    invoked right after specific KG mutations, so every change counts.
    """
    import frontmatter

    from app.assistant.kg_projection import (
        find_changed_neighborhood_nodes,
        get_entity_neighborhood,
    )
    from app.assistant.wiki_generator.consistency_critic import run_consistency_critic
    from app.assistant.wiki_generator.nightly_refresh import _parse_iso
    from app.assistant.wiki_generator.page_writer import (
        _load_section_outputs,
        regenerate_affected_sections,
    )
    from app.assistant.wiki_generator.wiki_writer import regenerate_entity_page

    prose_path = vault_path / "prose" / f"{safe_filename(label)}.md"
    if not prose_path.exists():
        return None
    try:
        meta = frontmatter.load(prose_path).metadata or {}
    except Exception as e:
        logger.warning(
            "Frontmatter parse failed for %s: %s — falling back to full regen.",
            prose_path, e,
        )
        return None
    kg_node_id = meta.get("kg_node_id")
    generated_at = _parse_iso(meta.get("generated_at"))
    if not kg_node_id or generated_at is None:
        return None
    if not _load_section_outputs(vault_path, label):
        return None

    changed = find_changed_neighborhood_nodes(
        entity_node_id=str(kg_node_id), since_ts=generated_at,
    )
    if not changed:
        return {"label": label, "status": "unchanged", "changed": 0}

    neighborhood = get_entity_neighborhood(node_id=str(kg_node_id))
    rough_path = regenerate_entity_page(
        node_id=str(kg_node_id), vault_path=vault_path, neighborhood=neighborhood,
    )
    result = regenerate_affected_sections(
        entity_node_id=str(kg_node_id),
        vault_path=vault_path,
        changed_node_ids=list(changed),
        neighborhood=neighborhood,
    )
    if result is None:
        # Bullet-text diff came back clean, or the inclusion critic gated
        # every dirty section out — the prose is already up to date.
        return {"label": label, "status": "unchanged", "changed": len(changed)}
    crit_summary = (
        run_consistency_critic(entity_label=neighborhood.entity.label, vault_path=vault_path)
        if run_critic else None
    )
    return {
        "label": neighborhood.entity.label,
        "status": "ok",
        "mode": "incremental",
        "changed": len(changed),
        "rough": str(rough_path),
        "prose": str(result),
        "critic_findings": (crit_summary or {}).get("findings_count"),
    }


def build_one_page(label: str, vault_path: Path, run_critic: bool = True) -> Dict[str, Any]:
    """Render one entity's prose page end-to-end. Independent — failures
    don't affect other pages.

    An entity with an existing prose page and refresh baseline (frontmatter
    ``kg_node_id`` + ``generated_at``, section_outputs sidecar) takes the
    incremental path: only sections affected by KG changes since the page
    was written are rewritten. Full generation runs only when there is no
    baseline.

    Status values:
      - ``ok``: prose page was written. ``mode: "incremental"`` marks a
        sections-only refresh of an existing page; absent for a full
        generation.
      - ``unchanged``: the page exists and no KG change affects it —
        nothing was rewritten.
      - ``empty``: not enough biographical content to write a page
        (render_bullets returned nothing, or the writer LLM produced
        empty output for every section). Not an error — this entity
        just isn't biographical enough; happens for tools, generic
        nouns, abstract concepts.
      - ``error``: an unhandled exception was raised
    """
    from app.assistant.kg_projection import get_entity_neighborhood
    from app.assistant.wiki_generator.consistency_critic import run_consistency_critic
    from app.assistant.wiki_generator.page_writer import generate_prose_page_tagged
    from app.assistant.wiki_generator.wiki_writer import regenerate_entity_page

    started = time.time()
    try:
        refreshed = _refresh_existing_page(label, vault_path, run_critic)
        if refreshed is not None:
            refreshed["elapsed_sec"] = round(time.time() - started, 1)
            return refreshed

        neighborhood = get_entity_neighborhood(label)
        rough_path = regenerate_entity_page(
            label=label, vault_path=vault_path, neighborhood=neighborhood,
        )
        prose_path = generate_prose_page_tagged(
            entity_label=label, vault_path=vault_path, neighborhood=neighborhood,
        )
        if prose_path is None:
            try:
                deg = _entity_edge_count(label)
                mark_entity_empty(vault_path, label, deg)
            except Exception as e:
                logger.warning("mark_entity_empty failed for %s: %s", label, e)
            return {
                "label": label,
                "status": "empty",
                "rough": str(rough_path) if rough_path else None,
                "prose": None,
                "critic_findings": None,
                "elapsed_sec": round(time.time() - started, 1),
            }
        crit_summary: Optional[Dict[str, Any]] = None
        if run_critic:
            crit_summary = run_consistency_critic(entity_label=label, vault_path=vault_path)
        elapsed = time.time() - started
        return {
            "label": label,
            "status": "ok",
            "rough": str(rough_path) if rough_path else None,
            "prose": str(prose_path),
            "critic_findings": (crit_summary or {}).get("findings_count"),
            "elapsed_sec": round(elapsed, 1),
        }
    except Exception as e:
        return {
            "label": label,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "elapsed_sec": round(time.time() - started, 1),
        }


def run_wiki_growth(
    *,
    vault_path: Optional[Path] = None,
    max_new_pages: int = 5,
    min_degree: int = 4,
    run_critic: bool = True,
) -> Dict[str, Any]:
    """Build up to ``max_new_pages`` prose pages for the highest-degree
    Entity nodes that don't yet have one. Returns a counts+per-page summary.
    Safe to interrupt — every page is independent.
    """
    vault = vault_path or DEFAULT_VAULT
    targets = pick_growth_targets(vault_path=vault, limit=max_new_pages, min_degree=min_degree)
    logger.info("Wiki growth: vault=%s picked=%d/%d (min_degree=%d)", vault, len(targets), max_new_pages, min_degree)

    summaries: List[Dict[str, Any]] = []
    for i, label in enumerate(targets, start=1):
        logger.info("Wiki growth: START (%d/%d) label=%r", i, len(targets), label)
        r = build_one_page(label, vault_path=vault, run_critic=run_critic)
        summaries.append(r)
        status = r["status"]
        if status == "ok":
            logger.info(
                "Wiki growth: DONE  (%d/%d) label=%r elapsed=%.1fs critic_findings=%s",
                i, len(targets), label, r["elapsed_sec"], r.get("critic_findings"),
            )
        elif status == "empty":
            logger.info(
                "Wiki growth: SKIP  (%d/%d) label=%r — not biographical enough",
                i, len(targets), label,
            )
        else:
            logger.error("Wiki growth: ERROR (%d/%d) label=%r %s", i, len(targets), label, r.get("error"))

    counts = {
        "targets_picked": len(targets),
        "max_new_pages": max_new_pages,
        "ok": sum(1 for s in summaries if s.get("status") == "ok"),
        "empty": sum(1 for s in summaries if s.get("status") == "empty"),
        "errors": sum(1 for s in summaries if s.get("status") == "error"),
    }
    logger.info("Wiki growth complete: %s", counts)
    return {"counts": counts, "pages": summaries}
