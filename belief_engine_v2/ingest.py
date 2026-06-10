"""Stage-2 ingestion (§3a seam, step 2): enriched daily_insights items -> v2 observation log.

Each ActionableItem's structured `extracted_claim` (step 1) becomes ONE observation, resolved to a
canonical belief identity (registry + LLM-verified merge) and folded into the projection. SHADOW: it
writes its OWN db (belief_engine_v2/data/belief_v2_live.db, gitignored) and never touches the old
belief_engine or emi.db — the old engine keeps running while this accumulates a parallel, deduped
belief set for the shadow-run comparison.

Online by design: each new observation resolves against the existing registry with a few candidate
verifications (not a tens-of-thousands batch pass). Items without an extracted_claim are skipped.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

LIVE_DB = "belief_engine_v2/data/belief_v2_live.db"
EXTRACTOR_VERSION = "daily_timeline_insights-v1"
_TAU = 0.86


def _default_embedder():
    from app.assistant.embeddings.embedder import embed_texts
    return lambda t: embed_texts([t])[0]


def open_live_store(
    db_path: str = LIVE_DB,
    *,
    embedder=None,
    verifier=None,
    with_verifier: bool = True,
    tau: float = _TAU,
):
    """Open the shadow store wired with canonical identity (registry + resolver + LLM-verified merge).

    Pass `embedder` / `with_verifier=False` in tests to avoid the app embedder + a real LLM (no
    verifier ⇒ never auto-merges, exact-alias dedup still works — the fail-safe baseline)."""
    from belief_engine_v2.registry import Registry
    from belief_engine_v2.resolver import Resolver
    from belief_engine_v2.store import Store

    store = Store(db_path)
    emb = embedder or _default_embedder()
    if verifier is None and with_verifier:
        from belief_engine_v2.merge_verifier import LLMMergeVerifier
        verifier = LLMMergeVerifier(engine="gpt-5.4-mini")
    reg = Registry(store.conn, embedder=emb, verifier=verifier, threshold=tau, max_candidates=8)
    store.resolver = Resolver(reg)
    return store


def _claim_from_item(item: Dict[str, Any]):
    """Build a v2 Claim from an enriched ActionableItem; return (claim_or_None, polarity)."""
    from belief_engine_v2.store import Claim

    ec = item.get("extracted_claim")
    if not isinstance(ec, dict):
        return None, "affirm"
    polarity = str(ec.get("polarity") or "affirm").strip().lower()
    if polarity not in ("affirm", "contradict", "retract"):
        polarity = "affirm"

    claim = Claim(
        subject=str(ec.get("subject_phrase") or "").strip(),
        predicate=str(ec.get("predicate_phrase") or "").strip(),
        object=str(ec.get("object_phrase") or "").strip(),
        qualifiers={},                       # free-text qualifiers stay OUT of identity (preserved in extra)
        applies_when=None,                   # structured-cue parsing is a follow-up; neutral/soft for now
        statement_nl=str(item.get("fact_summary") or "").strip(),
        raw_text="\n".join(item.get("evidence") or []) or str(item.get("fact_summary") or ""),
        interpretation=str(ec.get("llm_interpretation") or "").strip(),
        extra={
            "tags": item.get("tags") or [],
            "qualifiers": ec.get("qualifiers") or [],
            "applies_when_cue": ec.get("applies_when"),     # keep the string cue for later parsing
            "temporal_scope": item.get("temporal_scope"),
            "change_recommended": item.get("change_recommended"),
        },
    )
    return claim, polarity


def ingest_items(store, items: List[Dict[str, Any]], *, occurred_at: Optional[str] = None,
                 source: str = "daily_insight") -> Dict[str, int]:
    """Append each item's extracted_claim as an observation, then rebuild the projection.
    Returns {appended, skipped}. Items without a usable claim are skipped (never raise)."""
    appended = skipped = 0
    for item in items or []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        claim, polarity = _claim_from_item(item)
        if claim is None or not (claim.subject or claim.predicate or claim.object):
            skipped += 1
            continue
        # The extractor can't supply the observation_id a true `retract` would withdraw, so model an
        # explicit withdrawal as evidence AGAINST the claim (a signed -1 justification), not a no-op.
        if polarity == "retract":
            polarity = "contradict"
        store.append(claim, source=source, polarity=polarity, occurred_at=occurred_at,
                     strength=1.0, extractor_version=EXTRACTOR_VERSION, commit=False)
        appended += 1
    store.conn.commit()
    store.rebuild_projection()
    return {"appended": appended, "skipped": skipped}


def consolidate_live_store(store, *, embedder=None, verifier=None, tau: float = _TAU,
                           max_pairs: int = 500) -> Dict[str, Any]:
    """#6 concept-to-concept consolidation sweep: collapse the RESIDUAL near-duplicate beliefs the
    greedy write-time pass can't ("standing-break nudges" vs "standing break reminders" — minted
    separately, never compared to each other). Embedding proposes candidate belief pairs; the LLM
    verifier decides; a verified merge is a REVERSIBLE redirect (belief_merges). Returns
    {candidates, verified, merges}. Reuses the store's verifier when present."""
    from belief_engine_v2.consolidate import consolidate

    emb = embedder or _default_embedder()
    if verifier is None:
        reg = getattr(store.resolver, "registry", None)
        verifier = getattr(reg, "verifier", None)
        if verifier is None:
            from belief_engine_v2.merge_verifier import LLMMergeVerifier
            verifier = LLMMergeVerifier(engine="gpt-5.4-mini")
    # Structural recall ON: production beliefs carry canonical subject+predicate, so two phrasings of
    # one preference (embedding only ~0.6 apart) are proposed via their shared subject+predicate —
    # the verifier still decides. Without this the sweep misses framing-divergent duplicates.
    return consolidate(store, embedder=emb, verifier=verifier, threshold=tau, max_pairs=max_pairs,
                       pair_same_subject_predicate=True)


def ingest_actionable_information(actionable_information: List[Dict[str, Any]], *,
                                  occurred_at: Optional[str] = None,
                                  db_path: str = LIVE_DB,
                                  consolidate_after: bool = True) -> Dict[str, Any]:
    """Top-level: open the shadow store, ingest a day's enriched items, then run the consolidation
    sweep so the store stays collapsed. Returns the ingest summary (+ consolidation result)."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    store = open_live_store(db_path)
    try:
        summary: Dict[str, Any] = dict(ingest_items(store, actionable_information, occurred_at=occurred_at))
        if consolidate_after:
            # Best-effort: the ingest is already durable, so a sweep failure must not lose it —
            # log loud and carry the ingest result; the next night's sweep retries.
            try:
                summary["consolidation"] = consolidate_live_store(store)
            except Exception as e:
                logger.error("[belief_v2] consolidation sweep failed (ingest kept): %s", e)
                logger.debug("[belief_v2] consolidation exception details", exc_info=True)
                summary["consolidation"] = {"error": str(e)}
        return summary
    finally:
        store.close()
