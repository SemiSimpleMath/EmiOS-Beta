"""
Step: series_link_investigate

Processes pending `event_series_link` findings produced by
step_series_link_scan. For each cluster, pulls richer context than the
triage saw, calls the kg_maintenance::series_link_investigator agent,
and routes the cluster to `approved` or `rejected`.

Algorithm
---------
Phase 1 -- Group pending findings by cluster_id (from evidence_json).
Phase 2 -- For each cluster:
    a. Resolve cluster metadata (canonical_label, action, all_event_ids)
       from any finding's evidence_json.
    b. Load all Event nodes + all edge sentences touching them (single
       short-lived read session).
    c. If action='link_events_to_entity', load the candidate Entity's
       neighborhood too.
    d. Pre-compute concept-level aggregations: participants appearing
       on multiple Events, observed cadence over start_dates.
    e. Build cluster_brief and existing_entity_brief as JSON-ish strings.
    f. Call the investigator agent (no session open).
    g. Apply verdict to every finding in the cluster:
       - approve  -> status='approved', investigation_report_json carries
                     the agent's reasoning + cross-instance participants
                     + cadence note + refined canonical_label.
       - reject   -> status='rejected', investigation_report_json carries
                     the reasoning so the user can audit the call.

Sessions are short-lived and never open across the LLM call (matches the
duplicate_scan contract).

Output
------
Returns {clusters_seen, approved, rejected, errors}.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import or_

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)


MAX_EDGE_SENTENCES_PER_EVENT = 6
MAX_ENTITY_EDGE_SENTENCES = 20
MAX_EVENTS_PER_CLUSTER_BRIEF = 12  # truncate giant clusters to keep prompts under budget


# ── Phase 1: group pending findings by cluster_id ─────────────────────────

def _load_pending_clusters() -> dict[str, list[dict]]:
    """Returns {cluster_id: [finding_dict, ...]} for all pending
    event_series_link findings.

    Findings without a cluster_id are skipped (defensive — every finding
    minted by step_series_link_scan carries one).
    """
    session = get_session()
    try:
        rows = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == "event_series_link")
            .filter(KGMaintenanceFinding.status == "pending")
            .all()
        )
        clusters: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            evidence = r.evidence_json or {}
            cid = evidence.get("cluster_id")
            if not cid:
                continue
            clusters[cid].append({
                "id": r.id,
                "primary_node_id": r.primary_node_id,
                "secondary_node_id": r.secondary_node_id,
                "evidence_json": evidence,
            })
    finally:
        session.close()
    return clusters


# ── Phase 2a: cluster metadata ────────────────────────────────────────────

def _cluster_meta(findings: list[dict]) -> dict:
    """Pull the shared metadata from any finding's evidence_json. All
    findings in a cluster carry the same canonical_label / action /
    all_event_ids; we just take from the first."""
    evidence = findings[0]["evidence_json"]
    return {
        "canonical_label": evidence.get("canonical_label", ""),
        "normalized_label": evidence.get("normalized_label", ""),
        "action": evidence.get("action", ""),
        "entity_already_exists": bool(evidence.get("entity_already_exists")),
        "all_event_ids": list(evidence.get("all_event_ids") or []),
    }


# ── Phase 2b: load Event + (optional) Entity context ──────────────────────

def _load_node_context(
    event_ids: list[str],
    entity_id: Optional[str],
) -> tuple[list[dict], Optional[dict]]:
    """Return (event_records, entity_record_or_None).

    event_records: list of dicts (id, label, start_date, edges) for each
      Event id. `edges` is a list of {direction, relationship, sentence,
      other_label} dicts.
    entity_record: same shape, when entity_id is supplied AND found.

    One session opens for the whole read; closes before return.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    if not event_ids and not entity_id:
        return [], None

    ids_of_interest = set(event_ids)
    if entity_id:
        ids_of_interest.add(entity_id)

    session = get_session()
    try:
        # Load the nodes themselves
        nodes = (
            session.query(Node.id, Node.label, Node.node_type, Node.start_date, Node.description)
            .filter(Node.id.in_(ids_of_interest))
            .all()
        )
        node_index: dict[str, dict] = {
            str(n.id): {
                "id": str(n.id),
                "label": n.label or "",
                "node_type": n.node_type or "",
                "start_date": str(n.start_date)[:10] if n.start_date else None,
                "description": (n.description or "")[:400],
            }
            for n in nodes
        }

        # Load all edges touching any of the nodes
        edge_rows = (
            session.query(
                Edge.source_id, Edge.target_id, Edge.relationship_type, Edge.sentence,
            )
            .filter(
                or_(
                    Edge.source_id.in_(ids_of_interest),
                    Edge.target_id.in_(ids_of_interest),
                )
            )
            .all()
        )

        # Collect labels of the OTHER endpoints so the LLM sees who is
        # connected (e.g. participant labels)
        other_endpoint_ids: set[str] = set()
        for src, tgt, _, _ in edge_rows:
            src_s, tgt_s = str(src), str(tgt)
            if src_s in ids_of_interest and tgt_s not in ids_of_interest:
                other_endpoint_ids.add(tgt_s)
            if tgt_s in ids_of_interest and src_s not in ids_of_interest:
                other_endpoint_ids.add(src_s)
        other_label_index: dict[str, str] = {}
        if other_endpoint_ids:
            other_rows = (
                session.query(Node.id, Node.label)
                .filter(Node.id.in_(other_endpoint_ids))
                .all()
            )
            other_label_index = {str(n.id): (n.label or "") for n in other_rows}
    finally:
        session.close()

    # Distribute edges to their owning nodes
    edge_index: dict[str, list[dict]] = defaultdict(list)
    for src, tgt, rel, sent in edge_rows:
        src_s, tgt_s = str(src), str(tgt)
        sent_clean = (sent or "").strip()
        for owner, other in ((src_s, tgt_s), (tgt_s, src_s)):
            if owner not in ids_of_interest:
                continue
            direction = "outgoing" if owner == src_s else "incoming"
            other_label = node_index.get(other, {}).get("label") or other_label_index.get(other, "")
            edge_index[owner].append({
                "direction": direction,
                "relationship": rel or "related_to",
                "sentence": sent_clean,
                "other_id": other,
                "other_label": other_label,
            })

    event_records: list[dict] = []
    for eid in event_ids:
        rec = dict(node_index.get(eid, {"id": eid, "label": "", "node_type": "Event"}))
        rec["edges"] = edge_index.get(eid, [])[:MAX_EDGE_SENTENCES_PER_EVENT]
        event_records.append(rec)

    entity_record = None
    if entity_id and entity_id in node_index:
        entity_record = dict(node_index[entity_id])
        entity_record["edges"] = edge_index.get(entity_id, [])[:MAX_ENTITY_EDGE_SENTENCES]

    return event_records, entity_record


