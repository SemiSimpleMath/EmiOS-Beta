"""v1 belief tagging — the writer for the belief_tags table (emi.db).

Drives `belief_engine::belief_tagger` over active user_beliefs in batches and writes belief_tags.
Each belief is tagged with the UNION of its derivation `domain` (itself a valid vocab tag, except
`general` which is deliberately not a tag) and the LLM's cross-cutting tags, all validated against
the standardized vocab (configs/belief_tags.yaml); off-vocab tags are dropped. So a belief the LLM
leaves empty is still tagged by its domain, and the LLM only ADDS reach across areas.

Tags are a function of the CURRENT statement, so a belief is re-tagged when its statement changes
(its `updated_at` moves past its tags' `assigned_at`). `mode="needs"` selects untagged + stale
beliefs for the nightly pass; `mode="all"` is the one-time backfill.
"""
from __future__ import annotations

import functools
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

import yaml

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message
from belief_engine.db.paths import belief_db_path as _db_path

logger = get_logger(__name__)

_AGENT = "belief_engine::belief_tagger"
_BATCH = 15


@functools.lru_cache(maxsize=1)
def _vocab() -> dict:
    cfg = yaml.safe_load(open(get_repo_root() / "configs" / "belief_tags.yaml", encoding="utf-8")) or {}
    if not isinstance(cfg.get("tags"), dict) or not cfg["tags"]:
        raise ValueError("belief_tags.yaml has no 'tags' map")
    return dict(cfg["tags"])


def sanitize(tags) -> List[str]:
    """Keep only in-vocab tags (dedup, lowercased) — the enforcement point for any writer."""
    v = set(_vocab())
    seen, out = set(), []
    for t in tags or []:
        t = str(t or "").strip().lower()
        if t in v and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def pull_set(name: str) -> List[str]:
    """The tag list a named consumer retrieves (configs/belief_tags.yaml `pull_sets`), vocab-validated.
    A belief surfaces for that consumer if it carries ANY tag in this set."""
    cfg = yaml.safe_load(open(get_repo_root() / "configs" / "belief_tags.yaml", encoding="utf-8")) or {}
    tags = (cfg.get("pull_sets") or {}).get(name)
    if not tags:
        raise KeyError(f"no pull_set '{name}' in belief_tags.yaml")
    return sanitize(tags)


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS belief_tags ("
        " belief_id TEXT NOT NULL, tag TEXT NOT NULL, assigned_at TEXT, method TEXT,"
        " PRIMARY KEY (belief_id, tag))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_belief_tags_tag ON belief_tags(tag)")
    conn.commit()


def set_tags(conn, belief_id: str, tags, *, method: str = "categorizer") -> List[str]:
    """Replace a belief's tags with the sanitized set; returns what was written."""
    clean = sanitize(tags)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM belief_tags WHERE belief_id=?", (belief_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO belief_tags (belief_id, tag, assigned_at, method) VALUES (?,?,?,?)",
        [(belief_id, t, now, method) for t in clean],
    )
    conn.commit()
    return clean


def _select(conn, mode: str):
    base = ("SELECT b.id AS id, b.belief_key AS key, b.statement AS stmt, "
            "COALESCE(b.domain,'') AS domain FROM user_beliefs b WHERE b.status='active'")
    if mode == "needs":
        base += (
            " AND (NOT EXISTS (SELECT 1 FROM belief_tags t WHERE t.belief_id=b.id)"
            " OR EXISTS (SELECT 1 FROM belief_tags t WHERE t.belief_id=b.id AND t.assigned_at < b.updated_at))"
        )
    base += " ORDER BY b.domain, b.belief_key"
    return conn.execute(base).fetchall()


def tag_beliefs(*, mode: str = "all", limit: Optional[int] = None, batch_size: int = _BATCH) -> dict:
    """Tag active beliefs (domain baseline UNION LLM enrichment). mode='all' (backfill) or
    mode='needs' (untagged + stale, for the nightly pass). Returns a summary dict."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn)
        rows = _select(conn, mode)
        if limit:
            rows = rows[:limit]
        if not rows:
            return {"tagged": 0, "domain_only": 0, "untagged": 0, "total": 0}

        vocab_block = "\n".join(f"- {t}: {d}" for t, d in _vocab().items())
        tagged = domain_only = untagged = 0

        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            ref_id = {f"b{k + 1}": r["id"] for k, r in enumerate(batch)}
            ref_dom = {f"b{k + 1}": r["domain"] for k, r in enumerate(batch)}
            batch_text = "\n".join(
                f"[b{k + 1}] (domain: {r['domain'] or 'none'}) {(r['stmt'] or '').strip()}"
                for k, r in enumerate(batch)
            )
            agent = DI.agent_factory.create_agent(_AGENT)
            if agent is None:
                raise RuntimeError(f"agent {_AGENT} unavailable")
            result = agent.action_handler(
                Message(agent_input={"task": batch_text, "information": vocab_block}, task=batch_text))
            data = getattr(result, "data", None) or {}

            llm = {}
            for a in (data.get("assignments") or []):
                ref = str(a.get("id") or "").strip()
                if ref in ref_id:
                    llm[ref] = a.get("tags") or []

            for ref, bid in ref_id.items():
                dom = ref_dom.get(ref, "")
                llm_clean = sanitize(llm.get(ref, []))
                merged = sanitize(([dom] if dom else []) + llm_clean)
                if not merged:
                    untagged += 1   # general-domain belief the LLM left empty; the nightly pass retries
                    continue
                set_tags(conn, bid, merged)
                tagged += 1
                if not llm_clean:
                    domain_only += 1
            logger.info("[v1 belief_tagger] %d/%d tagged", tagged, len(rows))

        return {"tagged": tagged, "domain_only": domain_only, "untagged": untagged, "total": len(rows)}
    finally:
        conn.close()
