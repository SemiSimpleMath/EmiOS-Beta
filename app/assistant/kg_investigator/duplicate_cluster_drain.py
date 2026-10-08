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

import hashlib
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_

from app.assistant.kg import failure_ledger
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.identity_names import PRINCIPAL_USER
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)

AGENT_NAME = "kg_maintenance::duplicate_cluster_resolver"
ACTOR = "kg_maintenance::duplicate_cluster_resolver"
LEDGER_KIND = "kg_duplicate_cluster"

MAX_NODE_EDGE_SENTENCES = 4


def _cluster_key(cluster: Dict[str, Any]) -> str:
    """Ledger id of a cluster: its node set, so a cluster that gains or loses a
    node is a new question."""
    return hashlib.sha1("|".join(cluster["node_ids"]).encode("utf-8")).hexdigest()


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

def _coverage_problems(data: Dict[str, Any], node_ids: List[str]) -> List[str]:
    """Where an answer breaks the coverage contract: every node id shown appears
    exactly once across the merge groups and distinct_node_ids, only those ids,
    and every merge group names at least one duplicate. Empty when complete."""
    node_set = set(node_ids)
    named: List[str] = []
    problems: List[str] = []
    for g in (data.get("merge_groups") or []):
        canon = g.get("canonical_node_id")
        dups = list(g.get("duplicate_node_ids") or [])
        if not dups:
            problems.append(f"the merge group for {canon} names no duplicate_node_ids")
        named.append(canon)
        named.extend(dups)
    named.extend(data.get("distinct_node_ids") or [])
    unknown = sorted({str(n) for n in named if n not in node_set})
    if unknown:
        problems.append(f"not ids of this cluster: {unknown}")
    repeated = sorted({n for n in named if n in node_set and named.count(n) > 1})
    if repeated:
        problems.append(f"placed more than once: {repeated}")
    missing = [n for n in node_ids if n not in named]
    if missing:
        problems.append(f"not placed: {missing}")
    return problems


def resolve_one_cluster(cluster: Dict[str, Any], *, scope_context, dry_run: bool) -> Dict[str, Any]:
    """Adjudicate one cluster with the LLM; validate coverage; apply unless dry_run.

    A node the answer leaves out is never taken as 'distinct': an incomplete
    answer gets one correction naming what is missing, and raises if it is
    still incomplete."""
    node_ids: List[str] = cluster["node_ids"]
    node_data = _load_node_brief_data(node_ids)
    brief = _serialize_cluster(node_ids, node_data)

    agent = DI.agent_factory.create_agent(AGENT_NAME)
    if agent is None:
        raise RuntimeError(f"could not create agent {AGENT_NAME}")

    def call(agent_input: Dict[str, Any]) -> Dict[str, Any]:
        response = agent.action_handler(
            Message(agent_input=agent_input, scope_context=scope_context)
        )
        return getattr(response, "data", None) or {}

    payload = {"cluster_brief": brief}
    data = call(payload)
    problems = _coverage_problems(data, node_ids)
    if problems:
        logger.warning("[dup_cluster] incomplete answer, one correction: %s", problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _coverage_problems(data, node_ids)
        if problems:
            raise ValueError(
                f"duplicate cluster answer still incomplete after one correction: {problems}"
            )

    merge_groups: List[Tuple[str, List[str], str]] = [
        (g["canonical_node_id"], list(g["duplicate_node_ids"]), str(g.get("reason") or ""))
        for g in (data.get("merge_groups") or [])
    ]
    distinct_ids = list(data.get("distinct_node_ids") or [])

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
    """Merge true duplicates (revertible), record distinct verdicts, close findings.

    Every merge group of the cluster commits in one transaction: a failure
    rolls the whole cluster back and leaves its findings pending."""
    from app.assistant.kg.db.knowledge_graph_db import Node
    from app.assistant.kg_core.kg_utils.node_merge import (
        merge_nodes_in_session,
        snapshot_node,
    )
    from app.assistant.kg_maintenance.store import set_status
    from app.assistant.kg_maintenance.verdict_store import record_distinct_pairs_bulk
    from app.assistant.pipelines.kg_shared.merge_utils import (
        merge_node_fields_into_existing,
        node_to_merge_dict,
    )

    remap: Dict[str, str] = {}  # loser_id -> surviving winner_id
    merges_done = 0

    session = get_session()
    try:
        for (winner_id, loser_ids, reason) in merge_groups:
            winner = session.query(Node).filter_by(id=winner_id).first()
            if winner is None:
                # Closing this group's findings would record its pairs as
                # distinct, a verdict nobody made.
                raise RuntimeError(f"merge winner {winner_id} no longer exists")
            for loser_id in loser_ids:
                loser = session.query(Node).filter_by(id=loser_id).first()
                if loser is None:
                    remap[loser_id] = winner_id  # already gone; treat as merged
                    continue
                winner_pre = snapshot_node(winner)
                merge_node_fields_into_existing(winner, node_to_merge_dict(loser))
                session.flush()
                merge_nodes_in_session(
                    session,
                    loser_node=loser,
                    winner_node=winner,
                    merge_actor=ACTOR,
                    notes=f"cluster merge: {reason}",
                    winner_pre_snapshot=winner_pre,
                )
                remap[loser_id] = winner_id
                merges_done += 1
        session.commit()
    except Exception:
        session.rollback()
        logger.error("[dup_cluster] cluster merge failed; rolled back", exc_info=True)
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
    default — produces the full verdict report without writing to the KG.

    The first cluster that fails stops the run: its failure is counted in
    the KG failure ledger (keyed by the cluster's node set; three failed runs
    set it aside) and the exception propagates; later clusters stay pending."""
    scope = _resolver_scope()
    clusters = build_duplicate_clusters()
    total = len(clusters)
    set_aside = failure_ledger.set_aside_ids(LEDGER_KIND)
    clusters = [c for c in clusters if _cluster_key(c) not in set_aside]
    if limit:
        clusters = clusters[:limit]

    reports: List[Dict[str, Any]] = []
    for i, cluster in enumerate(clusters):
        key = _cluster_key(cluster)
        try:
            rep = resolve_one_cluster(cluster, scope_context=scope, dry_run=dry_run)
        except Exception as exc:
            failure_ledger.record_failure(
                LEDGER_KIND, key, exc,
                label=f"duplicate cluster of nodes {', '.join(cluster['node_ids'])}",
            )
            raise
        failure_ledger.clear(LEDGER_KIND, key)
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
