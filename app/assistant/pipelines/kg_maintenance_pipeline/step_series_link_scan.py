"""
Step: series_link_scan

Detects clusters of Event nodes that look like recurring instances of one
concept (e.g. nine "Friday Night Meats" Events across different weeks) and
proposes:
  - link the Events to an existing Entity at the same label, if one exists
  - OR create a parent Entity and then link

Mirrors step_duplicate_scan's shape (Phase 1 load -> Phase 2 candidates ->
Phase 3 LLM confirm -> Phase 4 write findings) but for series-linking
instead of node-merging.

Algorithm
---------
Phase 1 -- Read (short-lived session)
    Load every Event node's id, label, start_date, plus a small sample of
    its edge sentences. Also load every Entity node's id + label for
    parent-lookup.

Phase 2 -- Fuzzy clustering (no DB, no LLM)
    Normalize labels (lowercase, strip non-alphanumerics, collapse
    whitespace). Group Event nodes by normalized label, filter to clusters
    of size >= MIN_CLUSTER_SIZE.

Phase 3 -- Nano confirmation (no session open)
    Batch the clusters and send to kg_maintenance::series_link_triage. The
    agent returns one verdict per cluster: 'is_series' or 'not_series'.
    Only 'is_series' clusters move on.

Phase 4 -- Write findings (one short-lived session per finding via store)
    For each confirmed cluster, emit pairwise findings of type
    'event_series_link'. When a parent Entity exists, primary=Entity and
    one finding per (Entity, Event_i) pair. When no Entity exists,
    primary=the seed Event (smallest UUID for stability) and one finding
    per (seed, Event_i) pair, with evidence flagging the executor to
    create the Entity at the canonical label.

The executor handler reads the cluster from the evidence dict and applies
the mutation atomically (create-Entity-if-needed + N instance_of edges).
"""
from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from typing import Any, Optional

from app.assistant.kg_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)


# Tunables
MIN_CLUSTER_SIZE = 2
TRIAGE_BATCH_SIZE = 8
MAX_EDGE_SENTENCES_PER_EVENT = 2
MAX_SAMPLE_EVENTS_PER_CLUSTER = 6
MAX_ENTITY_EDGE_SENTENCES = 3


# ── Label normalization ───────────────────────────────────────────────────

def normalize_label(label: str) -> str:
    """Fuzzy match key. Lowercase, strip non-alphanumerics, collapse spaces."""
    if not label:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", "", label.lower())).strip()


# ── Phase 1: read ─────────────────────────────────────────────────────────

def _load_events_and_entities() -> tuple[list[dict], dict[str, list[dict]]]:
    """Return (event_records, entities_by_normalized_label).

    event_records: list of dicts with id, label, start_date, edge_sentences,
      edge_count.
    entities_by_normalized_label: {normalized_label: [{id, label,
      edge_sentences, edge_count}, ...]}. A label can map to multiple
      Entities if cluster_resolver didn't fully merge them — we surface
      that to the LLM and let it pick.

    All session work is contained here; nothing leaves while a session is
    open.
    """
    from sqlalchemy import func as sqlfunc
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    session = get_session()
    try:
        event_rows = (
            session.query(
                Node.id, Node.label, Node.start_date,
            )
            .filter(Node.node_type == "Event")
            .filter(Node.label.isnot(None))
            .all()
        )
        entity_rows = (
            session.query(Node.id, Node.label)
            .filter(Node.node_type == "Entity")
            .filter(Node.label.isnot(None))
            .all()
        )
        event_ids = {str(r.id) for r in event_rows}
        entity_ids = {str(r.id) for r in entity_rows}
        relevant_ids = event_ids | entity_ids

        edge_rows = (
            session.query(Edge.source_id, Edge.target_id, Edge.sentence)
            .filter(
                (Edge.source_id.in_(relevant_ids))
                | (Edge.target_id.in_(relevant_ids))
            )
            .all()
        )
    finally:
        session.close()

    # Build edge sentence + edge count indices
    sentence_index: dict[str, list[str]] = defaultdict(list)
    edge_count_index: dict[str, int] = defaultdict(int)
    for src, tgt, sent in edge_rows:
        for nid in (str(src), str(tgt)):
            if nid in relevant_ids:
                edge_count_index[nid] += 1
                if sent and sent.strip() and len(sentence_index[nid]) < max(
                    MAX_EDGE_SENTENCES_PER_EVENT, MAX_ENTITY_EDGE_SENTENCES
                ):
                    sentence_index[nid].append(sent.strip())

    event_records: list[dict] = []
    for r in event_rows:
        nid = str(r.id)
        event_records.append({
            "id": nid,
            "label": r.label or "",
            "normalized_label": normalize_label(r.label or ""),
            "start_date": str(r.start_date)[:10] if r.start_date else None,
            "edge_count": edge_count_index.get(nid, 0),
            "edge_sentences": (
                sentence_index.get(nid, [])[:MAX_EDGE_SENTENCES_PER_EVENT]
            ),
        })

    entities_by_label: dict[str, list[dict]] = defaultdict(list)
    for r in entity_rows:
        nid = str(r.id)
        key = normalize_label(r.label or "")
        if not key:
            continue
        entities_by_label[key].append({
            "id": nid,
            "label": r.label or "",
            "edge_count": edge_count_index.get(nid, 0),
            "edge_sentences": (
                sentence_index.get(nid, [])[:MAX_ENTITY_EDGE_SENTENCES]
            ),
        })

    logger.info(
        "[series_link_scan] Loaded %d Events and %d Entities",
        len(event_records), sum(len(v) for v in entities_by_label.values()),
    )
    return event_records, entities_by_label