# ── Phase 2d: pre-compute aggregations ────────────────────────────────────

def _aggregate_participants(event_records: list[dict]) -> list[dict]:
    """Count cross-Event participant labels (the OTHER endpoint of
    participant-like edges)."""
    PARTICIPANT_PREDICATES = {"participant", "has_participant", "attended_by", "involves"}
    counter: Counter = Counter()
    for ev in event_records:
        seen_in_this_event: set[str] = set()
        for e in ev.get("edges") or []:
            rel = (e.get("relationship") or "").lower()
            if rel not in PARTICIPANT_PREDICATES:
                continue
            lbl = (e.get("other_label") or "").strip()
            if not lbl:
                continue
            if lbl in seen_in_this_event:
                continue
            seen_in_this_event.add(lbl)
            counter[lbl] += 1
    return [
        {"participant_label": lbl, "occurrences": n}
        for lbl, n in counter.most_common()
        if n >= 2
    ]


def _cadence_signal(event_records: list[dict]) -> str:
    """Return a short prose summary of start_date spread. Empty when
    too few dates."""
    dates = sorted(
        ev["start_date"] for ev in event_records if ev.get("start_date")
    )
    if len(dates) < 2:
        return ""
    span_days = (
        datetime.strptime(dates[-1], "%Y-%m-%d") - datetime.strptime(dates[0], "%Y-%m-%d")
    ).days
    return (
        f"{len(dates)} dated instances from {dates[0]} to {dates[-1]} "
        f"(spread of {span_days} days; mean interval ~"
        f"{span_days // max(1, len(dates) - 1)} days)"
    )


# ── Phase 2e: build briefs ────────────────────────────────────────────────

