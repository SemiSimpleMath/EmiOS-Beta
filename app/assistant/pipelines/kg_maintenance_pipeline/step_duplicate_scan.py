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
from sqlalchemy import text

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

MAX_PAIRS_PER_RUN = 30000  # one-time catch-up cap; drop back to 120 after the backlog clears

MAX_EDGE_SENTENCES = 5

MAX_NEIGHBORS = 10

# Tier 4 (anchor-propagation) tunables.
ANCHOR_MIN_EDGE_COUNT = 5  # only Entity anchors with >=N edges can drive a candidate
TIER4_MAX_PAIRS = 80       # safety cap on pairs Tier 4 emits per run


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
            Node.original_sentence,
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
            "original_sentence": (r.original_sentence or ""),
            "edge_sentences": sentence_index.get(nid, []),
            "edge_count": edge_count_index.get(nid, 0),
            "neighborhood": neighbor_index.get(nid, []),
        }

    logger.info("[duplicate_scan] Loaded %d node descriptors", len(descriptors))
    return descriptors


def _load_anchor_propagation_data(descriptors: dict[str, dict]) -> dict[str, Any]:
    """Build the adjacency indices Tier 4 needs.

    For each Entity anchor (node_type='Entity' with >= ANCHOR_MIN_EDGE_COUNT
    edges), collect: every 1-hop State/Event/Goal node it connects to,
    that state node's "other side" Entity endpoints, plus the edge sentences
    on both legs.

    The structural signal Tier 4 uses: anchor A has multiple state nodes
    sharing (predicate, state_label, state_category); each state node has
    its own "other Entity" endpoint; those other Entities become candidate
    pair members ("A relates to both X and Y via the same predicate path —
    maybe X and Y are the same referent").

    Returns:
      {
        "anchor_to_states": {anchor_id: [state_record, ...]},
        "state_to_others": {state_id: [other_record, ...]},
      }
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    session = get_session()
    try:
        rows = session.execute(
            text(
                "SELECT e.source_id, e.target_id, e.relationship_type, e.sentence, "
                "       sn.node_type, sn.label, "
                "       tn.node_type, tn.label, tn.category, tn.start_date "
                "FROM kg_edge_metadata e "
                "JOIN kg_node_metadata sn ON sn.id = e.source_id "
                "JOIN kg_node_metadata tn ON tn.id = e.target_id "
            )
        ).fetchall()
    finally:
        session.close()

    from collections import defaultdict
    anchor_to_states: dict[str, list[dict]] = defaultdict(list)
    state_to_others: dict[str, list[dict]] = defaultdict(list)

    for r in rows:
        s_id, t_id, rel, sent = str(r[0]), str(r[1]), r[2], r[3]
        s_type, s_lbl = r[4] or "", r[5] or ""
        t_type, t_lbl, t_cat, t_start = r[6] or "", r[7] or "", r[8] or "", r[9]

        # Entity → State/Event/Goal direction (Entity is potential anchor)
        if s_type == "Entity" and t_type in ("State", "Event", "Goal"):
            anchor_to_states[s_id].append({
                "predicate": rel or "related_to",
                "direction": "outgoing",
                "state_id": t_id, "state_label": t_lbl,
                "state_category": (t_cat or "").strip().lower(),
                "state_start_date": str(t_start)[:10] if t_start else None,
                "edge_sentence": sent or "",
            })
        # State/Event/Goal → Entity (Entity may be anchor; state is the bridge)
        if t_type == "Entity" and s_type in ("State", "Event", "Goal"):
            anchor_to_states[t_id].append({
                "predicate": rel or "related_to",
                "direction": "incoming",
                "state_id": s_id, "state_label": s_lbl,
                "state_category": "",
                "state_start_date": None,
                "edge_sentence": sent or "",
            })
        # State → Entity (the "other side" of a state)
        if s_type in ("State", "Event", "Goal") and t_type == "Entity":
            state_to_others[s_id].append({
                "other_id": t_id, "other_label": t_lbl,
                "other_category": (t_cat or "").strip().lower(),
                "predicate": rel or "related_to",
                "direction": "outgoing",
                "edge_sentence": sent or "",
            })
        if t_type in ("State", "Event", "Goal") and s_type == "Entity":
            state_to_others[t_id].append({
                "other_id": s_id, "other_label": s_lbl,
                "other_category": "",
                "predicate": rel or "related_to",
                "direction": "incoming",
                "edge_sentence": sent or "",
            })

    # Fill empty other_category from descriptors (we already have category there)
    for st_id, recs in state_to_others.items():
        for r in recs:
            if not r["other_category"]:
                d = descriptors.get(r["other_id"])
                if d:
                    r["other_category"] = (d.get("category") or "").strip().lower()

    return {"anchor_to_states": anchor_to_states, "state_to_others": state_to_others}


def _anchor_propagation_pairs(
    descriptors: dict[str, dict],
    prop_data: dict[str, Any],
) -> list[tuple[str, str, dict]]:
    """Tier 4 candidate generator.

    For each Entity anchor with >= ANCHOR_MIN_EDGE_COUNT edges, group its
    state-connected neighbors by (predicate, state_label, state_category).
    For each group with ≥2 distinct "other side" Entity endpoints AND no
    explicit date conflict among the state nodes, emit those Entity
    endpoints as candidate dup pairs. The pair's anchor context carries
    the edge sentences on both legs so the LLM can verify the merge by
    reading the actual claims.

    Returns: list of (entity_a_id, entity_b_id, anchor_context_dict).
    Caller dedupes against already-seen pairs and combines with other tiers.
    """
    from collections import defaultdict
    anchor_to_states = prop_data["anchor_to_states"]
    state_to_others = prop_data["state_to_others"]
    pairs: list[tuple[str, str, dict]] = []

    for anchor_id, states in anchor_to_states.items():
        anchor_desc = descriptors.get(anchor_id)
        if not anchor_desc:
            continue
        if (anchor_desc.get("node_type") or "") != "Entity":
            continue
        if (anchor_desc.get("edge_count") or 0) < ANCHOR_MIN_EDGE_COUNT:
            continue

        # Group anchor-side records by (predicate, state_label, state_category)
        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for s in states:
            key = (s["predicate"], (s["state_label"] or "").lower(), s["state_category"])
            groups[key].append(s)

        for key, members in groups.items():
            if len(members) < 2:
                continue
            # For each state in the group, collect its "other" Entity endpoints
            entity_to_evidence: dict[str, list[dict]] = defaultdict(list)
            for m in members:
                for o in state_to_others.get(m["state_id"], []):
                    if o["other_id"] == anchor_id:
                        continue  # don't pair the anchor with itself
                    o_desc = descriptors.get(o["other_id"])
                    if not o_desc or (o_desc.get("node_type") or "") != "Entity":
                        continue
                    entity_to_evidence[o["other_id"]].append({
                        "state_label": m["state_label"],
                        "state_category": m["state_category"],
                        "predicate_anchor_state": m["predicate"],
                        "state_start_date": m["state_start_date"],
                        "anchor_to_state_sentence": m["edge_sentence"],
                        "state_to_other_sentence": o["edge_sentence"],
                    })

            ents = list(entity_to_evidence.keys())
            if len(ents) < 2:
                continue

            # All unordered pairs with category match + no explicit date conflict
            for i in range(len(ents)):
                a = ents[i]
                a_evidence = entity_to_evidence[a]
                a_cat = (descriptors.get(a, {}).get("category") or "").strip().lower()
                for j in range(i + 1, len(ents)):
                    b = ents[j]
                    b_evidence = entity_to_evidence[b]
                    b_cat = (descriptors.get(b, {}).get("category") or "").strip().lower()
                    # Category filter: skip cross-category pairs (person vs place, etc.)
                    if a_cat and b_cat and a_cat != b_cat:
                        continue
                    # Date conflict: any (a_state, b_state) with both dates set
                    # and differing → reject (recurring/distinct-occurrence rule).
                    has_conflict = False
                    for ae in a_evidence:
                        if has_conflict:
                            break
                        for be in b_evidence:
                            if ae["state_start_date"] and be["state_start_date"]:
                                if ae["state_start_date"] != be["state_start_date"]:
                                    has_conflict = True
                                    break
                    if has_conflict:
                        continue
                    pairs.append((a, b, {
                        "anchor_id": anchor_id,
                        "anchor_label": anchor_desc.get("label") or "",
                        "predicate_path": key,
                        "a_evidence": a_evidence[:2],
                        "b_evidence": b_evidence[:2],
                    }))

    # Cap so a few high-fanout anchors don't dominate the LLM budget.
    return pairs[:TIER4_MAX_PAIRS]


# ── Phase 2: three-tier candidate generation ──────────────────────────────────

def _build_candidate_pairs(
    descriptors: dict[str, dict],
    unscanned_ids: set[str],
) -> tuple[list[tuple[str, str, str]], dict[tuple[str, str], dict]]:
    """
    Generate candidate duplicate pairs using four tiers.

    ``unscanned_ids`` is the set of nodes with ``last_dupe_scanned_at IS NULL``
    — i.e., never evaluated against the graph by a prior successful run.
    Pair (A, B) is emitted only when at least one side is in this set.
    Pairs of two already-stamped nodes were settled by an earlier run and
    re-triaging them is waste (constraint: "once we deem two nodes not
    same we should not keep re-comparing them"). On the first patched
    run, every node is in ``unscanned_ids`` so the scan does its full
    pass; subsequent runs converge to ~0 pairs in steady state.

    Returns:
      - candidates: list of (node_id_a, node_id_b, tier_source) tuples, deduplicated.
        Pairs are sorted by combined edge count (most connected first) so the
        most important nodes are reviewed within the per-run budget.
      - pair_anchor_context: dict keyed by sorted-pair tuple (a, b) carrying
        anchor-propagation context (anchor entity + per-side edge sentences)
        for pairs sourced from Tier 4. Empty dict for pairs from Tiers 1-3.
        Consumed by _confirm_pairs_with_llm to enrich the LLM brief.
    """
    from app.assistant.kg_maintenance.verdict_store import load_distinct_pairs

    # Bulk-fetch all active 'distinct' verdicts once, locally canonicalized
    # by the store as (a, b) with a < b. Hot path: every Tier 1/2/3 pair
    # checks membership in this set instead of opening a read session.
    distinct_pairs = load_distinct_pairs()

    seen_pairs: set[tuple[str, str]] = set()
    candidates: list[tuple[str, str, str]] = []
    pair_anchor_context: dict[tuple[str, str], dict] = {}
    cross_type_skipped = 0
    prior_verdict_skipped = 0
    already_scanned_skipped = 0

    def _add_pair(a: str, b: str, tier: str, anchor_ctx: dict | None = None) -> None:
        nonlocal cross_type_skipped, prior_verdict_skipped, already_scanned_skipped
        key = (min(a, b), max(a, b))
        if key in seen_pairs:
            # Already added by an earlier tier; if a richer anchor context
            # arrives now, fold it in so the LLM still gets it.
            if anchor_ctx and key not in pair_anchor_context:
                pair_anchor_context[key] = anchor_ctx
            return
        # Per-node scanned-stamp short-circuit. Both sides previously
        # processed by a successful maintenance run → the pair was either
        # triaged distinct (cached in distinct_pairs) or escalated to a
        # finding (which the investigator path handles). Re-asking is
        # pure waste at every layer (descriptor build, embedding, LLM).
        if a not in unscanned_ids and b not in unscanned_ids:
            already_scanned_skipped += 1
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
        if anchor_ctx:
            pair_anchor_context[key] = anchor_ctx

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

    # --- Tier 2: Label containment / token-subset (same node_type) ---
    # Two paths emit a pair:
    #   (a) raw substring with length ratio ≥ 60% — original behavior. Catches
    #       "Morning routine" / "Morning routine (weekday)" where aliases don't
    #       help. Ratio gates noise like "Jukka" inside "Jukka's CPAP machine".
    #   (b) **whole-word token subset** — the shorter label's tokens are ALL
    #       present in the longer label's tokens. Catches "Joe" / "Joe Booth",
    #       "Katy" / "Katy Bowen", "Peter" / "Peter Virtanen" — first-name vs
    #       full-name pairs that the % ratio rejects. Word boundaries make this
    #       safe: "joes" doesn't match "joe" as a token, so we don't accidentally
    #       pair "Joe" with "Joes Pizza".
    import re as _re
    CONTAINMENT_MIN_RATIO = 0.60
    def _tokens(s: str) -> list[str]:
        return [t for t in _re.split(r"[^A-Za-z0-9]+", s.lower()) if t]

    by_type: dict[str, list[tuple[str, str, frozenset]]] = {}
    for nid, d in descriptors.items():
        if d["label"] and len(d["label"].strip()) >= 3:
            nt = d["node_type"]
            lbl = d["label"].strip().lower()
            by_type.setdefault(nt, []).append((nid, lbl, frozenset(_tokens(lbl))))

    for nt, entries in by_type.items():
        entries.sort(key=lambda x: len(x[1]))
        for i in range(len(entries)):
            nid_i, label_i, toks_i = entries[i]
            for j in range(i + 1, len(entries)):
                nid_j, label_j, toks_j = entries[j]
                if label_i == label_j:
                    continue
                # Path (a): high-ratio substring containment
                if label_i in label_j and len(label_i) / len(label_j) >= CONTAINMENT_MIN_RATIO:
                    _add_pair(nid_i, nid_j, "label_containment")
                    continue
                # Path (b): whole-word token subset — the shorter label's
                # non-empty token set is fully inside the longer label's
                # token set. Requires the shorter to have at least one
                # token (already guaranteed by len>=3 + tokenizer).
                if toks_i and toks_i.issubset(toks_j):
                    _add_pair(nid_i, nid_j, "label_token_subset")

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
    tier3_count = len(candidates)

    # --- Tier 4: Anchor-propagation ---
    # Structural: when a single Entity anchor relates to two endpoints via
    # the same predicate path through structurally-similar state nodes, those
    # endpoints are merge candidates. Catches kinship/role placeholders
    # (Jukka's son <-> Peter, Phil's mother <-> Pam) and label variants that
    # share a workplace/school/family relation but whose strings don't match.
    try:
        prop_data = _load_anchor_propagation_data(descriptors)
        for a, b, anchor_ctx in _anchor_propagation_pairs(descriptors, prop_data):
            _add_pair(a, b, "anchor_propagation", anchor_ctx=anchor_ctx)
    except Exception as exc:
        logger.warning("[duplicate_scan] Tier 4 failed: %s", exc, exc_info=True)

    logger.info(
        "[duplicate_scan] Tier 4 (anchor propagation): %d new pairs",
        len(candidates) - tier3_count,
    )

    logger.info(
        "[duplicate_scan] Total unique candidate pairs: %d "
        "(cross_type_skipped=%d, prior_verdict_skipped=%d, "
        "already_scanned_skipped=%d, anchor_ctx_count=%d)",
        len(candidates), cross_type_skipped, prior_verdict_skipped,
        already_scanned_skipped, len(pair_anchor_context),
    )

    # Prioritize pairs involving the most-connected nodes
    def _pair_priority(pair: tuple[str, str, str]) -> float:
        a, b, _tier = pair
        ec_a = descriptors.get(a, {}).get("edge_count", 0)
        ec_b = descriptors.get(b, {}).get("edge_count", 0)
        return ec_a + ec_b

    candidates.sort(key=_pair_priority, reverse=True)
    return candidates[:MAX_PAIRS_PER_RUN], pair_anchor_context


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

def _triage_filter_pairs(
    pairs: list[tuple[str, str, str]],
    descriptors: dict[str, dict],
    scope_context,
    pair_anchor_context: dict[tuple[str, str], dict],
) -> list[tuple[str, str, str]]:
    """Pre-filter step: nano-class LLM marks each pair as 'distinct' (drop) or
    'uncertain' (forward to the heavy detector). Asymmetric trust — we trust
    the nano's NO (drop), but never its YES (it never emits 'merge').

    Falls back to passing all pairs through if the triage agent fails.
    """
    if not pairs:
        return pairs
    try:
        agent = DI.agent_factory.create_agent("kg_maintenance::duplicate_triage")
        if agent is None:
            logger.warning("[duplicate_scan] triage agent unavailable — skipping pre-filter")
            return pairs
    except Exception as exc:
        logger.warning("[duplicate_scan] triage agent create failed: %s — skipping pre-filter", exc)
        return pairs

    # Build a compact per-pair brief.
    brief_pairs = []
    for idx, (nid_a, nid_b, tier) in enumerate(pairs):
        da = descriptors.get(nid_a) or {}
        db = descriptors.get(nid_b) or {}
        entry: dict[str, Any] = {
            "pair_index": idx + 1,
            "tier": tier,
            "a": {
                "label": da.get("label"),
                "node_type": da.get("node_type"),
                "category": da.get("category"),
                "aliases": da.get("aliases") or [],
                "description": (da.get("description") or "")[:200],
                "original_sentence": da.get("original_sentence") or "",
                "edge_sentences": (da.get("edge_sentences") or [])[:3],
            },
            "b": {
                "label": db.get("label"),
                "node_type": db.get("node_type"),
                "category": db.get("category"),
                "aliases": db.get("aliases") or [],
                "description": (db.get("description") or "")[:200],
                "original_sentence": db.get("original_sentence") or "",
                "edge_sentences": (db.get("edge_sentences") or [])[:3],
            },
        }
        key = (min(nid_a, nid_b), max(nid_a, nid_b))
        anchor_ctx = pair_anchor_context.get(key)
        if anchor_ctx:
            entry["shared_anchor"] = {
                "anchor_label": anchor_ctx.get("anchor_label"),
                "predicate_path": list(anchor_ctx.get("predicate_path") or []),
                "a_edges": [
                    {"anchor_to_state": e.get("anchor_to_state_sentence"),
                     "state_to_other": e.get("state_to_other_sentence"),
                     "state_start_date": e.get("state_start_date")}
                    for e in (anchor_ctx.get("a_evidence") or [])
                ],
                "b_edges": [
                    {"anchor_to_state": e.get("anchor_to_state_sentence"),
                     "state_to_other": e.get("state_to_other_sentence"),
                     "state_start_date": e.get("state_start_date")}
                    for e in (anchor_ctx.get("b_evidence") or [])
                ],
            }
        brief_pairs.append(entry)

    try:
        response = agent.action_handler(
            Message(
                agent_input={"pairs_batch": json.dumps(brief_pairs, ensure_ascii=False)},
                scope_context=scope_context,
            )
        )
    except Exception as exc:
        logger.warning("[duplicate_scan] triage call failed: %s — passing all pairs through", exc)
        return pairs

    data = response.data if response and hasattr(response, "data") else {}
    verdicts_raw = (data.get("pairs") if isinstance(data, dict) else None) or []
    distinct_indices: set[int] = set()
    for v in verdicts_raw:
        if not isinstance(v, dict):
            continue
        try:
            idx = int(v.get("pair_index") or 0)
        except (TypeError, ValueError):
            continue
        verdict = str(v.get("verdict") or "").strip().lower()
        if verdict == "distinct" and 1 <= idx <= len(pairs):
            distinct_indices.add(idx - 1)

    survivors = [p for i, p in enumerate(pairs) if i not in distinct_indices]

    # Persist the triage's distinct verdicts so future maintenance runs
    # short-circuit these same pairs at the `_add_pair` filter instead of
    # paying nano-LLM cost on them every week. Without this, the same
    # ~95-97% of candidate pairs get re-evaluated every run.
    if distinct_indices:
        try:
            from app.assistant.kg_maintenance.verdict_store import (
                record_distinct_pairs_bulk,
            )
            distinct_pair_ids = [
                (pairs[i][0], pairs[i][1]) for i in distinct_indices
            ]
            record_distinct_pairs_bulk(
                distinct_pair_ids,
                decided_by="kg_maintenance::duplicate_triage",
                memo=(
                    "nano triage scored as distinct in duplicate_scan; "
                    "tiers vary across batch"
                ),
            )
        except Exception as exc:
            logger.warning(
                "[duplicate_scan] failed to persist %d triage distinct verdicts: %s",
                len(distinct_indices), exc,
            )

    logger.info(
        "[duplicate_scan] Triage: %d pairs → %d survived (%d dropped as 'distinct')",
        len(pairs), len(survivors), len(distinct_indices),
    )
    return survivors


def _confirm_pairs_with_llm(
    pairs: list[tuple[str, str, str]],
    descriptors: dict[str, dict],
    scope_context,
    pair_anchor_context: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """
    Calls kg_maintenance::duplicate_detector for each candidate pair.
    Sends rich context including aliases, category, and neighborhood.
    For Tier 4 (anchor-propagation) pairs, additionally includes the shared
    anchor entity + edge sentences on both legs in `analysis_context.shared_anchor`
    so the agent can decide via direct edge-sentence comparison.

    Returns a flat list of merge_action dicts.

    No DB session is open during this function.
    """
    pair_anchor_context = pair_anchor_context or {}

    # Pre-filter cheap: nano-class triage drops obviously-distinct pairs so the
    # heavy detector only spends time on plausible ones. Asymmetric trust:
    # we accept its NO, never its YES (the detector still owns the merge call).
    pairs = _triage_filter_pairs(pairs, descriptors, scope_context, pair_anchor_context)

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

        analysis_context: dict[str, Any] = {
            "pair_index": idx + 1,
            "total_pairs": len(pairs),
            "detection_tier": tier,
        }
        key = (min(nid_a, nid_b), max(nid_a, nid_b))
        anchor_ctx = pair_anchor_context.get(key)
        if anchor_ctx:
            # Tier 4 anchor-propagation context — the strongest structural
            # signal a duplicate-detector LLM can use: a shared anchor entity
            # plus the actual edge-sentences linking the anchor to each side.
            analysis_context["shared_anchor"] = {
                "anchor_label": anchor_ctx.get("anchor_label"),
                "predicate_path": list(anchor_ctx.get("predicate_path") or []),
                "a_edges": [
                    {"anchor_to_state": e.get("anchor_to_state_sentence"),
                     "state_to_other": e.get("state_to_other_sentence"),
                     "state_start_date": e.get("state_start_date")}
                    for e in (anchor_ctx.get("a_evidence") or [])
                ],
                "b_edges": [
                    {"anchor_to_state": e.get("anchor_to_state_sentence"),
                     "state_to_other": e.get("state_to_other_sentence"),
                     "state_start_date": e.get("state_start_date")}
                    for e in (anchor_ctx.get("b_evidence") or [])
                ],
            }

        try:
            response = agent.action_handler(
                Message(
                    agent_input={
                        "duplicate_group_data": json.dumps(node_data, ensure_ascii=False),
                        "analysis_context": json.dumps(analysis_context),
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
    Returns {"nodes_loaded": int, "candidate_pairs": int, "llm_confirmed_actions": int, "new_findings": int, "nodes_stamped": int}.
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
        return {"nodes_loaded": 0, "candidate_pairs": 0, "llm_confirmed_actions": 0, "new_findings": 0, "nodes_stamped": 0}

    # Load the set of nodes that have never been evaluated by a prior
    # successful maintenance run. The pair generator emits (A, B) only
    # when at least one side is in this set; pairs of two already-scanned
    # nodes were settled by an earlier run.
    unscanned_ids = _load_unscanned_node_ids()
    logger.info(
        "[duplicate_scan] %d/%d nodes need scanning (last_dupe_scanned_at IS NULL)",
        len(unscanned_ids), len(descriptors),
    )

    # Phase 2 — four-tier candidate generation (no LLM; Tier 4 takes one
    # extra read session for the anchor-propagation graph walk)
    pairs, pair_anchor_context = _build_candidate_pairs(descriptors, unscanned_ids)
    if not pairs:
        logger.info("[duplicate_scan] No candidate pairs found")
        # Still stamp — no work needed means every node is current.
        n_stamped = _stamp_nodes_scanned(list(descriptors.keys()))
        return {
            "nodes_loaded": len(descriptors), "candidate_pairs": 0,
            "llm_confirmed_actions": 0, "new_findings": 0,
            "nodes_stamped": n_stamped,
        }

    # Phase 3 — LLM confirmation (no session open)
    merge_actions = _confirm_pairs_with_llm(
        pairs, descriptors, scope_context,
        pair_anchor_context=pair_anchor_context,
    )

    # Phase 4 — write findings (store manages its own sessions)
    new_findings = _write_findings(merge_actions, pipeline_run_id=ctx.run_id)

    # Phase 5 — stamp every node as scanned. This is the watermark that
    # lets the next run's pair generator skip pairs of two already-
    # scanned nodes via `_add_pair`. Stamping all nodes (not just those
    # in pair candidates) is correct: a node that produced zero pairs
    # was still evaluated by the tier 1-4 generators against every
    # other node — finding no candidates IS a complete scan result.
    n_stamped = _stamp_nodes_scanned(list(descriptors.keys()))

    result = {
        "nodes_loaded": len(descriptors),
        "candidate_pairs": len(pairs),
        "llm_confirmed_actions": len(merge_actions),
        "new_findings": new_findings,
        "nodes_stamped": n_stamped,
    }
    logger.info("[duplicate_scan] Done: %s", result)
    return result


def _load_unscanned_node_ids() -> set[str]:
    """Return ids of nodes with ``last_dupe_scanned_at IS NULL``."""
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.models.base import get_session
    session = get_session()
    try:
        rows = (
            session.query(Node.id)
            .filter(Node.last_dupe_scanned_at.is_(None))
            .all()
        )
        return {r[0] for r in rows}
    finally:
        session.close()


def _stamp_nodes_scanned(node_ids: list[str]) -> int:
    """Stamp ``last_dupe_scanned_at = now`` on the given node ids.

    Uses a direct UPDATE with ``Node.updated_at = Node.updated_at`` self-
    reference to suppress the onupdate hook — `last_dupe_scanned_at` is
    operational metadata, not a content change, and bumping updated_at
    here would cascade into wiki/card refreshes for no semantic reason
    (same protective pattern as step_pagerank).
    """
    if not node_ids:
        return 0
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.utils.time_utils import utc_now
    from app.models.base import get_session
    now = utc_now()
    session = get_session()
    try:
        chunk = 500
        n = 0
        for i in range(0, len(node_ids), chunk):
            batch = node_ids[i:i + chunk]
            session.query(Node).filter(Node.id.in_(batch)).update(
                {
                    Node.last_dupe_scanned_at: now,
                    Node.updated_at: Node.updated_at,
                },
                synchronize_session=False,
            )
            n += len(batch)
        session.commit()
        return n
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
