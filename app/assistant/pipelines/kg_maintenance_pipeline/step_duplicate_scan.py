"""
Step: duplicate_scan

Finds duplicate KG nodes using three-tier candidate generation, then confirms
candidates with the LLM duplicate detector agent.

Algorithm
---------
Phase 1 — Read (short-lived session)
    Load every node's id, label, description, node_type, aliases, category,
    and its neighborhood (connected node labels + relationship types + edge
    sentences).  Close session before touching ChromaDB or the LLM.

Phase 2 — Three-tier candidate generation (no DB, no LLM)
    Tier 1: Exact alias overlap — if node A's label or any alias exactly
            matches node B's label or any alias.  Zero cost, pure Python.
    Tier 2: Label containment — if one label is a substring of the other
            AND they share node_type.  Also cheap.
    Tier 3: Embedding similarity — cosine similarity from ChromaDB
            embeddings above SIMILARITY_THRESHOLD.  Catches semantic
            dupes that string matching misses.

    De-duplicate pairs across tiers to form unique candidate clusters.

Phase 3 — LLM confirmation (no session open)
    Send each candidate pair's rich descriptors (including neighborhood)
    to kg_maintenance::duplicate_detector.  The agent returns explicit
    merge_actions with node ID pairs.

Phase 4 — Write findings (one short-lived session per finding via store)
    For each confirmed pair call upsert_finding() which self-manages its session.

Session / LLM contract
-----------------------
NO session is open during any LLM call.  Each session is opened, used for one
operation, committed/closed before the next step.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

SIMILARITY_THRESHOLD = 0.88

MAX_CLUSTER_SIZE = 8

MAX_PAIRS_PER_RUN = 120

MAX_EDGE_SENTENCES = 5

MAX_NEIGHBORS = 10


# ── Phase 1: read node descriptors ────────────────────────────────────────────

def _load_node_descriptors() -> dict[str, dict[str, Any]]:
    """
    Returns {node_id: descriptor_dict} for all nodes.
    Includes aliases, category, and neighborhood information.
    Session is opened and closed entirely within this function.
    """
    from sqlalchemy import func as sqlfunc
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    session = get_session()
    try:
        nodes = session.query(
            Node.id, Node.label, Node.description, Node.node_type,
            Node.aliases, Node.category, Node.semantic_label,
        ).all()

        edges = (
            session.query(
                Edge.source_id, Edge.target_id,
                Edge.sentence, Edge.relationship_type,
            )
            .all()
        )

        outgoing = session.query(Edge.source_id, sqlfunc.count(Edge.id)).group_by(Edge.source_id).all()
        incoming = session.query(Edge.target_id, sqlfunc.count(Edge.id)).group_by(Edge.target_id).all()

        node_label_index: dict[str, str] = {}
        for r in nodes:
            node_label_index[str(r.id)] = r.label or ""
    finally:
        session.close()

    edge_count_index: dict[str, int] = {}
    for nid, cnt in outgoing:
        edge_count_index[str(nid)] = edge_count_index.get(str(nid), 0) + cnt
    for nid, cnt in incoming:
        edge_count_index[str(nid)] = edge_count_index.get(str(nid), 0) + cnt

    sentence_index: dict[str, list[str]] = {}
    neighbor_index: dict[str, list[dict[str, str]]] = {}

    for src, tgt, sent, rel_type in edges:
        src_s, tgt_s = str(src), str(tgt)

        if sent and sent.strip():
            for nid in (src_s, tgt_s):
                sentence_index.setdefault(nid, [])
                if len(sentence_index[nid]) < MAX_EDGE_SENTENCES:
                    sentence_index[nid].append(sent.strip())

        neighbor_index.setdefault(src_s, [])
        if len(neighbor_index[src_s]) < MAX_NEIGHBORS:
            neighbor_index[src_s].append({
                "node_label": node_label_index.get(tgt_s, "?"),
                "relationship": rel_type or "related_to",
                "direction": "outgoing",
            })

        neighbor_index.setdefault(tgt_s, [])
        if len(neighbor_index[tgt_s]) < MAX_NEIGHBORS:
            neighbor_index[tgt_s].append({
                "node_label": node_label_index.get(src_s, "?"),
                "relationship": rel_type or "related_to",
                "direction": "incoming",
            })

    descriptors = {}
    for r in nodes:
        nid = str(r.id)
        raw_aliases = r.aliases or []
        if isinstance(raw_aliases, str):
            try:
                raw_aliases = json.loads(raw_aliases)
            except (json.JSONDecodeError, TypeError):
                raw_aliases = []

        descriptors[nid] = {
            "node_id": nid,
            "label": r.label or "",
            "description": r.description or "",
            "node_type": r.node_type or "",
            "aliases": raw_aliases,
            "category": r.category or "",
            "semantic_label": r.semantic_label or "",
            "edge_sentences": sentence_index.get(nid, []),
            "edge_count": edge_count_index.get(nid, 0),
            "neighborhood": neighbor_index.get(nid, []),
        }

    logger.info("[duplicate_scan] Loaded %d node descriptors", len(descriptors))
    return descriptors


# ── Phase 2: three-tier candidate generation ──────────────────────────────────

def _build_candidate_pairs(
    descriptors: dict[str, dict],
) -> list[tuple[str, str, str]]:
    """
    Generate candidate duplicate pairs using three tiers.
    Returns list of (node_id_a, node_id_b, tier_source) tuples, deduplicated.
    Pairs are sorted by combined edge count (most connected first) so the
    most important nodes are reviewed within the per-run budget.
    """
    from app.assistant.kg_maintenance.verdict_store import load_distinct_pairs

    # Bulk-fetch all active 'distinct' verdicts once, locally canonicalized
    # by the store as (a, b) with a < b. Hot path: every Tier 1/2/3 pair
    # checks membership in this set instead of opening a read session.
    distinct_pairs = load_distinct_pairs()

    seen_pairs: set[tuple[str, str]] = set()
    candidates: list[tuple[str, str, str]] = []
    cross_type_skipped = 0
    prior_verdict_skipped = 0

    def _add_pair(a: str, b: str, tier: str) -> None:
        nonlocal cross_type_skipped, prior_verdict_skipped
        key = (min(a, b), max(a, b))
        if key in seen_pairs:
            return
        # Cross-type duplicate proposals are nearly always wrong: an
        # Entity (e.g. a recurring social-event entity with hundreds
        # of edges) and an Event (one specific occurrence) share a
        # label but are categorically distinct things. Merging them
        # would corrupt the graph. Tier 2 already gates on same-type
        # by construction; this catches the cross-type leaks from
        # Tier 1 (alias overlap) and Tier 3 (embedding similarity).
        type_a = (descriptors.get(a, {}).get("node_type") or "").strip()
        type_b = (descriptors.get(b, {}).get("node_type") or "").strip()
        if type_a and type_b and type_a != type_b:
            cross_type_skipped += 1
            return
        # Prior-verdict short-circuit: if the investigator has already
        # decided these two are distinct, skip the LLM call. The
        # finding's verdict prose is durable; re-asking is waste.
        if key in distinct_pairs:
            prior_verdict_skipped += 1
            return
        seen_pairs.add(key)
        candidates.append((a, b, tier))

    all_ids = list(descriptors.keys())

    # --- Tier 1: Exact alias/label overlap ---
    label_to_ids: dict[str, list[str]] = {}
    for nid, d in descriptors.items():
        names = {d["label"].strip().lower()} if d["label"] else set()
        for alias in d["aliases"]:
            if alias and alias.strip():
                names.add(alias.strip().lower())
        for name in names:
            label_to_ids.setdefault(name, []).append(nid)

    for name, nids in label_to_ids.items():
        if len(nids) < 2:
            continue
        for i in range(len(nids)):
            for j in range(i + 1, len(nids)):
                _add_pair(nids[i], nids[j], "alias_overlap")

    logger.info("[duplicate_scan] Tier 1 (alias overlap): %d pairs", len(candidates))
    tier1_count = len(candidates)

    # --- Tier 2: Label containment (same node_type) ---
    # The shorter label must be >= 60% of the longer label's length to avoid
    # noisy matches like "Jukka" contained in "Jukka's CPAP machine".
    # This catches "Jukka" / "Jukka Virtanen" (5/15=33% — too low) but that's
    # already covered by Tier 1 alias overlap.  Tier 2 is for cases like
    # "Morning routine" / "Morning routine (weekday)" where aliases don't help.
    CONTAINMENT_MIN_RATIO = 0.60
    by_type: dict[str, list[tuple[str, str]]] = {}
    for nid, d in descriptors.items():
        if d["label"] and len(d["label"].strip()) >= 3:
            nt = d["node_type"]
            by_type.setdefault(nt, []).append((nid, d["label"].strip().lower()))

    for nt, entries in by_type.items():
        entries.sort(key=lambda x: len(x[1]))
        for i in range(len(entries)):
            nid_i, label_i = entries[i]
            for j in range(i + 1, len(entries)):
                nid_j, label_j = entries[j]
                if label_i == label_j:
                    continue
                if label_i in label_j and len(label_i) / len(label_j) >= CONTAINMENT_MIN_RATIO:
                    _add_pair(nid_i, nid_j, "label_containment")

    logger.info(
        "[duplicate_scan] Tier 2 (label containment): %d new pairs",
        len(candidates) - tier1_count,
    )
    tier2_count = len(candidates)

    # --- Tier 3: Embedding similarity ---
    embedding_pairs = _embedding_similarity_pairs(all_ids)
    for a, b in embedding_pairs:
        _add_pair(a, b, "embedding_similarity")

    logger.info(
        "[duplicate_scan] Tier 3 (embedding similarity): %d new pairs",
        len(candidates) - tier2_count,
    )
    logger.info(
        "[duplicate_scan] Total unique candidate pairs: %d "
        "(cross_type_skipped=%d, prior_verdict_skipped=%d)",
        len(candidates), cross_type_skipped, prior_verdict_skipped,
    )

    # Prioritize pairs involving the most-connected nodes
    def _pair_priority(pair: tuple[str, str, str]) -> float:
        a, b, _tier = pair
        ec_a = descriptors.get(a, {}).get("edge_count", 0)
        ec_b = descriptors.get(b, {}).get("edge_count", 0)
        return ec_a + ec_b

    candidates.sort(key=_pair_priority, reverse=True)
    return candidates[:MAX_PAIRS_PER_RUN]


def _embedding_similarity_pairs(all_node_ids: list[str]) -> list[tuple[str, str]]:
    """
    Fetch embeddings from ChromaDB and find pairs above SIMILARITY_THRESHOLD.
    Prefers context embeddings (original_sentence) over label-only embeddings.
    Falls back to label embeddings for nodes without context embeddings.
    """
    from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager

    chroma = get_chroma_manager()
    if not all_node_ids:
        return []

    BATCH = 5000
    ids_with_embs: list[str] = []
    emb_matrix: list[list[float]] = []

    # First try context embeddings (richer signal)
    context_ids: set[str] = set()
    for start in range(0, len(all_node_ids), BATCH):
        batch_ids = all_node_ids[start: start + BATCH]
        result = chroma.node_context_collection.get(
            ids=batch_ids, include=["embeddings"]
        )
        raw_ids = result.get("ids") or []
        raw_embs = result.get("embeddings")
        if raw_embs is None:
            raw_embs = []
        for nid, emb in zip(raw_ids, raw_embs):
            if emb is not None:
                ids_with_embs.append(nid)
                emb_matrix.append(emb)
                context_ids.add(nid)

    # Fall back to label embeddings for nodes without context embeddings
    missing_ids = [nid for nid in all_node_ids if nid not in context_ids]
    if missing_ids:
        for start in range(0, len(missing_ids), BATCH):
            batch_ids = missing_ids[start: start + BATCH]
            result = chroma.node_collection.get(
                ids=batch_ids, include=["embeddings"]
            )
            raw_ids = result.get("ids") or []
            raw_embs = result.get("embeddings")
            if raw_embs is None:
                raw_embs = []
            for nid, emb in zip(raw_ids, raw_embs):
                if emb is not None:
                    ids_with_embs.append(nid)
                    emb_matrix.append(emb)

    if len(ids_with_embs) < 2:
        return []

    logger.info(
        "[duplicate_scan] Embeddings: %d context + %d label-only / %d nodes total",
        len(context_ids), len(ids_with_embs) - len(context_ids), len(all_node_ids),
    )

    mat = np.array(emb_matrix, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    mat = mat / norms

    pairs: list[tuple[str, str]] = []
    n = len(ids_with_embs)
    for i in range(n):
        sims = mat[i] @ mat[i + 1:].T
        for offset, sim in enumerate(sims):
            if sim >= SIMILARITY_THRESHOLD:
                pairs.append((ids_with_embs[i], ids_with_embs[i + 1 + offset]))
    return pairs


# ── Phase 3: LLM confirmation ─────────────────────────────────────────────────

def _confirm_pairs_with_llm(
    pairs: list[tuple[str, str, str]],
    descriptors: dict[str, dict],
    scope_context,
) -> list[dict]:
    """
    Calls kg_maintenance::duplicate_detector for each candidate pair.
    Sends rich context including aliases, category, and neighborhood.
    Returns a flat list of merge_action dicts.

    No DB session is open during this function.
    """
    agent = DI.agent_factory.create_agent("kg_maintenance::duplicate_detector")
    merge_actions: list[dict] = []

    for idx, (nid_a, nid_b, tier) in enumerate(pairs):
        desc_a = descriptors.get(nid_a)
        desc_b = descriptors.get(nid_b)
        if not desc_a or not desc_b:
            continue

        node_data = []
        for d in (desc_a, desc_b):
            sentences = d["edge_sentences"]
            context_parts = [d["description"]] + sentences
            if d["aliases"]:
                context_parts.append(f"aliases: {', '.join(d['aliases'])}")
            if d["category"]:
                context_parts.append(f"category: {d['category']}")
            if d["semantic_label"]:
                context_parts.append(f"semantic_label: {d['semantic_label']}")
            context = " | ".join(filter(None, context_parts))

            neighborhood_sample = []
            for neighbor in d.get("neighborhood", []):
                neighborhood_sample.append(
                    f"{neighbor['direction']}: {neighbor['relationship']} → {neighbor['node_label']}"
                )

            node_data.append({
                "node_id": d["node_id"],
                "label": d["label"],
                "node_type": d["node_type"],
                "context": context,
                "edge_count": d["edge_count"],
                "neighborhood_sample": neighborhood_sample,
            })

        try:
            response = agent.action_handler(
                Message(
                    agent_input={
                        "duplicate_group_data": json.dumps(node_data, ensure_ascii=False),
                        "analysis_context": json.dumps({
                            "pair_index": idx + 1,
                            "total_pairs": len(pairs),
                            "detection_tier": tier,
                        }),
                    },
                    scope_context=scope_context,
                )
            )
            if response and response.data:
                actions = response.data.get("merge_actions") or []
                merge_actions.extend(actions)
                logger.info(
                    "[duplicate_scan] Pair %d/%d '%s' vs '%s' [%s] → %d merge actions",
                    idx + 1, len(pairs),
                    desc_a["label"][:30], desc_b["label"][:30],
                    tier, len(actions),
                )
        except Exception:
            logger.debug(
                "[duplicate_scan] LLM call failed for pair %d — continuing",
                idx, exc_info=True,
            )

    return merge_actions


# ── Phase 4: write findings ───────────────────────────────────────────────────

def _write_findings(merge_actions: list[dict], pipeline_run_id: str) -> int:
    """
    Converts LLM merge_actions into kg_maintenance_finding rows.
    Each action that covers N nodes becomes N-1 pairwise findings
    (primary=most-connected by agent ordering, secondary=each other node).
    Returns count of new findings created.
    """
    new_findings = 0
    for action in merge_actions:
        node_ids = action.get("merge") or []
        labels = action.get("labels") or []
        reason = action.get("reason") or "LLM duplicate detector flagged these as likely duplicates."

        if len(node_ids) < 2:
            continue

        primary = node_ids[0]
        for secondary in node_ids[1:]:
            _, created = upsert_finding(
                finding_type="duplicate_node",
                primary_node_id=primary,
                secondary_node_id=secondary,
                suggested_action="merge",
                reason=reason,
                confidence=0.80,
                priority="medium",
                agent_name="kg_maintenance::duplicate_detector",
                evidence={"labels": labels, "full_group": node_ids},
                pipeline_run_id=pipeline_run_id,
            )
            if created:
                new_findings += 1

    return new_findings


# ── Entry point ───────────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> dict:
    """
    Returns {"nodes_loaded": int, "candidate_pairs": int, "llm_confirmed_actions": int, "new_findings": int}.
    """
    logger.info("[duplicate_scan] Starting run_id=%s", ctx.run_id)

    scope_context = build_pipeline_scope_context(
        pipeline_id=ctx.pipeline_id,
        actor_id="kg_maintenance_runner",
    )

    # Phase 1 — read (session opens and closes inside)
    descriptors = _load_node_descriptors()
    if not descriptors:
        logger.info("[duplicate_scan] No nodes in graph — skipping")
        return {"nodes_loaded": 0, "candidate_pairs": 0, "llm_confirmed_actions": 0, "new_findings": 0}

    # Phase 2 — three-tier candidate generation (no DB, no LLM)
    pairs = _build_candidate_pairs(descriptors)
    if not pairs:
        logger.info("[duplicate_scan] No candidate pairs found")
        return {"nodes_loaded": len(descriptors), "candidate_pairs": 0, "llm_confirmed_actions": 0, "new_findings": 0}

    # Phase 3 — LLM confirmation (no session open)
    merge_actions = _confirm_pairs_with_llm(pairs, descriptors, scope_context)

    # Phase 4 — write findings (store manages its own sessions)
    new_findings = _write_findings(merge_actions, pipeline_run_id=ctx.run_id)

    result = {
        "nodes_loaded": len(descriptors),
        "candidate_pairs": len(pairs),
        "llm_confirmed_actions": len(merge_actions),
        "new_findings": new_findings,
    }
    logger.info("[duplicate_scan] Done: %s", result)
    return result
