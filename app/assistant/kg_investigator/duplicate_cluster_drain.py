"""
Cluster-level resolution of the duplicate_node finding backlog.

Why this exists
---------------
``finding_processor.drain_pending_findings`` investigates ONE finding (one
node pair) per full ``kg_investigation`` manager loop — ~4.5 min each. With
~1400 pending ``duplicate_node`` findings that is ~100h of serial work, so the
backlog never drains. But those pairs form only ~290 connected components in
the duplicate graph, and most components are recurring-event occurrences whose
correct verdict is "distinct, keep separate" — a cheap, cluster-level judgment.

This module:
  1. Union-finds the pending ``duplicate_node`` findings into components.
  2. Asks one ``kg_maintenance::duplicate_cluster_resolver`` agent per
     component to partition it into true-duplicate groups (merge) vs distinct
     nodes (keep separate). The brief surfaces each node's date so the agent
     can apply the recurring-occurrence rule (same label, different date ->
     distinct).
  3. Applies the verdict:
       - distinct pairs -> ``record_distinct_pairs_bulk`` so the scanner's
         ``load_distinct_pairs`` short-circuit never re-compares them, and the
         finding is closed ``dismissed``.
       - true duplicates -> merged via the revertible ``node_merge`` path
         (``kg_merge_log`` / unmerge), and the finding is closed ``executed``.

Run ``drain_duplicate_clusters(dry_run=True)`` first to review the verdicts
before anything writes to the real KG.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.identity_names import PRINCIPAL_USER
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)

AGENT_NAME = "kg_maintenance::duplicate_cluster_resolver"
ACTOR = "kg_maintenance::duplicate_cluster_resolver"

MAX_NODE_EDGE_SENTENCES = 4


def _resolver_scope():
    """Scope context for the cluster-resolver agent call (strict-scope mode
    requires every agent ingress to carry one). Mirrors the investigator's
    subsystem scope."""
    return load_scope_for_source(
        kind="subsystem",
        source_id="kg_investigator",
        actor_id="kg_dup_cluster_resolver",
        identity_overrides={
            "owner_id": PRINCIPAL_USER,
            "actor_id": "kg_dup_cluster_resolver",
            "scope_id": "scope::kg_investigator::dup_cluster_resolver",
        },
    )


def _node_to_dict(node) -> Dict[str, Any]:
    """Plain dict of the merge-relevant fields for merge_node_fields_into_existing."""
    return {
        "label": node.label,
        "node_type": node.node_type,
        "aliases": node.aliases or [],
        "hash_tags": node.hash_tags or [],
        "semantic_label": node.semantic_label,
        "goal_status": node.goal_status,
        "valid_during": node.valid_during,
        "category": node.category,
        "start_date": node.start_date,
        "end_date": node.end_date,
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
        "confidence": node.confidence,
        "importance": node.importance,
    }


# ── Clustering ────────────────────────────────────────────────────────────────

def build_duplicate_clusters() -> List[Dict[str, Any]]:
    """Union-find the pending duplicate_node findings into connected components.

    Returns a list of clusters (biggest first), each:
      {"node_ids": [sorted ids], "findings": [(finding_id, primary, secondary), ...]}
    """
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding

    session = get_session()
    try:
        rows = (
            session.query(
                KGMaintenanceFinding.id,
                KGMaintenanceFinding.primary_node_id,
                KGMaintenanceFinding.secondary_node_id,
            )
            .filter(KGMaintenanceFinding.finding_type == "duplicate_node")
            .filter(KGMaintenanceFinding.status == "pending")
            .all()
        )
    finally:
        session.close()

    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    findings: List[Tuple[str, str, str]] = []
    for fid, p, s in rows:
        if not p:
            continue
        find(p)
        if s:
            find(s)
            union(p, s)
        findings.append((fid, p, s or ""))

    comp_nodes: Dict[str, set] = defaultdict(set)
    for n in list(parent.keys()):
        comp_nodes[find(n)].add(n)
    comp_findings: Dict[str, list] = defaultdict(list)
    for fid, p, s in findings:
        comp_findings[find(p)].append((fid, p, s))

    clusters = [
        {"node_ids": sorted(nodes), "findings": comp_findings.get(root, [])}
        for root, nodes in comp_nodes.items()
    ]
    clusters.sort(key=lambda c: len(c["node_ids"]), reverse=True)
    return clusters


def _load_node_brief_data(node_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per-node fields the adjudicator needs, plus a few edge sentences each."""
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node

    session = get_session()
    try:
        nodes = session.query(Node).filter(Node.id.in_(node_ids)).all()
        out: Dict[str, Dict[str, Any]] = {}
        for n in nodes:
            out[str(n.id)] = {
                "label": n.label or "",
                "node_type": n.node_type or "",
                "category": n.category or "",
                "start_date": n.start_date,
                "original_sentence": (n.original_sentence or ""),
                "observation_count": getattr(n, "observation_count", None),
                "edge_sentences": [],
            }
        edges = (
            session.query(Edge.source_id, Edge.target_id, Edge.sentence)
            .filter(or_(Edge.source_id.in_(node_ids), Edge.target_id.in_(node_ids)))
            .all()
        )
        for src, tgt, sent in edges:
            if not sent or not sent.strip():
                continue
            for nid in (str(src), str(tgt)):
                d = out.get(nid)
                if d is not None and len(d["edge_sentences"]) < MAX_NODE_EDGE_SENTENCES:
                    d["edge_sentences"].append(sent.strip())
    finally:
        session.close()
    return out


