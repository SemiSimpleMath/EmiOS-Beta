"""Nightly wiki refresh runner.

Scans the vault for existing prose pages, detects which entities' KG
neighborhoods have changed since the page's ``generated_at`` timestamp, and
incrementally regenerates only the sections affected by those changes.
Optionally runs the consistency critic on each refreshed page afterward.

Before the timestamp diff runs, ``_apply_pre_refresh_revision_log_effects``
consults ``kg_revision_log`` for mutator events that the timestamp logic
can't see by itself:

  - ``merge_nodes``: pages whose ``kg_node_id`` was the fold target are
    silently orphaned (the row is gone, so node-lookup returns None and
    the diff is empty). Rewrite frontmatter to point at the keep node,
    chasing chained merges to land on the final id.
  - ``delete_edge``: an edge that no longer exists has no ``updated_at``
    to read, so the timestamp diff can't flag it. Pre-collect both
    endpoints from ``before_json`` and force-flag the non-self endpoint
    for the page entity that still exists.

Designed to be called as a routine function (see configs/routines.json).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import frontmatter

from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.kg_projection import (
    find_changed_neighborhood_nodes,
    get_entity_neighborhood,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.wiki_generator.consistency_critic import run_consistency_critic
from app.assistant.wiki_generator.page_writer import (
    generate_prose_page_tagged,
    regenerate_affected_sections,
)
from app.assistant.wiki_generator.wiki_writer import regenerate_entity_page
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

def _get_assistant_name() -> str:
    """Read assistant name (e.g. 'Floppy') from resources/assistant/
    assistant_core.json. Fallback: 'Emi'."""
    try:
        from app.assistant.utils.path_utils import get_resources_dir
        import json as _json
        path = get_resources_dir() / "assistant" / "assistant_core.json"
        if not path.exists():
            return "Emi"
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict):
            n = str(data.get("name") or data.get("assistant_name") or "").strip()
            if n:
                return n
    except Exception:
        pass
    return "Emi"


def _default_vault() -> Path:
    """Per-assistant vault root, lazily evaluated. Override via EMI_WIKI_DIR; else
    under the data dir when EMI_DATA_DIR is set; else ~/<Name>Wiki (legacy/dev)."""
    override = os.environ.get("EMI_WIKI_DIR")
    if override:
        return Path(override)
    if os.environ.get("EMI_DATA_DIR"):
        from app.assistant.utils.path_utils import get_data_dir, get_resources_dir
        return get_data_dir() / "wiki"
    return Path.home() / f"{_get_assistant_name()}Wiki"


# Back-compat — code that imported DEFAULT_VAULT still works (callable shim
# that resolves on access). Prefer _default_vault() in new code.
class _LazyVault:
    def __truediv__(self, other): return _default_vault() / other
    def __fspath__(self): return str(_default_vault())
    def __str__(self): return str(_default_vault())
    def __getattr__(self, name): return getattr(_default_vault(), name)

DEFAULT_VAULT = _LazyVault()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from frontmatter. Returns UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _list_prose_pages(vault_path: Path) -> List[Dict[str, Any]]:
    """Return one entry per prose page with (label, path, generated_at, kg_node_id).

    ``label`` is the canonical KG label from frontmatter (`name:` field), not
    the filename stem. Stems are sanitized via ``utils.filename_safety.safe_filename`` (chars like
    '&', ',', '@' become '_'), so a page about AT&T lives at ``AT_T.md`` —
    the stem is unsafe to use as the KG lookup key. Fall back to the stem
    only when frontmatter is missing or malformed (legacy / hand-edited).
    """
    prose_dir = vault_path / "prose"
    if not prose_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(prose_dir.glob("*.md")):
        if p.stem.lower() in {"log", "index"}:
            continue
        try:
            post = frontmatter.load(p)
            meta = post.metadata or {}
        except Exception as e:
            logger.warning("Skipping %s — frontmatter parse failed: %s", p, e)
            continue
        canonical = str(meta.get("name") or "").strip() or p.stem
        out.append({
            "label": canonical,
            "path": p,
            "generated_at": _parse_iso(meta.get("generated_at")),
            "kg_node_id": meta.get("kg_node_id"),
        })
    return out


def _apply_pre_refresh_revision_log_effects(
    pages: List[Dict[str, Any]],
    vault_path: Path,
) -> Dict[str, Set[str]]:
    """Pre-process pages against kg_revision_log mutator events.

    Mutates ``pages`` in place: rewrites frontmatter ``kg_node_id`` for
    pages whose entity was folded into another node by ``merge_nodes``.
    Returns ``{label: {forced_changed_node_id, ...}}`` for ``delete_edge``
    events that the timestamp diff would otherwise miss (deleted rows
    have no ``updated_at`` to read).
    """
    cutoffs = [p["generated_at"] for p in pages if p.get("generated_at")]
    if not cutoffs:
        return {}
    earliest = min(cutoffs)

    with get_db_manager().read_session() as s:
        rows = (
            s.query(KGRevisionLog)
            .filter(KGRevisionLog.op.in_(["merge_nodes", "delete_edge"]))
            .filter(KGRevisionLog.created_at > earliest)
            .filter(KGRevisionLog.succeeded == 1)
            .order_by(KGRevisionLog.created_at.asc())
            .all()
        )
        events = [(r.op, r.args_json or {}, r.before_json or {}) for r in rows]

    # Collapse chained merges (A→B then B→C should land an A-page on C).
    merges_by_fold: Dict[str, str] = {}
    for op, args, _before in events:
        if op != "merge_nodes":
            continue
        fold = args.get("fold_id")
        keep = args.get("keep_id")
        if not fold or not keep:
            continue
        while keep in merges_by_fold:
            keep = merges_by_fold[keep]
        merges_by_fold[fold] = keep
        for f, k in list(merges_by_fold.items()):
            if k == fold:
                merges_by_fold[f] = keep

    for p in pages:
        nid = p.get("kg_node_id")
        if not nid or nid not in merges_by_fold:
            continue
        new_id = merges_by_fold[nid]
        try:
            post = frontmatter.load(p["path"])
            post.metadata["kg_node_id"] = new_id
            p["path"].write_text(frontmatter.dumps(post), encoding="utf-8")
            logger.info(
                "Page %s: kg_node_id rewrite %s → %s (folded by merge_nodes)",
                p["label"], nid[:8], new_id[:8],
            )
            p["kg_node_id"] = new_id
        except Exception as exc:
            logger.warning(
                "Failed to rewrite kg_node_id for %s: %s", p["label"], exc,
            )

    page_id_index = {p["kg_node_id"]: p for p in pages if p.get("kg_node_id")}
    force: Dict[str, Set[str]] = defaultdict(set)
    for op, _args, before in events:
        if op != "delete_edge":
            continue
        src = before.get("source_id")
        tgt = before.get("target_id")
        if not src or not tgt:
            continue
        if src in page_id_index:
            force[page_id_index[src]["label"]].add(tgt)
        if tgt in page_id_index:
            force[page_id_index[tgt]["label"]].add(src)
    return dict(force)


PER_PAGE_REFRESH_COOLDOWN_DAYS = 7

# Minimum node importance (0-10 scale) for a changed node to even be
# considered for triggering a section refresh. Drops ephemeral chat-extracted
# noise before the critic gate runs. Tunable; 4 keeps "median-ish" content
# moving and excludes the long tail of trivia.
from app.assistant.importance.consumers import WIKI_REFRESH_CHANGE_FLOOR


def _filter_changed_by_importance(
    changed_ids: List[str], threshold: float = WIKI_REFRESH_CHANGE_FLOOR,
) -> List[str]:
    """Drop ids whose effective_importance is NULL or below threshold.

    NULL is treated as "below threshold" — unrated content shouldn't trigger
    refresh until the periodic kg_importance_rater routine has rated it.

    Uses `effective_importance` so damping (when calibrated) will
    automatically suppress artifact-pattern nodes from triggering wiki
    refresh storms.
    """
    if not changed_ids:
        return []
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.importance.effective import effective_importance
    from app.models.db_manager import get_db_manager
    kept: List[str] = []
    with get_db_manager().read_session() as session:
        nodes = (
            session.query(Node)
            .filter(Node.id.in_(list(changed_ids)))
            .all()
        )
        node_by_id = {str(n.id): n for n in nodes}
    for nid in changed_ids:
        node = node_by_id.get(str(nid))
        if node is None or node.importance is None:
            continue
        if effective_importance(node) >= threshold:
            kept.append(nid)
    return kept


def refresh_one_page(
    *,
    label: str,
    page_path: Path,
    generated_at: Optional[datetime],
    kg_node_id: Optional[str],
    vault_path: Path,
    run_critic: bool,
    force_changed_node_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Refresh one wiki page. Returns a summary of what happened.

    ``force_changed_node_ids`` lets callers seed the changed-node set with
    ids the timestamp diff can't see (e.g., endpoints of deleted edges).

    Per-page cooldown: a page is never rewritten more than once per
    PER_PAGE_REFRESH_COOLDOWN_DAYS, regardless of how often the refresh
    routine runs (catch-up or otherwise). Applied AFTER the no-id and
    first-time-generation gates so brand-new pages still get their initial
    generation, but any subsequent refresh is throttled.
    """
    if kg_node_id is None:
        return {"label": label, "status": "no_kg_node_id", "changed": 0}
    if generated_at is None:
        # Never been generated under tracking — regenerate fully.
        # One neighborhood load feeds both the rough renderer and the prose
        # generator.
        neighborhood = get_entity_neighborhood(node_id=kg_node_id)
        regenerate_entity_page(
            node_id=kg_node_id, vault_path=vault_path, neighborhood=neighborhood,
        )
        generate_prose_page_tagged(
            entity_node_id=kg_node_id, vault_path=vault_path,
            neighborhood=neighborhood,
        )
        crit = (
            run_consistency_critic(entity_label=label, vault_path=vault_path)
            if run_critic else None
        )
        return {
            "label": label, "status": "full_regen_no_timestamp",
            "changed": None,
            "critic_findings": (crit or {}).get("findings_count"),
        }

    # Per-page cooldown — protects against catch-up thrash and ad-hoc invocations.
    age = datetime.now(timezone.utc) - generated_at
    if age.days < PER_PAGE_REFRESH_COOLDOWN_DAYS:
        return {
            "label": label,
            "status": "cooldown_skip",
            "changed": 0,
            "age_days": age.days,
            "cooldown_days": PER_PAGE_REFRESH_COOLDOWN_DAYS,
        }

    changed = set(find_changed_neighborhood_nodes(
        entity_node_id=kg_node_id, since_ts=generated_at,
    ))
    if force_changed_node_ids:
        changed |= force_changed_node_ids
    if not changed:
        return {"label": label, "status": "unchanged", "changed": 0}

    # Importance pre-filter: drop low-importance changes before the critic
    # gate / writer ever sees them. Most chat-extracted ephemera dies here
    # for free (no LLM cost). Only nodes meeting the threshold proceed.
    raw_count = len(changed)
    changed = sorted(_filter_changed_by_importance(list(changed)))
    if not changed:
        return {
            "label": label,
            "status": "unchanged_after_importance_filter",
            "changed": 0,
            "raw_changed_count": raw_count,
        }

    # Rough needs to be up-to-date with current KG before we diff bullets.
    # One neighborhood load feeds both the rough renderer and the section
    # refresher (loaded only now — unchanged pages never pay for it).
    neighborhood = get_entity_neighborhood(node_id=kg_node_id)
    regenerate_entity_page(
        node_id=kg_node_id, vault_path=vault_path, neighborhood=neighborhood,
    )

    result = regenerate_affected_sections(
        entity_node_id=kg_node_id,
        vault_path=vault_path,
        changed_node_ids=changed,
        neighborhood=neighborhood,
    )
    crit = (
        run_consistency_critic(entity_label=label, vault_path=vault_path)
        if run_critic else None
    )
    return {
        "label": label,
        "status": "updated" if result else "incremental_failed_or_noop",
        "changed": len(changed),
        "critic_findings": (crit or {}).get("findings_count"),
    }