def _build_cluster_brief(
    meta: dict,
    event_records: list[dict],
    participants: list[dict],
    cadence: str,
) -> str:
    """Compact JSON string. Truncate at MAX_EVENTS_PER_CLUSTER_BRIEF to
    keep the prompt under nano's nominal limit even on giant clusters."""
    truncated_events = event_records[:MAX_EVENTS_PER_CLUSTER_BRIEF]
    payload = {
        "canonical_label": meta["canonical_label"],
        "normalized_label": meta["normalized_label"],
        "action": meta["action"],
        "cluster_size": len(event_records),
        "events_shown": len(truncated_events),
        "events_total_omitted": max(0, len(event_records) - len(truncated_events)),
        "events": [
            {
                "id_prefix": (e.get("id") or "")[:8],
                "label": e.get("label"),
                "start_date": e.get("start_date"),
                "edges": [
                    {
                        "direction": x.get("direction"),
                        "relationship": x.get("relationship"),
                        "other_label": x.get("other_label"),
                        "sentence": x.get("sentence"),
                    }
                    for x in (e.get("edges") or [])
                ],
            }
            for e in truncated_events
        ],
        "cross_instance_participants_precomputed": participants,
        "cadence_note": cadence,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_existing_entity_brief(entity_record: Optional[dict]) -> str:
    if entity_record is None:
        return json.dumps({"present": False}, ensure_ascii=False)
    payload = {
        "present": True,
        "id_prefix": (entity_record.get("id") or "")[:8],
        "label": entity_record.get("label"),
        "description": entity_record.get("description"),
        "edges": [
            {
                "direction": x.get("direction"),
                "relationship": x.get("relationship"),
                "other_label": x.get("other_label"),
                "sentence": x.get("sentence"),
            }
            for x in (entity_record.get("edges") or [])
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ── Phase 2f: invoke investigator ─────────────────────────────────────────

def _investigate(cluster_brief: str, existing_entity_brief: str, scope_context) -> Optional[dict]:
    """Call kg_maintenance::series_link_investigator. Returns the agent's
    output dict or None on failure."""
    try:
        agent = DI.agent_factory.create_agent("kg_maintenance::series_link_investigator")
        if agent is None:
            logger.warning("[series_link_investigate] investigator unavailable")
            return None
        response = agent.action_handler(
            Message(
                agent_input={
                    "cluster_brief": cluster_brief,
                    "existing_entity_brief": existing_entity_brief,
                },
                scope_context=scope_context,
            )
        )
    except Exception as exc:
        logger.warning("[series_link_investigate] agent raised: %s", exc)
        return None

    data = response.data if response and hasattr(response, "data") else {}
    if hasattr(data, "model_dump"):
        data = data.model_dump()
    if not isinstance(data, dict):
        return None
    return data


# ── Phase 2g: apply verdict ───────────────────────────────────────────────

def _apply_verdict(
    findings: list[dict],
    verdict: str,
    investigation_report: dict,
) -> None:
    """Update every finding in the cluster to approved/rejected with the
    investigator's report in investigation_report_json. Single transaction."""
    if verdict not in {"approved", "rejected"}:
        raise ValueError(f"unexpected verdict {verdict!r}")
    session = get_session()
    try:
        finding_ids = [f["id"] for f in findings]
        rows = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.id.in_(finding_ids))
            .all()
        )
        now = datetime.now(timezone.utc)
        for r in rows:
            r.status = verdict
            r.investigation_report_json = investigation_report
            r.investigated_at = now
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Entry point ───────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> dict:
    """Returns {clusters_seen, approved, rejected, errors}."""
    logger.info("[series_link_investigate] Starting run_id=%s", ctx.run_id)

    scope_context = build_pipeline_scope_context(
        pipeline_id=ctx.pipeline_id,
        actor_id="kg_maintenance_runner",
    )

    clusters = _load_pending_clusters()
    if not clusters:
        return {"clusters_seen": 0, "approved": 0, "rejected": 0, "errors": 0}

    approved = 0
    rejected = 0
    errors = 0

    for cluster_id, findings in clusters.items():
        try:
            meta = _cluster_meta(findings)
            event_ids = meta["all_event_ids"]
            entity_id = (
                findings[0]["primary_node_id"]
                if meta["action"] == "link_events_to_entity"
                else None
            )

            event_records, entity_record = _load_node_context(event_ids, entity_id)
            participants = _aggregate_participants(event_records)
            cadence = _cadence_signal(event_records)

            cluster_brief = _build_cluster_brief(meta, event_records, participants, cadence)
            existing_entity_brief = _build_existing_entity_brief(entity_record)

            data = _investigate(cluster_brief, existing_entity_brief, scope_context)
            if data is None:
                errors += 1
                continue

            verdict_raw = str(data.get("verdict") or "").strip().lower()
            reasoning = (data.get("reasoning") or "").strip()
            refined_label = (data.get("canonical_label") or "").strip()
            cross_participants = data.get("cross_instance_participants") or []
            cadence_note = (data.get("cadence_note") or "").strip()

            investigation_report = {
                "agent": "kg_maintenance::series_link_investigator",
                "verdict": verdict_raw,
                "reasoning": reasoning,
                "canonical_label": refined_label or meta["canonical_label"],
                "cross_instance_participants": cross_participants,
                "cadence_note": cadence_note,
                "cluster_id": cluster_id,
                "events_in_cluster": len(event_records),
                "investigated_at": datetime.now(timezone.utc).isoformat(),
            }

            if verdict_raw == "approve":
                _apply_verdict(findings, "approved", investigation_report)
                approved += 1
            elif verdict_raw == "reject":
                _apply_verdict(findings, "rejected", investigation_report)
                rejected += 1
            else:
                logger.warning(
                    "[series_link_investigate] cluster_id=%s got unexpected verdict %r — leaving pending",
                    cluster_id, verdict_raw,
                )
                errors += 1
        except Exception as exc:
            logger.warning(
                "[series_link_investigate] cluster_id=%s failed: %s", cluster_id, exc,
                exc_info=True,
            )
            errors += 1
            continue

    result = {
        "clusters_seen": len(clusters),
        "approved": approved,
        "rejected": rejected,
        "errors": errors,
    }
    logger.info("[series_link_investigate] Done: %s", result)
    return result
