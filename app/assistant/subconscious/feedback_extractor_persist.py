"""Persist feedback_extractor output into the belief catalog + mark comments processed.

Since the 2026-09-29 cutover the catalog is the belief intake's store (belief_intake_*). Each
extraction becomes one intake atom whose source is the user's own comment (kind "said",
source_ref "pod:<comment id>"), and is judged by the same steps the nightly intake uses — the
store's dedup decides which held belief it is, never the extractor's belief_key:

- confirms / qualifies: dedup → apply → (on contradicts) revise → contradiction fan-out, exactly
  as a day's atom. A new claim becomes a new belief.
- contradicts / rejects: the extractor states the belief the user pushed back on. It attaches as
  contradicting evidence only when dedup finds that belief held ("same"). Any other verdict is
  skipped, never minted: contradicting a belief nobody holds would create an affirmative belief
  carrying only negative evidence (the 2026-06 zucchini phantoms). The paired confirms extraction
  carries the user's actual claim through the revise path.

Then per source-comment pod: set metadata.processed_at_utc + metadata.extracted_belief_ids and
flip the 'unprocessed' tag, so the next run skips it. A comment whose extraction failed is left
unprocessed (ERROR logged) so the next run retries it.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# The extractor's scope → the intake's kind. Its statements are household preferences
# ("what the household prefers/wants"); `temporary` is its "this week, this trip".
_KIND_FOR_SCOPE = {"chronic": "stable_preference", "temporary": "episodic_context"}
_CONTRA = {"contradicts", "rejects"}


def _comment_source(pod, relation: str) -> tuple[str, dict]:
    """(evidence day, source) for a comment pod: the user's words, dated when they were written."""
    from app.assistant.utils.time_utils import get_local_time, utc_to_local
    from belief_engine.intake.redact import redact
    meta = pod.metadata or {}
    submitted = meta.get("submitted_at_utc")
    local = utc_to_local(submitted) if submitted else get_local_time()
    return local.date().isoformat(), {
        "time": local.strftime("%Y-%m-%d %H:%M"), "kind": "said", "relation": relation,
        "text": redact(str(meta.get("text") or "")), "source_ref": f"pod:{pod.pod_id}",
    }


def apply_feedback_extractor_output(output: Dict[str, Any], *, intake_store=None, pod_store=None,
                                    embed_texts=None, scope_ctx=None) -> Dict[str, Any]:
    """Judge the extractor's belief updates into the catalog + mark source comments processed.

    Returns a summary with counts and per-extraction belief ids. The keyword arguments are
    injectable for tests; production resolves the app store, pod store, embedder and scope."""
    from belief_engine.intake import agents
    from belief_engine.intake.rank import ordered
    from belief_engine.intake.run import app_store, judge_atom

    extractions = output.get("extractions") or []
    skipped = output.get("skipped") or []
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    store = PodStore() if pod_store is None else pod_store
    intake = app_store() if intake_store is None else intake_store
    if embed_texts is None:
        from app.assistant.embeddings.embedder import embed_texts
    scope_ctx = agents.scope() if scope_ctx is None else scope_ctx

    upserted: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    phantom_skipped: List[Dict[str, Any]] = []
    ids_by_comment: Dict[str, List[str]] = defaultdict(list)
    failed_comments: set = set()

    for ext in extractions:
        cid = str((ext or {}).get("source_comment_pod_id") or "")
        signal = str(ext.get("signal_type") or "confirms")
        try:
            pod = store.get(cid) if cid else None
            if pod is None:
                raise ValueError(f"source comment pod {cid!r} not found")
            relation = "contradict" if signal in _CONTRA else "support"
            day, source = _comment_source(pod, relation)
            atom = {"statement": str(ext.get("statement") or "").strip(),
                    "kind": _KIND_FOR_SCOPE[str(ext.get("scope") or "chronic")],
                    "scope": str(ext.get("scope") or "chronic"), "sources": [source]}
            if not atom["statement"]:
                raise ValueError("extraction has no statement")
            vec = embed_texts([atom["statement"]])[0]
            if signal in _CONTRA:
                verdict = agents.dedup({**atom, "sources": [{**source, "relation": "support"}]}, day,
                                       ordered(intake.beliefs(), vec), scope_ctx)
                if verdict["verdict"] != "same":
                    phantom_skipped.append({"statement": atom["statement"], "source_comment_pod_id": cid,
                                            "verdict": verdict["verdict"],
                                            "reason": "pushback on a belief not held — not minting a phantom"})
                    continue
                bid = intake.apply(day, atom, vec, verdict)
            else:
                bid = judge_atom(day, atom, vec, intake, scope_ctx, embed_texts,
                                 log=lambda line: logger.info("[feedback_persist]%s", line))
            ids_by_comment[cid].append(bid)
            upserted.append({"belief_id": bid, "signal_type": signal, "comment_pod_id": cid})
        except Exception as e:
            logger.error("[feedback_persist] extraction failed; comment %s stays unprocessed and is "
                         "retried next run: %s — extraction: %r", cid, e, ext, exc_info=True)
            failed.append({"extraction": ext, "error": str(e)})
            failed_comments.add(cid)

    # Mark each processed comment pod. A failed mark is COUNTED and logged at ERROR: the comment
    # stays tagged 'unprocessed', so the next run re-drains it and its evidence double-counts.
    comment_pods_marked: List[str] = []
    mark_failed = 0
    done = {c for c in {str(e.get("source_comment_pod_id") or "") for e in extractions} if c} - failed_comments
    for comment_pod_id in sorted(done):
        try:
            pod = store.get(comment_pod_id)
            meta = dict(pod.metadata or {})
            meta["processed_at_utc"] = now_utc_iso
            meta["extracted_belief_ids"] = list({*(meta.get("extracted_belief_ids") or []),
                                                 *ids_by_comment.get(comment_pod_id, [])})
            pod.metadata = meta
            tags = [t for t in (pod.tags or []) if t != "unprocessed"]
            if "processed" not in tags:
                tags.append("processed")
            pod.tags = tags
            store.put(pod)
            comment_pods_marked.append(comment_pod_id)
        except Exception:
            mark_failed += 1
            logger.exception(
                "[feedback_persist] failed to mark comment %s processed — it will "
                "be RE-DRAINED next run and its evidence double-counted", comment_pod_id,
            )

    # Also mark skipped comments processed (with 0 extracted_belief_ids)
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
        except Exception:
            mark_failed += 1
            logger.exception("[feedback_persist] failed to mark skipped comment %s", cid)

    return {
        "extractions_count": len(extractions),
        "skipped_count": len(skipped),
        "upserted_count": len(upserted),
        "upsert_failed_count": len(failed),
        "phantom_skipped_count": len(phantom_skipped),
        "comments_marked_processed": len(comment_pods_marked),
        "comments_mark_failed": mark_failed,
        "upserted": upserted,
        "phantom_skipped": phantom_skipped,
        "failed": failed,
    }