def run_nightly_wiki_refresh(
    *,
    vault_path: Optional[Path] = None,
    run_critic: bool = True,
) -> Dict[str, Any]:
    """Scan the vault, refresh pages whose neighborhood nodes changed since
    the page's ``generated_at``, and optionally run the consistency critic."""
    vault_path = vault_path or DEFAULT_VAULT
    pages = _list_prose_pages(vault_path)
    force_by_label = _apply_pre_refresh_revision_log_effects(pages, vault_path)
    summaries: List[Dict[str, Any]] = []
    for p in pages:
        try:
            summaries.append(refresh_one_page(
                label=p["label"],
                page_path=p["path"],
                generated_at=p["generated_at"],
                kg_node_id=p["kg_node_id"],
                vault_path=vault_path,
                run_critic=run_critic,
                force_changed_node_ids=force_by_label.get(p["label"]),
            ))
        except Exception as e:
            logger.error("refresh_one_page failed for %s: %s", p["label"], e)
            summaries.append({
                "label": p["label"],
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })
    counts = {
        "pages_scanned": len(pages),
        "updated": sum(1 for s in summaries if s.get("status") == "updated"),
        "unchanged": sum(1 for s in summaries if s.get("status") == "unchanged"),
        "full_regen": sum(1 for s in summaries if s.get("status") == "full_regen_no_timestamp"),
        "errors": sum(1 for s in summaries if s.get("status") == "error"),
    }
    logger.info("Nightly wiki refresh: %s", counts)
    return {"counts": counts, "pages": summaries}
