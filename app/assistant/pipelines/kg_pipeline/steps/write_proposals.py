"""
Stage 4: Write proposals (deterministic, no LLM).

For each kg_window_enrichment row not yet referenced from
claim_proposal_evidence, write one claim_proposal group:
  - 1 claim_proposal
  - N claim_proposal_node (one per enriched node)
  - M claim_proposal_edge (one per enriched edge)
  - 1 claim_proposal_evidence (links back to enrichment + window + provenance)

Component-splitting and metadata enrichment have already happened upstream
in Stage 3.5, so this stage is pure structured DB writes — no LLM calls,
no agent invocations. The actual row-writing is delegated to
``proposal_writer.write_one_proposal_group`` so the legacy chat-window path
and this pipeline stay byte-identical in output.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.assistant.utils.time_utils import utc_now
from typing import Any, Dict, List, Optional

from app.assistant.database.claim_proposals import ClaimProposal, ClaimProposalEvidence
from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.database.kg_pipeline_models import (
    KGResolvedMessage,
    KGWindow,
    KGWindowEnrichment,
    KGWindowExtraction,
    KGWindowMessage,
)
from app.assistant.kg.proposal_writer import write_one_proposal_group
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

WRITER_VERSION = "kg_pipeline_v1"
EXTRACTOR_AGENT_NAME = "knowledge_graph_add::fact_extractor"

MAX_ENRICHMENTS_PER_CYCLE = 60


class WriteProposalsStep:
    name = "write_proposals"

    def _claim_pending_enrichment_ids(self, limit: int) -> List[str]:
        with get_db_manager().read_session() as session:
            written_ids = (
                session.query(ClaimProposalEvidence.enrichment_id)
                .filter(ClaimProposalEvidence.enrichment_id.isnot(None))
                .distinct()
                .subquery()
                .select()
            )
            rows = (
                session.query(KGWindowEnrichment.id)
                .filter(~KGWindowEnrichment.id.in_(written_ids))
                .order_by(KGWindowEnrichment.created_at.asc())
                .limit(limit)
                .all()
            )
            return [rid for (rid,) in rows]

    def pending_count(self) -> int:
        from sqlalchemy import func as sqlfunc
        with get_db_manager().read_session() as session:
            written_ids = (
                session.query(ClaimProposalEvidence.enrichment_id)
                .filter(ClaimProposalEvidence.enrichment_id.isnot(None))
                .distinct()
                .subquery()
                .select()
            )
            return int(
                session.query(sqlfunc.count(KGWindowEnrichment.id))
                .filter(~KGWindowEnrichment.id.in_(written_ids))
                .scalar() or 0
            )

    def _load_enrichment_with_anchor(self, enrichment_id: str) -> Optional[Dict[str, Any]]:
        with get_db_manager().read_session() as session:
            enr = (
                session.query(KGWindowEnrichment)
                .filter(KGWindowEnrichment.id == enrichment_id)
                .first()
            )
            if enr is None:
                return None
            extraction = (
                session.query(KGWindowExtraction)
                .filter(KGWindowExtraction.id == enr.window_extraction_id)
                .first()
            )
            if extraction is None:
                return None
            window = (
                session.query(KGWindow)
                .filter(KGWindow.id == extraction.window_id)
                .first()
            )
            if window is None:
                return None

            # Anchor: the first user message in this window.
            anchor_row = (
                session.query(KGWindowMessage, UnifiedLog2026, KGResolvedMessage)
                .join(UnifiedLog2026, UnifiedLog2026.id == KGWindowMessage.unified_log_id)
                .outerjoin(KGResolvedMessage, KGResolvedMessage.unified_log_id == KGWindowMessage.unified_log_id)
                .filter(KGWindowMessage.window_id == window.id)
                .order_by(KGWindowMessage.item_order.asc())
                .all()
            )
            anchor_msg = None
            anchor_resolved = None
            for sm, m, rm in anchor_row:
                if (m.role or "").lower() == "user":
                    anchor_msg = m
                    anchor_resolved = rm
                    break
            if anchor_msg is None and anchor_row:
                _, anchor_msg, anchor_resolved = anchor_row[0]

            # Detach relevant fields up front so we don't lean on a closed
            # session after this read scope exits. enr is also detached but
            # its enriched_nodes/enriched_edges are JSON columns (plain
            # python lists) so they survive the close.
            payload = {
                "enrichment_id": enr.id,
                "enriched_nodes": list(enr.enriched_nodes or []),
                "enriched_edges": list(enr.enriched_edges or []),
                "window_id": window.id,
                "anchor": {
                    "room_id": anchor_msg.room_id if anchor_msg else None,
                    "speaker_name": anchor_msg.speaker_name if anchor_msg else None,
                    "speaker_role": (anchor_msg.role or None) if anchor_msg else None,
                    "unified_log_id": anchor_msg.id if anchor_msg else None,
                    "observed_at": anchor_msg.timestamp if anchor_msg else None,
                    "raw_text": (
                        (anchor_resolved.resolved_text if anchor_resolved else None)
                        or (anchor_msg.message if anchor_msg else None)
                    ),
                },
            }
            return payload

    def _persist_proposal(self, payload: Dict[str, Any]) -> bool:
        nodes = payload["enriched_nodes"]
        edges = payload["enriched_edges"]
        enrichment_id = payload["enrichment_id"]
        window_id = payload.get("window_id")
        anchor = payload["anchor"]

        # Empty enrichment — leave a marker evidence row so we don't
        # reprocess this enrichment forever.
        if not nodes and not edges:
            self._write_marker_evidence(enrichment_id, window_id, anchor, "empty enrichment")
            return False

        with get_db_manager().transaction(op="write_proposals.persist_proposal") as session:
            stats: Dict[str, int] = {}
            proposal_id = write_one_proposal_group(
                session,
                nodes=nodes,
                edges=edges,
                anchor=anchor,
                window_id=window_id,
                enrichment_id=enrichment_id,
                extractor_agent_name=EXTRACTOR_AGENT_NAME,
                extractor_prompt_version=WRITER_VERSION,
                stats=stats,
            )
            if proposal_id is None:
                # Skipped due to placeholder labels — write a marker in the
                # same transaction so the enrichment doesn't requeue.
                self._write_marker_evidence_inline(
                    session, enrichment_id, window_id, anchor, "skipped_placeholder",
                )
                return False
            return True

    def _write_marker_evidence(
        self, enrichment_id: str, window_id: Optional[str],
        anchor: Dict[str, Any], status_note: str,
    ) -> None:
        """Write a stub abandoned-proposal + evidence row so we don't
        reprocess this enrichment. Owns its own transaction."""
        with get_db_manager().transaction(op="write_proposals.marker") as session:
            self._write_marker_evidence_inline(
                session, enrichment_id, window_id, anchor, status_note,
            )

    def _write_marker_evidence_inline(
        self, session, enrichment_id: str, window_id: Optional[str],
        anchor: Dict[str, Any], status_note: str,
    ) -> None:
        observed_at = anchor.get("observed_at")
        # ClaimProposal.first/last_observed_at are DateTime(timezone=True) —
        # write aware UTC, not naive. `datetime.utcnow()` would force naive,
        # producing the same asymmetry that breaks downstream comparisons.
        now_utc = utc_now()
        proposal_id = str(uuid.uuid4())
        session.add(ClaimProposal(
            id=proposal_id,
            status="abandoned",
            claim_type="unknown",
            confidence=0.0,
            representative_sentence=(status_note or "empty enrichment")[:500],
            observation_count=1,
            first_observed_at=observed_at or now_utc,
            last_observed_at=observed_at or now_utc,
        ))
        session.flush()  # parent before child for FK ordering
        session.add(ClaimProposalEvidence(
            id=str(uuid.uuid4()),
            proposal_id=proposal_id,
            source_type="chat",
            window_id=window_id,
            enrichment_id=enrichment_id,
            unified_log_id=anchor.get("unified_log_id"),
            raw_text=anchor.get("raw_text"),
            speaker_name=anchor.get("speaker_name"),
            speaker_role=anchor.get("speaker_role"),
            room_id=anchor.get("room_id"),
            extractor_agent_name=EXTRACTOR_AGENT_NAME,
            extractor_prompt_version=WRITER_VERSION,
            observed_at=observed_at,
        ))

    def run_one_cycle(self, ctx) -> int:
        enrichment_ids = self._claim_pending_enrichment_ids(MAX_ENRICHMENTS_PER_CYCLE)
        if not enrichment_ids:
            return 0
        written = 0
        for enr_id in enrichment_ids:
            payload = self._load_enrichment_with_anchor(enr_id)
            if payload is None:
                continue
            try:
                if self._persist_proposal(payload):
                    written += 1
            except Exception as exc:
                logger.exception("[kg_pipeline.write_proposals] failed for enrichment %s: %s",
                                 enr_id, exc)
        if written:
            logger.info("[kg_pipeline.write_proposals] wrote %d proposals", written)
        return written
