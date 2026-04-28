"""
/kg-proposals — Review UI for the shadow KG (claim_proposals layer).

GET  /kg-proposals/                  — list view with status filters + trigger buttons
GET  /kg-proposals/<id>              — detail view with graph viz + evidence
POST /kg-proposals/run-pipeline      — trigger kg_chat_pipeline_parallel routine
POST /kg-proposals/run-promoter      — run the promoter (dry-run default; ?commit=1 to apply)
POST /kg-proposals/<id>/approve      — promote a single proposal manually (TODO)
POST /kg-proposals/<id>/reject       — retract a proposal
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import desc, func

from app.assistant.database.claim_proposals import (
    ClaimProposal, ClaimProposalEdge, ClaimProposalEvidence, ClaimProposalNode,
)
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.kg.proposal_promoter import run_promoter
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

kg_proposals_bp = Blueprint("kg_proposals", __name__, url_prefix="/kg-proposals")


NEIGHBOR_LIMIT = 6


@kg_proposals_bp.route("/", methods=["GET"])
def list_view():
    status_filter = request.args.get("status", "pending")

    session = get_session()
    try:
        counts = _status_counts(session)
        q = session.query(ClaimProposal)
        if status_filter != "all":
            q = q.filter(ClaimProposal.status == status_filter)
        proposals = (
            q.order_by(desc(ClaimProposal.last_observed_at), desc(ClaimProposal.created_at))
            .limit(200).all()
        )
        rows = [_list_row(session, p) for p in proposals]
    finally:
        session.close()

    return render_template(
        "kg_proposals_list.html",
        counts=counts,
        status_filter=status_filter,
        proposals=rows,
    )


@kg_proposals_bp.route("/<proposal_id>", methods=["GET"])
def detail_view(proposal_id: str):
    session = get_session()
    try:
        p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
        if p is None:
            return render_template("kg_proposals_detail.html", not_found=True), 404

        nodes = (
            session.query(ClaimProposalNode)
            .filter(ClaimProposalNode.proposal_id == proposal_id)
            .order_by(ClaimProposalNode.created_at.asc())
            .all()
        )
        edges = (
            session.query(ClaimProposalEdge)
            .filter(ClaimProposalEdge.proposal_id == proposal_id)
            .order_by(ClaimProposalEdge.created_at.asc())
            .all()
        )
        evidence = (
            session.query(ClaimProposalEvidence)
            .filter(ClaimProposalEvidence.proposal_id == proposal_id)
            .order_by(ClaimProposalEvidence.observed_at.asc())
            .all()
        )

        nodes_data = [_node_row(session, n) for n in nodes]
        edges_data = [_edge_row(e, nodes) for e in edges]
        evidence_data = [_evidence_row(e) for e in evidence]
        # Load full window items + resolved text per evidence row so the
        # detail page can expand the entire originating chat on demand.
        for ev_row, ev_out in zip(evidence, evidence_data):
            ev_out["window_items"] = _load_window_items(session, ev_row.window_id)
            ev_out["window_resolved_text"] = _load_window_resolved(
                session, ev_row.window_id,
            )
        mermaid = _build_mermaid(session, p, nodes, edges)

        ctx = {
            "proposal": _detail_row(p, len(nodes), len(edges)),
            "nodes": nodes_data,
            "edges": edges_data,
            "evidence": evidence_data,
            "mermaid": mermaid,
        }
    finally:
        session.close()

    return render_template("kg_proposals_detail.html", not_found=False, **ctx)


@kg_proposals_bp.route("/run-pipeline", methods=["POST"])
def run_pipeline():
    """Run the kg_pipeline routine synchronously and return a concrete
    before/after diff. Pipeline workers are capped per cycle so this never
    runs unbounded.
    """
    import time
    from sqlalchemy import func as sa_func

    def snapshot(session):
        snap = {}
        snap["proposals"] = session.query(sa_func.count(ClaimProposal.id)).scalar() or 0
        # Counts across the new bucket-per-stage tables.
        from app.assistant.database.kg_pipeline_models import (
            KGResolvedMessage, KGWindow, KGWindowExtraction, KGWindowEnrichment,
        )
        snap["resolved_messages"] = session.query(sa_func.count(KGResolvedMessage.id)).scalar() or 0
        snap["windows"] = session.query(sa_func.count(KGWindow.id)).scalar() or 0
        snap["extractions"] = session.query(sa_func.count(KGWindowExtraction.id)).scalar() or 0
        snap["enrichments"] = session.query(sa_func.count(KGWindowEnrichment.id)).scalar() or 0
        rows = (
            session.query(
                KGWindowExtraction.verdict,
                sa_func.count(KGWindowExtraction.id),
            )
            .group_by(KGWindowExtraction.verdict).all()
        )
        snap["extraction_verdict_counts"] = {s: n for s, n in rows}
        return snap

    try:
        from app.assistant.routine_manager.routine_manager import get_routine_manager
        rm = get_routine_manager()

        session = get_session()
        try:
            before = snapshot(session)
        finally:
            session.close()

        t0 = time.time()
        try:
            rm.run_routine_now("kg_pipeline")
            routine_error = None
        except Exception as exc:
            routine_error = str(exc)
            logger.exception("[kg_proposals] pipeline routine raised")
        elapsed = round(time.time() - t0, 2)

        session = get_session()
        try:
            after = snapshot(session)
        finally:
            session.close()

        proposals_diff = after["proposals"] - before["proposals"]
        verdict_changes = []
        before_v = before.get("extraction_verdict_counts", {})
        after_v = after.get("extraction_verdict_counts", {})
        for verdict in set(before_v) | set(after_v):
            b = before_v.get(verdict, 0)
            a = after_v.get(verdict, 0)
            if a != b:
                verdict_changes.append({"verdict": verdict, "before": b, "after": a, "delta": a - b})

        return jsonify({
            "ok": routine_error is None,
            "elapsed_seconds": elapsed,
            "proposals_added": proposals_diff,
            "extraction_verdict_changes": verdict_changes,
            "before_totals": before,
            "after_totals": after,
            "error": routine_error,
        })
    except Exception as exc:
        logger.exception("[kg_proposals] pipeline trigger failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@kg_proposals_bp.route("/run-promoter", methods=["POST"])
def run_promoter_route():
    commit = request.args.get("commit", "0") == "1"
    limit = int(request.args.get("limit", "100"))
    stats = run_promoter(limit=limit, commit=commit)
    stats.pop("_samples", None)
    return jsonify({"ok": True, "mode": "commit" if commit else "dry_run", "stats": stats})


@kg_proposals_bp.route("/<proposal_id>.md", methods=["GET"])
def export_markdown(proposal_id: str):
    """Return a portable markdown representation of the proposal group —
    embedded Mermaid diagram + metadata + evidence. Paste into notes, docs,
    anywhere that renders Mermaid natively (GitHub, Obsidian, etc.)."""
    from flask import Response

    session = get_session()
    try:
        p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
        if p is None:
            return Response("Not found", status=404, mimetype="text/plain")
        nodes = (
            session.query(ClaimProposalNode)
            .filter(ClaimProposalNode.proposal_id == proposal_id)
            .order_by(ClaimProposalNode.created_at.asc()).all()
        )
        edges = (
            session.query(ClaimProposalEdge)
            .filter(ClaimProposalEdge.proposal_id == proposal_id)
            .order_by(ClaimProposalEdge.created_at.asc()).all()
        )
        evidence = (
            session.query(ClaimProposalEvidence)
            .filter(ClaimProposalEvidence.proposal_id == proposal_id)
            .order_by(ClaimProposalEvidence.observed_at.asc()).all()
        )
        mermaid = _build_mermaid(session, p, nodes, edges)

    finally:
        session.close()

    # Build per-node and per-edge summaries for markdown readers.
    by_pid = {n.id: n for n in nodes}
    node_lines = []
    for n in nodes:
        extra = []
        if n.valid_from:
            extra.append(f"valid_from={n.valid_from}")
        if n.resolution_action and n.resolution_action != "pending":
            extra.append(f"resolution={n.resolution_action}")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        node_lines.append(f"- **[{n.node_type}]** `{n.label}`{suffix}")

    edge_lines = []
    for e in edges:
        src = by_pid.get(e.source_node_id)
        tgt = by_pid.get(e.target_node_id)
        src_lbl = src.label if src else "?"
        tgt_lbl = tgt.label if tgt else "?"
        edge_lines.append(f"- `{src_lbl}` — **{e.predicate}** → `{tgt_lbl}`")

    ev_lines = []
    for ev in evidence:
        who = f"{ev.speaker_name or '?'} ({ev.speaker_role or '?'})"
        where = ev.room_id or ev.source_type
        when = ev.observed_at or ev.created_at
        ev_lines.append(
            f"- **{when}** — {who} in `{where}`:\n"
            f"  > {ev.raw_text or '(no text)'}"
        )

    md = (
        f"# Proposal `{p.id[:8]}` — {p.status}\n\n"
        f"**{p.representative_sentence or '(no sentence)'}**\n\n"
        f"| Field | Value |\n"
        f"|-------|-------|\n"
        f"| id | `{p.id}` |\n"
        f"| status | {p.status} |\n"
        f"| claim_type | {p.claim_type} |\n"
        f"| observation_count | {p.observation_count} |\n"
        f"| first_observed_at | {_fmt_ts(p.first_observed_at) or '—'} |\n"
        f"| last_observed_at | {_fmt_ts(p.last_observed_at) or '—'} |\n"
        f"| created_at | {_fmt_ts(p.created_at) or '—'} |\n"
        f"\n## Structure\n\n"
        f"```mermaid\n{mermaid}\n```\n\n"
        f"### Nodes ({len(nodes)})\n\n"
        + "\n".join(node_lines)
        + f"\n\n### Edges ({len(edges)})\n\n"
        + "\n".join(edge_lines)
        + f"\n\n## Evidence ({len(evidence)})\n\n"
        + "\n".join(ev_lines)
        + "\n"
    )
    return Response(md, mimetype="text/markdown; charset=utf-8")


@kg_proposals_bp.route("/<proposal_id>/reject", methods=["POST"])
def reject(proposal_id: str):
    reason = None
    if request.is_json:
        reason = (request.json or {}).get("reason")
    session = get_session()
    try:
        p = session.query(ClaimProposal).filter(ClaimProposal.id == proposal_id).first()
        if p is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        p.status = "retracted"
        p.retracted_at = datetime.utcnow()
        p.retraction_reason = reason or "rejected via UI"
        session.commit()
        return jsonify({"ok": True})
    finally:
        session.close()


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------

def _status_counts(session) -> Dict[str, int]:
    rows = (
        session.query(ClaimProposal.status, func.count(ClaimProposal.id))
        .group_by(ClaimProposal.status).all()
    )
    return {s: n for s, n in rows}


def _list_row(session, p: ClaimProposal) -> Dict[str, Any]:
    n_nodes = session.query(func.count(ClaimProposalNode.id)).filter(
        ClaimProposalNode.proposal_id == p.id).scalar() or 0
    n_edges = session.query(func.count(ClaimProposalEdge.id)).filter(
        ClaimProposalEdge.proposal_id == p.id).scalar() or 0
    return {
        "id": p.id,
        "short_id": p.id[:8],
        "status": p.status,
        "claim_type": p.claim_type,
        "representative_sentence": p.representative_sentence,
        "observation_count": p.observation_count,
        "n_nodes": n_nodes,
        "n_edges": n_edges,
        "last_observed_at": _fmt_ts(p.last_observed_at),
        "created_at": _fmt_ts(p.created_at),
    }


def _detail_row(p: ClaimProposal, n_nodes: int, n_edges: int) -> Dict[str, Any]:
    return {
        "id": p.id,
        "short_id": p.id[:8],
        "status": p.status,
        "claim_type": p.claim_type,
        "representative_sentence": p.representative_sentence,
        "observation_count": p.observation_count,
        "n_nodes": n_nodes,
        "n_edges": n_edges,
        "first_observed_at": _fmt_ts(p.first_observed_at),
        "last_observed_at": _fmt_ts(p.last_observed_at),
        "created_at": _fmt_ts(p.created_at),
        "retracted_at": _fmt_ts(p.retracted_at),
        "retraction_reason": p.retraction_reason,
    }


def _node_row(session, n: ClaimProposalNode) -> Dict[str, Any]:
    row = {
        "id": n.id,
        "short_id": n.id[:8],
        "extractor_temp_id": n.extractor_temp_id,
        "label": n.label,
        "node_type": n.node_type,
        "category": n.category,
        "description_draft": n.description_draft,
        "valid_from": _fmt_ts(n.valid_from),
        "valid_to": _fmt_ts(n.valid_to),
        "valid_from_prose": n.valid_from_prose,
        "valid_to_prose": n.valid_to_prose,
        "resolved_node_id": n.resolved_node_id,
        "resolution_action": n.resolution_action,
        "resolved_link": None,
        "resolved_label": None,
        "resolved_locked": False,
    }
    if n.resolved_node_id:
        kg = session.query(Node).filter(Node.id == n.resolved_node_id).first()
        if kg:
            row["resolved_link"] = f"/kg/node/{kg.id}"
            row["resolved_label"] = kg.label
            row["resolved_locked"] = kg.locked_by_user_at is not None
    return row


def _edge_row(e: ClaimProposalEdge, nodes: List[ClaimProposalNode]) -> Dict[str, Any]:
    by_id = {n.id: n for n in nodes}
    src = by_id.get(e.source_node_id)
    tgt = by_id.get(e.target_node_id)
    return {
        "id": e.id,
        "short_id": e.id[:8],
        "predicate": e.predicate,
        "bidirectional": e.bidirectional,
        "sentence": e.sentence,
        "source_label": src.label if src else "?",
        "target_label": tgt.label if tgt else "?",
        "resolved_edge_id": e.resolved_edge_id,
    }


def _evidence_row(e: ClaimProposalEvidence) -> Dict[str, Any]:
    return {
        "id": e.id,
        "source_type": e.source_type,
        "room_id": e.room_id,
        "room_surface": e.room_surface,
        "speaker_name": e.speaker_name,
        "speaker_role": e.speaker_role,
        "window_id": e.window_id,
        "window_short": e.window_id[:8] if e.window_id else None,
        "raw_text": e.raw_text,
        "extractor_model": e.extractor_model,
        "observed_at": _fmt_ts(e.observed_at),
    }


def _build_mermaid(
    session, proposal: ClaimProposal,
    nodes: List[ClaimProposalNode], edges: List[ClaimProposalEdge],
) -> str:
    """Render proposal's subgraph. Proposed edges are dotted; already-resolved
    nodes link to their KG neighborhood so you can see graph-fit."""
    lines = ["graph LR"]
    used: set = set()

    def node_id(n: ClaimProposalNode) -> str:
        return f"P{n.id.replace('-', '')[:10]}"

    def kg_node_id(kg_id: str) -> str:
        return f"K{kg_id.replace('-', '')[:10]}"

    by_pid = {n.id: n for n in nodes}

    # proposal nodes
    for n in nodes:
        nid = node_id(n)
        if nid in used:
            continue
        used.add(nid)
        label = _mermaid_safe(f"{n.label} [{n.node_type}]")
        cls = "propnode"
        if n.resolution_action == "matched_existing":
            cls = "propnodeMatched"
        lines.append(f'    {nid}["{label}"]:::{cls}')

    # proposal edges (dotted — pending promotion)
    for e in edges:
        src = by_pid.get(e.source_node_id)
        tgt = by_pid.get(e.target_node_id)
        if not src or not tgt:
            continue
        lines.append(
            f'    {node_id(src)} -. "{_mermaid_safe(e.predicate)}" .-> {node_id(tgt)}'
        )

    # For each matched_existing node, attach a bit of its KG neighborhood
    for n in nodes:
        if not n.resolved_node_id:
            continue
        neighbors = (
            session.query(Edge, Node)
            .join(Node, Node.id == Edge.target_id)
            .filter(Edge.source_id == n.resolved_node_id)
            .limit(NEIGHBOR_LIMIT).all()
        )
        src = node_id(n)
        for edge, other in neighbors:
            kid = kg_node_id(other.id)
            if kid not in used:
                used.add(kid)
                label = _mermaid_safe(f"{other.label}")
                cls = "kgnode"
                if other.locked_by_user_at is not None:
                    cls = "kgnodeLocked"
                lines.append(f'    {kid}["{label}"]:::{cls}')
            lines.append(
                f'    {src} -- "{_mermaid_safe(edge.relationship_type)}" --> {kid}'
            )

    # Styling classes
    lines.append("    classDef propnode fill:#ffe4b3,stroke:#c38500,stroke-width:2px;")
    lines.append("    classDef propnodeMatched fill:#d8ffd8,stroke:#2e7d32,stroke-width:2px;")
    lines.append("    classDef kgnode fill:#eef3ff,stroke:#5577a9,stroke-width:1px;")
    lines.append("    classDef kgnodeLocked fill:#ffd7d7,stroke:#a40000,stroke-width:2px;")
    return "\n".join(lines)


def _mermaid_safe(s: str) -> str:
    return (s or "").replace('"', "'").replace("\n", " ")[:40]


def _fmt_ts(ts) -> Optional[str]:
    if ts is None:
        return None
    return str(ts)[:19]


def _load_window_items(session, window_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return every chat message in the window, in order. Used by the
    provenance expander on the proposal detail page."""
    if not window_id:
        return []
    from app.assistant.database.kg_chat_projection import KGChatConversationWindowItem
    rows = (
        session.query(KGChatConversationWindowItem)
        .filter(KGChatConversationWindowItem.window_id == window_id)
        .order_by(KGChatConversationWindowItem.item_order.asc())
        .all()
    )
    return [
        {
            "order": it.item_order,
            "role": it.role,
            "speaker": it.speaker_name,
            "text": it.text,
            "ts": _fmt_ts(it.unified_timestamp),
        }
        for it in rows
    ]


def _load_window_resolved(session, window_id: Optional[str]) -> Optional[str]:
    """Return the window's resolved_text (JSON-flattened to human-readable
    prose) if available. Lets the user see the resolver's output alongside
    the raw chat. Defensive — if the schema doesn't carry resolved_text on
    the window object (post-segment->window migration), returns None and
    the page just hides the section."""
    if not window_id:
        return None
    import json as _json
    from app.assistant.database.kg_chat_projection import KGChatConversationWindow
    if not hasattr(KGChatConversationWindow, "resolved_text"):
        return None
    try:
        w = (
            session.query(KGChatConversationWindow.resolved_text)
            .filter(KGChatConversationWindow.id == window_id).first()
        )
    except Exception:
        return None
    if w is None or not w[0]:
        return None
    rt = w[0].strip()
    if not rt.startswith("{"):
        return rt
    try:
        d = _json.loads(rt)
        lines = []
        for entry in d.values():
            if isinstance(entry, dict):
                role = entry.get("role", "?")
                text = (entry.get("resolved_text") or "").strip()
                if text:
                    lines.append(f"[{role}] {text}")
        return "\n".join(lines) or None
    except Exception:
        return rt[:2000]
