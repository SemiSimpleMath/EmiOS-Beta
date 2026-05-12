"""KG pipeline status — bucket-depth dashboard.

Read-only Flask blueprint that surfaces:
  - Pending counts per pipeline stage (resolver → segmenter → extractor →
    enricher → write_proposals → promoter)
  - Completed totals per output table
  - KG state summary (nodes + edges, broken down a bit)
  - Subsystem flag state for kg_pipeline + kg_proposal_promoter

Routes:
  GET /kg-pipeline/status            — HTML page that auto-refreshes via JSON
  GET /api/kg-pipeline/status.json   — same data as JSON for polling

Future additions (deferred):
  - Run-time / token cost stats (would query llm_client logs)
  - Halt-reason surfacing (needs in-flight runner reference)
"""
from __future__ import annotations

from flask import Blueprint, render_template, jsonify

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

kg_pipeline_status_bp = Blueprint('kg_pipeline_status', __name__)


def _collect_status() -> dict:
    """Single read-only snapshot. Cheap — pure COUNT/SQL.

    Returns a dict ready for JSON serialization.
    """
    import sqlite3
    from pathlib import Path
    from datetime import datetime, timezone

    repo = Path(__file__).resolve().parents[2]
    db_path = repo / "emi.db"

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    def _count(sql: str, params=()) -> int:
        try:
            return int(cur.execute(sql, params).fetchone()[0] or 0)
        except sqlite3.OperationalError:
            return -1

    # ---- Pipeline stage queue depths (work waiting to be done) ----
    # Use each step's own pending_count() so filters (resolver cutoff,
    # critic-rejected windows, etc.) match what the live pipeline actually sees.
    from app.assistant.pipelines.kg_pipeline.steps.resolve_messages import ResolveMessagesStep
    from app.assistant.pipelines.kg_pipeline.steps.segment_messages import SegmentMessagesStep
    from app.assistant.pipelines.kg_pipeline.steps.critique_and_extract import CritiqueAndExtractStep
    from app.assistant.pipelines.kg_pipeline.steps.enrich_extraction import EnrichExtractionStep
    from app.assistant.pipelines.kg_pipeline.steps.write_proposals import WriteProposalsStep

    def _safe_pending(step) -> int:
        try:
            return int(step.pending_count() or 0)
        except Exception:
            logger.exception("pending_count failed for step=%s", getattr(step, 'name', '?'))
            return -1

    resolver_pending  = _safe_pending(ResolveMessagesStep())
    segmenter_pending = _safe_pending(SegmentMessagesStep())
    extractor_pending = _safe_pending(CritiqueAndExtractStep())
    enricher_pending  = _safe_pending(EnrichExtractionStep())
    proposer_pending  = _safe_pending(WriteProposalsStep())

    # Promoter is a separate routine — count pending proposals directly.
    promoter_pending = _count("SELECT COUNT(*) FROM claim_proposal WHERE status = 'pending'")

    # ---- Completed totals ----
    totals = {
        'unified_log_2026_total': _count("SELECT COUNT(*) FROM unified_log_2026"),
        'kg_resolved_message': _count("SELECT COUNT(*) FROM kg_resolved_message"),
        'kg_window': _count("SELECT COUNT(*) FROM kg_window"),
        'kg_window_extraction': _count("SELECT COUNT(*) FROM kg_window_extraction"),
        'kg_window_enrichment': _count("SELECT COUNT(*) FROM kg_window_enrichment"),
        'claim_proposal': _count("SELECT COUNT(*) FROM claim_proposal"),
        'claim_proposal_node': _count("SELECT COUNT(*) FROM claim_proposal_node"),
        'claim_proposal_edge': _count("SELECT COUNT(*) FROM claim_proposal_edge"),
        'kg_node_metadata': _count("SELECT COUNT(*) FROM kg_node_metadata"),
        'kg_edge_metadata': _count("SELECT COUNT(*) FROM kg_edge_metadata"),
        'kg_node_evidence': _count("SELECT COUNT(*) FROM kg_node_evidence"),
        'kg_edge_evidence': _count("SELECT COUNT(*) FROM kg_edge_evidence"),
        'entity_cards_v2_active': _count("SELECT COUNT(*) FROM entity_card_v2 WHERE is_active = 1"),
        'kg_maintenance_findings_open': _count(
            "SELECT COUNT(*) FROM kg_maintenance_finding WHERE status IS NULL OR status NOT IN ('resolved','dismissed','executed')"
        ),
    }

    # ---- Node-type breakdown for the materialized KG ----
    node_breakdown = {}
    try:
        for r in cur.execute(
            "SELECT node_type, COUNT(*) c FROM kg_node_metadata GROUP BY node_type ORDER BY c DESC"
        ):
            node_breakdown[r['node_type']] = r['c']
    except Exception:
        pass

    # Confidence-tier breakdown — useful operational signal
    tier_breakdown = {'node': {}, 'edge': {}}
    try:
        for r in cur.execute(
            "SELECT confidence_tier, COUNT(*) c FROM kg_node_metadata GROUP BY confidence_tier"
        ):
            tier_breakdown['node'][r['confidence_tier']] = r['c']
        for r in cur.execute(
            "SELECT confidence_tier, COUNT(*) c FROM kg_edge_metadata GROUP BY confidence_tier"
        ):
            tier_breakdown['edge'][r['confidence_tier']] = r['c']
    except Exception:
        pass

    con.close()

    # ---- Subsystem flags ----
    from app.assistant.utils.subsystem_flags import is_subsystem_enabled
    subsystems = {
        'kg_pipeline': is_subsystem_enabled('kg_pipeline'),
        'kg_proposal_promoter': is_subsystem_enabled('kg_proposal_promoter'),
    }

    return {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'stage_queues': {
            'resolver': resolver_pending,
            'segmenter': segmenter_pending,
            'extractor': extractor_pending,
            'enricher': enricher_pending,
            'write_proposals': proposer_pending,
            'promoter': promoter_pending,
        },
        'totals': totals,
        'node_breakdown_by_type': node_breakdown,
        'confidence_tier_breakdown': tier_breakdown,
        'subsystems': subsystems,
    }


@kg_pipeline_status_bp.route('/kg-pipeline/status', methods=['GET'])
def status_page():
    """HTML dashboard that polls the JSON endpoint."""
    return render_template('kg_pipeline_status.html')


@kg_pipeline_status_bp.route('/api/kg-pipeline/status.json', methods=['GET'])
def status_json():
    """JSON snapshot of current pipeline state."""
    try:
        return jsonify(_collect_status())
    except Exception as exc:
        logger.exception("kg_pipeline status collection failed")
        return jsonify({'error': f'{type(exc).__name__}: {exc}'}), 500