def _serialize_cluster(node_ids: List[str], node_data: Dict[str, Dict[str, Any]]) -> str:
    """Render the cluster as a compact, prompt-friendly brief. Full node ids so
    the agent echoes them back exactly."""
    lines = [f"Cluster of {len(node_ids)} candidate-duplicate nodes:", ""]
    for nid in node_ids:
        d = node_data.get(nid, {})
        lines.append(f"- node_id: {nid}")
        lines.append(
            f"  label: {d.get('label', '')!r} | type: {d.get('node_type', '')} "
            f"| category: {d.get('category', '')}"
        )
        date = d.get("start_date")
        if date:
            lines.append(f"  date: {str(date)[:10]}")
        if d.get("original_sentence"):
            lines.append(f"  sentence: {d['original_sentence'][:160]}")
        for s in (d.get("edge_sentences") or [])[:MAX_NODE_EDGE_SENTENCES]:
            lines.append(f"  edge: {s[:140]}")
        lines.append("")
    return "\n".join(lines)


# ── Resolution ────────────────────────────────────────────────────────────────

def resolve_one_cluster(cluster: Dict[str, Any], *, scope_context, dry_run: bool) -> Dict[str, Any]:
    """Adjudicate one cluster with the LLM; validate coverage; apply unless dry_run."""
    node_ids: List[str] = cluster["node_ids"]
    node_data = _load_node_brief_data(node_ids)
    brief = _serialize_cluster(node_ids, node_data)

    agent = DI.agent_factory.create_agent(AGENT_NAME)
    if agent is None:
        raise RuntimeError(f"could not create agent {AGENT_NAME}")
    response = agent.action_handler(
        Message(agent_input={"cluster_brief": brief}, scope_context=scope_context)
    )
    data = getattr(response, "data", None) or {}

    node_set = set(node_ids)
    assigned: set = set()
    merge_groups: List[Tuple[str, List[str], str]] = []

    for g in (data.get("merge_groups") or []):
        if not isinstance(g, dict):
            continue
        canon = g.get("canonical_node_id")
        if canon not in node_set or canon in assigned:
            continue
        dups = [
            d for d in (g.get("duplicate_node_ids") or [])
            if d in node_set and d != canon and d not in assigned
        ]
        if not dups:
            continue
        merge_groups.append((canon, dups, str(g.get("reason") or "")))
        assigned.add(canon)
        assigned.update(dups)

    distinct_ids = [
        d for d in (data.get("distinct_node_ids") or [])
        if d in node_set and d not in assigned
    ]
    assigned.update(distinct_ids)

    # Coverage: any node the agent failed to mention sinks to 'distinct', never
    # silently dropped (deterministic-proposes-LLM-decides: a miss must sink).
    missing = [n for n in node_ids if n not in assigned]
    if missing:
        logger.warning(
            "[dup_cluster] %d node(s) uncovered by agent -> kept distinct: %s",
            len(missing), missing,
        )
        distinct_ids.extend(missing)

    report: Dict[str, Any] = {
        "node_count": len(node_ids),
        "finding_count": len(cluster["findings"]),
        "merge_groups": [
            {
                "winner": w,
                "winner_label": node_data.get(w, {}).get("label"),
                "losers": losers,
                "loser_labels": [node_data.get(x, {}).get("label") for x in losers],
                "reason": reason,
            }
            for (w, losers, reason) in merge_groups
        ],
        "distinct_count": len(distinct_ids),
        "reasoning": str(data.get("reasoning") or ""),
        "applied": None,
    }
    if not dry_run:
        report["applied"] = _apply_resolution(cluster, merge_groups, node_data)
    return report