# ── Phase 2: cluster ──────────────────────────────────────────────────────

def _cluster_events_by_label(events: list[dict]) -> list[list[dict]]:
    """Group events by normalized label. Returns list of clusters (each a
    list of event dicts), filtered to size >= MIN_CLUSTER_SIZE.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e["normalized_label"]:
            grouped[e["normalized_label"]].append(e)
    return [v for v in grouped.values() if len(v) >= MIN_CLUSTER_SIZE]


# ── Phase 3: nano confirmation ────────────────────────────────────────────

def _build_cluster_brief(
    cluster_index_1based: int,
    cluster: list[dict],
    parent_entities: list[dict],
) -> dict[str, Any]:
    """One per-cluster JSON brief for the triage agent."""
    sample = cluster[:MAX_SAMPLE_EVENTS_PER_CLUSTER]
    brief: dict[str, Any] = {
        "cluster_index": cluster_index_1based,
        "normalized_label": cluster[0]["normalized_label"],
        "display_label_sample": cluster[0]["label"],
        "cluster_size": len(cluster),
        "sample_events": [
            {
                "id_prefix": e["id"][:8],
                "start_date": e["start_date"],
                "edge_sentences": e["edge_sentences"],
            }
            for e in sample
        ],
    }
    if parent_entities:
        ent = parent_entities[0]  # If multiple, surface the first; rare case
        brief["existing_entity"] = {
            "label": ent["label"],
            "edge_count": ent["edge_count"],
            "edge_sentences": ent["edge_sentences"],
        }
        if len(parent_entities) > 1:
            brief["existing_entity"]["note"] = (
                f"{len(parent_entities)} Entities share this label; "
                f"showing the first."
            )
    return brief


def _confirm_clusters(
    clusters: list[list[dict]],
    entities_by_label: dict[str, list[dict]],
    scope_context,
) -> dict[int, dict]:
    """Run kg_maintenance::series_link_triage in batches. Returns
    {cluster_index_0based: verdict_dict} for clusters the agent confirmed
    as 'is_series'. 'not_series' verdicts are dropped.
    """
    try:
        probe = DI.agent_factory.create_agent("kg_maintenance::series_link_triage")
        if probe is None:
            logger.warning("[series_link_scan] triage agent unavailable")
            return {}
    except Exception as exc:
        logger.warning("[series_link_scan] triage agent create failed: %s", exc)
        return {}

    confirmed: dict[int, dict] = {}
    n_batches = (len(clusters) + TRIAGE_BATCH_SIZE - 1) // TRIAGE_BATCH_SIZE

    for batch_start in range(0, len(clusters), TRIAGE_BATCH_SIZE):
        batch = clusters[batch_start:batch_start + TRIAGE_BATCH_SIZE]
        briefs = [
            _build_cluster_brief(
                cluster_index_1based=local_idx + 1,
                cluster=cluster,
                parent_entities=entities_by_label.get(cluster[0]["normalized_label"], []),
            )
            for local_idx, cluster in enumerate(batch)
        ]

        try:
            agent = DI.agent_factory.create_agent("kg_maintenance::series_link_triage")
            if agent is None:
                logger.warning(
                    "[series_link_scan] agent instantiate failed for batch %d",
                    batch_start // TRIAGE_BATCH_SIZE + 1,
                )
                continue
            response = agent.action_handler(
                Message(
                    agent_input={
                        "clusters_batch": json.dumps(briefs, ensure_ascii=False),
                    },
                    scope_context=scope_context,
                )
            )
        except Exception as exc:
            logger.warning(
                "[series_link_scan] triage batch %d failed: %s",
                batch_start // TRIAGE_BATCH_SIZE + 1, exc,
            )
            continue

        data = response.data if response and hasattr(response, "data") else {}
        verdicts_raw = (data.get("clusters") if isinstance(data, dict) else None) or []
        for v in verdicts_raw:
            if not isinstance(v, dict):
                continue
            try:
                local_idx_1b = int(v.get("cluster_index") or 0)
            except (TypeError, ValueError):
                continue
            verdict = str(v.get("verdict") or "").strip().lower()
            if verdict != "is_series" or not (1 <= local_idx_1b <= len(batch)):
                continue
            global_idx = batch_start + (local_idx_1b - 1)
            confirmed[global_idx] = {
                "verdict": verdict,
                "canonical_label": (v.get("canonical_label") or "").strip(),
                "reason": (v.get("reason") or "").strip(),
            }

    logger.info(
        "[series_link_scan] Triage: %d clusters → %d confirmed (%d batches)",
        len(clusters), len(confirmed), n_batches,
    )
    return confirmed


# ── Phase 4: write findings ───────────────────────────────────────────────

def _write_findings(
    clusters: list[list[dict]],
    confirmed: dict[int, dict],
    entities_by_label: dict[str, list[dict]],
    pipeline_run_id: str,
) -> int:
    """Emit findings of type 'event_series_link'. One finding per (canonical,
    event_i) pair within a cluster. Returns total findings created.

    The `cluster_id` in evidence ties the per-pair findings back together
    so the executor processes them atomically.
    """
    new_findings = 0
    for cluster_idx, cluster in enumerate(clusters):
        if cluster_idx not in confirmed:
            continue
        v = confirmed[cluster_idx]
        canonical_label = v["canonical_label"] or cluster[0]["label"]
        normalized_label = cluster[0]["normalized_label"]
        parents = entities_by_label.get(normalized_label, [])

        # Stable cluster id so the per-pair findings are grouped at exec time
        cluster_id = str(uuid.uuid4())

        # Determine the canonical (primary) node for this cluster's findings
        if parents:
            primary_id = parents[0]["id"]
            action = "link_events_to_entity"
            entity_already_exists = True
        else:
            # Sort event IDs for stable picker (so re-runs hit the same primary)
            sorted_event_ids = sorted(e["id"] for e in cluster)
            primary_id = sorted_event_ids[0]
            action = "create_parent_entity_and_link"
            entity_already_exists = False

        event_ids = [e["id"] for e in cluster]

        for ev in cluster:
            if ev["id"] == primary_id:
                continue  # don't pair the primary with itself
            evidence = {
                "cluster_id": cluster_id,
                "action": action,
                "canonical_label": canonical_label,
                "normalized_label": normalized_label,
                "entity_already_exists": entity_already_exists,
                "all_event_ids": event_ids,
                "cluster_size": len(event_ids),
                "triage_reason": v.get("reason", ""),
            }
            _, created = upsert_finding(
                finding_type="event_series_link",
                primary_node_id=primary_id,
                secondary_node_id=ev["id"],
                suggested_action=action,
                reason=(
                    f"Cluster of {len(event_ids)} Events sharing label "
                    f"{canonical_label!r}. {v.get('reason', '')}"
                ),
                confidence=0.85,
                priority="medium",
                agent_name="kg_maintenance::series_link_triage",
                evidence=evidence,
                pipeline_run_id=pipeline_run_id,
            )
            if created:
                new_findings += 1

    return new_findings


# ── Entry point ───────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> dict:
    """Returns {events_loaded, clusters_found, confirmed_series, new_findings}."""
    logger.info("[series_link_scan] Starting run_id=%s", ctx.run_id)

    scope_context = build_pipeline_scope_context(
        pipeline_id=ctx.pipeline_id,
        actor_id="kg_maintenance_runner",
    )

    events, entities_by_label = _load_events_and_entities()
    if not events:
        return {
            "events_loaded": 0, "clusters_found": 0,
            "confirmed_series": 0, "new_findings": 0,
        }

    clusters = _cluster_events_by_label(events)
    if not clusters:
        return {
            "events_loaded": len(events), "clusters_found": 0,
            "confirmed_series": 0, "new_findings": 0,
        }

    confirmed = _confirm_clusters(clusters, entities_by_label, scope_context)
    new_findings = _write_findings(
        clusters, confirmed, entities_by_label, pipeline_run_id=ctx.run_id,
    )

    result = {
        "events_loaded": len(events),
        "clusters_found": len(clusters),
        "confirmed_series": len(confirmed),
        "new_findings": new_findings,
    }
    logger.info("[series_link_scan] Done: %s", result)
    return result
