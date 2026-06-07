"""Persist feedback_extractor output to the belief store + mark comments processed.

For each BeliefExtraction in the agent's output:
1. Build a BeliefUpsertRequest + an EvidenceInput pointing at the
   source comment pod
2. Call BeliefStore.upsert_belief(req, [evidence])
3. Track the belief_id for the comment's processed-marker

Then per source-comment pod:
4. Read the pod, set metadata.processed_at_utc + metadata.extracted_belief_ids
5. Write the pod back so the next extractor run skips it

This is the final piece that closes the feedback loop: comment →
extractor → belief → (next-day proposer reads belief snapshot → behavior shifts).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_feedback_extractor_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the extractor's belief updates + mark source comments processed.

    Returns a summary with counts and per-extraction belief_ids for
    smoke-test visibility."""
    extractions = output.get("extractions") or []
    skipped = output.get("skipped") or []
    now_utc_iso = datetime.now(timezone.utc).isoformat()

    # Group extractions by source comment so we can mark each comment
    # processed with all the belief_ids it contributed to.
    by_comment: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ext in extractions:
        cid = (ext or {}).get("source_comment_pod_id") or ""
        if cid:
            by_comment[cid].append(ext)

    upserted: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    phantom_skipped: List[Dict[str, Any]] = []

    # 1. Upsert beliefs into the belief store
    store_unavailable = False
    BeliefStore = None
    BeliefUpsertRequest = None
    EvidenceInput = None
    try:
        from belief_engine.store.belief_store import (
            BeliefStore as _BeliefStore,
            BeliefUpsertRequest as _BeliefUpsertRequest,
            EvidenceInput as _EvidenceInput,
        )
        BeliefStore = _BeliefStore
        BeliefUpsertRequest = _BeliefUpsertRequest
        EvidenceInput = _EvidenceInput
    except Exception as e:
        logger.error("[feedback_persist] belief_engine.store.belief_store import failed: %s", e)
        store_unavailable = True

    belief_store = None
    if not store_unavailable:
        try:
            belief_store = BeliefStore()
        except Exception as e:
            logger.error("[feedback_persist] BeliefStore() init failed: %s", e)
            store_unavailable = True

    for ext in extractions:
        try:
            if store_unavailable or belief_store is None:
                failed.append({"extraction": ext, "error": "belief_store_unavailable"})
                continue
            belief_key = str(ext.get("belief_key") or "").strip()
            signal_type = str(ext.get("signal_type") or "confirms")
            evidence = EvidenceInput(
                source_type="user_comment",
                source_date=ext.get("source_comment_submitted_at"),  # may be None — see below
                source_ref=str(ext.get("source_comment_pod_id") or ""),
                signal_type=signal_type,
                summary=str(ext.get("reasoning") or "")[:500],
                raw_text=None,
                weight=_weight_for_confidence(str(ext.get("confidence") or "medium")),
                valence=None,  # derive from signal_type
                extracted_by="feedback_extractor",
            )
            if signal_type == "contradicts":
                # A contradiction must only WEAKEN a belief that already exists — never
                # mint a new affirmative one. Otherwise "kids don't like zucchini"
                # fabricates a phantom "kids will eat zucchini" carrying only negative
                # evidence (the paired 'confirms' extraction already records the real
                # signal). See scratch/MEAL-PLANNING-AUDIT.md.
                record = belief_store.add_evidence_to_existing(belief_key, [evidence])
                if record is None:
                    phantom_skipped.append({
                        "belief_key": belief_key,
                        "source_comment_pod_id": ext.get("source_comment_pod_id"),
                        "reason": "contradicts with no existing active belief — not minting a phantom",
                    })
                    continue
            else:
                req = BeliefUpsertRequest(
                    domain=str(ext.get("domain") or "other"),
                    belief_key=belief_key,
                    statement=str(ext.get("statement") or "").strip(),
                    confidence=str(ext.get("confidence") or "medium"),
                    scope=str(ext.get("scope") or "chronic"),
                    status="active",
                    conditions=None,
                    first_observed=None,  # store fills in if new
                    last_confirmed=now_utc_iso,
                    kind=None,  # let heuristic classifier decide
                )
                record = belief_store.upsert_belief(req, evidence=[evidence])
            upserted.append({
                "belief_id": record.id,
                "belief_key": record.belief_key,
                "signal_type": signal_type,
                "comment_pod_id": belief_key,  # for logging (re-use key)
            })
        except Exception as e:
            logger.warning("[feedback_persist] belief upsert failed: %s — extraction: %r", e, ext)
            failed.append({"extraction": ext, "error": str(e)[:240]})

    # 2. Mark each processed comment pod's metadata
    store = PodStore()
    comment_pods_marked: List[str] = []
    for comment_pod_id, exts in by_comment.items():
        try:
            pod = store.get(comment_pod_id)
            if pod is None:
                logger.warning("[feedback_persist] comment pod %s not found — skip mark", comment_pod_id)
                continue
            meta = dict(pod.metadata or {})
            meta["processed_at_utc"] = now_utc_iso
            existing_ids = list(meta.get("extracted_belief_ids") or [])
            # Append all belief_ids that came from upserts referencing this comment
            new_ids = [
                u["belief_id"] for u in upserted
                if any(ext.get("source_comment_pod_id") == comment_pod_id for ext in exts)
            ]
            meta["extracted_belief_ids"] = list({*existing_ids, *new_ids})
            pod.metadata = meta
            # Drop the "unprocessed" tag since this comment is no longer queued
            tags = [t for t in (pod.tags or []) if t != "unprocessed"]
            if "processed" not in tags:
                tags.append("processed")
            pod.tags = tags
            store.put(pod)
            comment_pods_marked.append(comment_pod_id)
        except Exception as e:
            logger.warning("[feedback_persist] failed to mark comment %s processed: %s", comment_pod_id, e)

    # 3. Also mark skipped comments processed (with 0 extracted_belief_ids)
    for s in skipped:
        cid = s.get("comment_pod_id") if isinstance(s, dict) else None
        if not cid:
            continue
        try:
            pod = store.get(cid)
            if pod is None:
                continue
            meta = dict(pod.metadata or {})
            if meta.get("processed_at_utc"):
                continue
            meta["processed_at_utc"] = now_utc_iso
            meta["extracted_belief_ids"] = list(meta.get("extracted_belief_ids") or [])
            meta["skip_reason"] = (s.get("reason") or "")[:300]
            pod.metadata = meta
            tags = [t for t in (pod.tags or []) if t != "unprocessed"]
            if "processed" not in tags:
                tags.append("processed")
            if "skipped" not in tags:
                tags.append("skipped")
            pod.tags = tags
            store.put(pod)
            comment_pods_marked.append(cid)
        except Exception as e:
            logger.warning("[feedback_persist] failed to mark skipped comment %s: %s", cid, e)

    return {
        "extractions_count": len(extractions),
        "skipped_count": len(skipped),
        "upserted_count": len(upserted),
        "upsert_failed_count": len(failed),
        "phantom_skipped_count": len(phantom_skipped),
        "comments_marked_processed": len(comment_pods_marked),
        "upserted": upserted,
        "phantom_skipped": phantom_skipped,
        "failed": failed[:10],  # cap for readability
    }


def _weight_for_confidence(confidence: str) -> float:
    """Map qualitative confidence to a numeric evidence weight.

    The belief store's decay v2 reweights by evidence; this gives the
    extractor a way to register "this comment is strong signal" vs
    "this is light signal" without inventing a separate field."""
    c = (confidence or "").lower()
    if c == "high":
        return 1.0
    if c == "medium":
        return 0.6
    if c == "low":
        return 0.3
    return 0.5
