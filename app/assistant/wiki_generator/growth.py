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

Each page is independent — interruptions don't corrupt anything; the next
run picks up wherever the last one stopped (existing pages get skipped).
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text as sql_text

from app.assistant.kg.db.knowledge_graph_db import get_session
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


DEFAULT_VAULT = Path.home() / "EmiWiki"


def _existing_prose_stems(vault_path: Path) -> set[str]:
    """Set of sanitized filename stems already present in the prose dir.

    Use sanitized stems (matching ``wiki_writer._safe_filename``) because
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


def pick_growth_targets(
    *,
    vault_path: Path,
    limit: int,
    min_degree: int = 4,
) -> List[str]:
    """Top-degree Entity nodes (by total incident edges) without a prose page.

    Returns labels in degree-descending order, capped at ``limit``. Entities
    with fewer than ``min_degree`` edges are skipped — too thin to write
    anything biographical about.

    "Has a prose page" is checked via the sanitized filename stem
    (matching wiki_writer._safe_filename) so labels with special chars
    like "AT&T" or "Irvine, California" correctly map to AT_T.md /
    Irvine_ California.md and don't get re-picked every tick.
    """
    from app.assistant.wiki_generator.wiki_writer import _safe_filename

    skip = _existing_prose_stems(vault_path)
    session = get_session()
    try:
        rows = session.execute(sql_text(
            """
            SELECT n.label, COUNT(e.id) AS deg
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
    finally:
        session.close()

    targets: List[str] = []
    for label, _deg in rows:
        if _safe_filename(label) in skip:
            continue
        targets.append(label)
        if len(targets) >= limit:
            break
    return targets


def build_one_page(label: str, vault_path: Path, run_critic: bool = True) -> Dict[str, Any]:
    """Render one entity's prose page end-to-end. Independent — failures
    don't affect other pages.

    Status values:
      - ``ok``: prose page was written
      - ``empty``: not enough biographical content to write a page
        (render_bullets returned nothing, or the writer LLM produced
        empty output for every section). Not an error — this entity
        just isn't biographical enough; happens for tools, generic
        nouns, abstract concepts.
      - ``error``: an unhandled exception was raised
    """
    from app.assistant.wiki_generator.consistency_critic import run_consistency_critic
    from app.assistant.wiki_generator.page_writer import generate_prose_page_tagged
    from app.assistant.wiki_generator.wiki_writer import regenerate_entity_page

    started = time.time()
    try:
        rough_path = regenerate_entity_page(label=label, vault_path=vault_path)
        prose_path = generate_prose_page_tagged(entity_label=label, vault_path=vault_path)
        if prose_path is None:
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