def _apply_resolution(
    cluster: Dict[str, Any],
    merge_groups: List[Tuple[str, List[str], str]],
    node_data: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge true duplicates (revertible), record distinct verdicts, close findings."""
    from app.assistant.kg.db.knowledge_graph_db import Node
    from app.assistant.kg_core.kg_utils.node_merge import (
        merge_nodes_in_session,
        snapshot_node,
    )
    from app.assistant.kg_maintenance.store import set_status
    from app.assistant.kg_maintenance.verdict_store import record_distinct_pairs_bulk
    from app.assistant.pipelines.kg_shared.merge_utils import (
        merge_node_fields_into_existing,
    )

    remap: Dict[str, str] = {}  # loser_id -> surviving winner_id
    merges_done = 0

    for (winner_id, loser_ids, reason) in merge_groups:
        session = get_session()
        try:
            winner = session.query(Node).filter_by(id=winner_id).first()
            if winner is None:
                logger.warning("[dup_cluster] winner %s vanished; skipping group", winner_id)
                continue
            for loser_id in loser_ids:
                loser = session.query(Node).filter_by(id=loser_id).first()
                if loser is None:
                    remap[loser_id] = winner_id  # already gone; treat as merged
                    continue
                winner_pre = snapshot_node(winner)
                merge_node_fields_into_existing(winner, _node_to_dict(loser))
                session.flush()
                merge_nodes_in_session(
                    session,
                    loser_node=loser,
                    winner_node=winner,
                    merge_actor=ACTOR,
                    notes=f"cluster merge: {reason}"[:300],
                    winner_pre_snapshot=winner_pre,
                )
                remap[loser_id] = winner_id
                merges_done += 1
            session.commit()
        except Exception:
            session.rollback()
            logger.error("[dup_cluster] merge group winner=%s failed", winner_id, exc_info=True)
            raise
        finally:
            session.close()

    # Close findings: remap endpoints through the merges. A pair that collapsed
    # to one node was a real duplicate -> executed; everything else is distinct.
    distinct_pairs: List[Tuple[str, str]] = []
    executed = dismissed = 0
    for (fid, p, s) in cluster["findings"]:
        rp = remap.get(p, p)
        rs = remap.get(s, s) if s else ""
        if s and rp == rs:
            set_status(fid, "executed", executed_by=ACTOR,
                       execution_notes="Merged via duplicate_cluster_resolver.")
            executed += 1
        else:
            if s:
                distinct_pairs.append((rp, rs))
            set_status(fid, "dismissed", executed_by=ACTOR,
                       execution_notes="duplicate_cluster_resolver: distinct nodes, kept separate.")
            dismissed += 1

    recorded = 0
    if distinct_pairs:
        recorded = record_distinct_pairs_bulk(
            distinct_pairs,
            decided_by=ACTOR,
            memo="duplicate_cluster_resolver: distinct nodes kept separate",
        )

    return {
        "merges": merges_done,
        "findings_executed": executed,
        "findings_dismissed": dismissed,
        "distinct_verdicts_recorded": recorded,
    }


def drain_duplicate_clusters(*, limit: Optional[int] = None, dry_run: bool = True) -> Dict[str, Any]:
    """Resolve up to ``limit`` duplicate clusters (all if None). Dry-run by
    default — produces the full verdict report without writing to the KG."""
    scope = _resolver_scope()
    clusters = build_duplicate_clusters()
    total = len(clusters)
    if limit:
        clusters = clusters[:limit]

    reports: List[Dict[str, Any]] = []
    for i, cluster in enumerate(clusters):
        try:
            rep = resolve_one_cluster(cluster, scope_context=scope, dry_run=dry_run)
        except Exception:
            logger.error("[dup_cluster] cluster %d failed", i, exc_info=True)
            continue
        reports.append(rep)
        logger.info(
            "[dup_cluster] cluster %d/%d nodes=%d merge_groups=%d distinct=%d%s",
            i + 1, len(clusters), rep["node_count"], len(rep["merge_groups"]),
            rep["distinct_count"], " (dry-run)" if dry_run else "",
        )

    return {
        "dry_run": dry_run,
        "total_clusters": total,
        "clusters_processed": len(reports),
        "nodes": sum(r["node_count"] for r in reports),
        "findings": sum(r["finding_count"] for r in reports),
        "merge_groups": sum(len(r["merge_groups"]) for r in reports),
        "nodes_to_merge": sum(
            sum(len(g["losers"]) for g in r["merge_groups"]) for r in reports
        ),
        "nodes_kept_distinct": sum(r["distinct_count"] for r in reports),
        "reports": reports,
    }
