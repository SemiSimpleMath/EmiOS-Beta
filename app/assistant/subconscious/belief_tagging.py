"""Constrained belief-tag categorizer — the writer for the belief_tags table.

Drives `belief_engine::belief_tagger` over active beliefs in batches and writes
belief_tags. Each belief is tagged with the UNION of:
  - its derivation `domain` (itself a valid vocab tag) — the deterministic baseline, and
  - the LLM's cross-cutting tags, validated against the standardized vocab
    (configs/belief_tags.yaml); off-vocab tags are dropped.
So a belief the LLM drops or leaves empty is still tagged by its domain (pullable
by that domain's consumers), and the LLM only ever ADDS reach across areas.

Used by the one-time backfill and the nightly "tag newly-formed beliefs" pass
(only_untagged=True is a shrinking selector, so the nightly run converges).
"""
from __future__ import annotations

from typing import Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message
from belief_engine_v2 import tags as tagvocab
from belief_engine_v2.ingest import open_live_store

logger = get_logger(__name__)

_AGENT = "belief_engine::belief_tagger"
_BATCH = 15


def _db_path() -> str:
    return str(get_repo_root() / "belief_engine_v2" / "data" / "belief_v2_live.db")


def _vocab_block() -> str:
    return "\n".join(f"- {t}: {d}" for t, d in tagvocab.vocab().items())


def _select(store, only_untagged: bool):
    sql = [
        "SELECT s.short_id AS sid, b.belief_id AS bid, b.statement_nl AS stmt,",
        "       COALESCE(c.domain,'') AS domain",
        " FROM beliefs b",
        " JOIN belief_short_id s ON s.belief_id = b.belief_id",
        " LEFT JOIN belief_category c ON c.belief_id = b.belief_id",
    ]
    where = "b.status='active'"
    if only_untagged:
        sql.append(" LEFT JOIN belief_tags t ON t.belief_id = b.belief_id")
        where += " AND t.belief_id IS NULL"
    sql.append(f" WHERE {where} ORDER BY s.short_id")
    return store.conn.execute("".join(sql)).fetchall()


def tag_beliefs(*, only_untagged: bool = False, limit: Optional[int] = None,
                batch_size: int = _BATCH) -> dict:
    """Tag active beliefs from the vocab (domain baseline UNION LLM enrichment).
    only_untagged=True tags only beliefs with no tags yet (the nightly converging
    pass). Returns a summary dict."""
    store = open_live_store(_db_path(), with_verifier=False)
    try:
        rows = _select(store, only_untagged)
        if limit:
            rows = rows[:limit]
        if not rows:
            return {"tagged": 0, "domain_only": 0, "untagged": 0, "total": 0}

        vocab_block = _vocab_block()
        tagged = domain_only = untagged = 0

        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            ref_bid = {f"b{r['sid']}": r["bid"] for r in batch}
            ref_dom = {f"b{r['sid']}": (r["domain"] or "") for r in batch}
            batch_text = "\n".join(
                f"[b{r['sid']}] (domain: {r['domain'] or 'none'}) {(r['stmt'] or '').strip()}"
                for r in batch
            )

            agent = DI.agent_factory.create_agent(_AGENT)
            if agent is None:
                raise RuntimeError(f"agent {_AGENT} unavailable")
            msg = Message(agent_input={"task": batch_text, "information": vocab_block}, task=batch_text)
            result = agent.action_handler(msg)
            data = getattr(result, "data", None) or {}

            llm_by_ref = {}
            for a in (data.get("assignments") or []):
                ref = str(a.get("id") or "").strip()
                if ref in ref_bid:
                    llm_by_ref[ref] = a.get("tags") or []

            # Every belief in the batch: domain baseline UNION the LLM's tags.
            for ref, bid in ref_bid.items():
                dom = ref_dom.get(ref, "")
                llm_clean = tagvocab.sanitize(llm_by_ref.get(ref, []))
                merged = tagvocab.sanitize(([dom] if dom else []) + llm_clean)
                if not merged:
                    untagged += 1   # no domain AND no valid LLM tag; the nightly pass retries it
                    continue
                tagvocab.set_tags(store.conn, bid, merged, method="categorizer")
                tagged += 1
                if not llm_clean:
                    domain_only += 1

            logger.info("[belief_tagger] batch %d-%d: %d returned by llm, %d total tagged",
                        i, i + len(batch), len(llm_by_ref), tagged)

        return {"tagged": tagged, "domain_only": domain_only, "untagged": untagged, "total": len(rows)}
    finally:
        store.close()
