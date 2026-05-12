"""
Stage 3.5: Enrich extracted nodes with metadata.

For each kg_window_extraction with verdict='extracted' not yet in
kg_window_enrichment:
  1. Split the extraction's nodes/edges into connected components
     (deterministic — proposal_group_splitter).
  2. For each component, call meta_data_add agent to fill in dates,
     aliases, importance, etc.
  3. Write one kg_window_enrichment row per component.

After this stage each enrichment row maps 1:1 to a future claim_proposal,
so Stage 4 becomes purely deterministic DB writes.
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.database.kg_pipeline_models import (
    KGResolvedMessage,
    KGWindow,
    KGWindowExtraction,
    KGWindowEnrichment,
    KGWindowMessage,
)
from app.assistant.kg.proposal_group_splitter import split_into_connected_components
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

ENRICHER_AGENT_NAME = "knowledge_graph_add::meta_data_add"
ENRICHER_VERSION = "kg_pipeline_v1"

MAX_EXTRACTIONS_PER_CYCLE = 30


class EnrichExtractionStep:
    name = "enrich_extraction"

    def _claim_pending_extraction_ids(self, limit: int) -> List[str]:
        with get_db_manager().read_session() as session:
            enriched_ids = session.query(KGWindowEnrichment.window_extraction_id).distinct().subquery().select()
            rows = (
                session.query(KGWindowExtraction.id)
                .filter(KGWindowExtraction.verdict == "extracted")
                .filter(~KGWindowExtraction.id.in_(enriched_ids))
                .order_by(KGWindowExtraction.created_at.asc())
                .limit(limit)
                .all()
            )
            return [rid for (rid,) in rows]

    def pending_count(self) -> int:
        from sqlalchemy import func as sqlfunc
        with get_db_manager().read_session() as session:
            enriched_ids = session.query(KGWindowEnrichment.window_extraction_id).distinct().subquery().select()
            return int(
                session.query(sqlfunc.count(KGWindowExtraction.id))
                .filter(KGWindowExtraction.verdict == "extracted")
                .filter(~KGWindowExtraction.id.in_(enriched_ids))
                .scalar() or 0
            )

    def _load_extraction(self, extraction_id: str) -> Optional[Dict[str, Any]]:
        """Return {extraction, window, resolved_window_text} or None."""
        with get_db_manager().read_session() as session:
            extraction = (
                session.query(KGWindowExtraction)
                .filter(KGWindowExtraction.id == extraction_id)
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

            # Build the resolved-window text for context (used by meta_data_add).
            rows = (
                session.query(
                    KGWindowMessage.item_order,
                    UnifiedLog2026.role,
                    UnifiedLog2026.speaker_name,
                    KGResolvedMessage.resolved_text,
                    UnifiedLog2026.message,
                )
                .join(UnifiedLog2026, UnifiedLog2026.id == KGWindowMessage.unified_log_id)
                .outerjoin(KGResolvedMessage, KGResolvedMessage.unified_log_id == KGWindowMessage.unified_log_id)
                .filter(KGWindowMessage.window_id == window.id)
                .order_by(KGWindowMessage.item_order.asc())
                .all()
            )
            resolved_lines = []
            for r in rows:
                _, role, speaker, resolved, raw = r
                role = (role or "").strip().lower()
                text = (resolved or raw or "").strip()
                if not text:
                    continue
                speaker = (speaker or "").strip() or role or "user"
                if role == "user":
                    resolved_lines.append(f"User ({speaker}): {text}")
                elif role == "assistant":
                    resolved_lines.append(f"Assistant: {text}")
            resolved_window_text = "\n".join(resolved_lines)

            return {
                "extraction": extraction,
                "window": window,
                "nodes": list(extraction.nodes or []),
                "edges": list(extraction.edges or []),
                "resolved_window": resolved_window_text,
                "message_timestamp": window.end_timestamp.isoformat() if window.end_timestamp else "",
            }

    def _call_meta_data_add(
        self,
        nodes: List[Dict],
        edges: List[Dict],
        resolved_window: str,
        message_timestamp: str,
    ) -> List[Dict]:
        """Call meta_data_add agent on State + Event nodes from a single component.

        Scoped to State + Event only (2026-05-12). Entity/Concept/Property
        pass through unchanged — they don't need this enrichment. Goals get
        valid_currently from goal_status deterministically (no LLM call).
        Returns enriched nodes merged with originals. Error-safe: returns
        originals on failure.
        """
        if not nodes:
            return nodes

        # Partition: only State+Event go through the LLM. Everything else
        # passes through (Entity/Concept/Property unchanged; Goal handled
        # by the deterministic goal-status path below).
        agent_targets = [
            n for n in nodes
            if n.get("temp_id") and n.get("node_type") in ("State", "Event")
        ]
        goal_targets = [
            n for n in nodes
            if n.get("temp_id") and n.get("node_type") == "Goal"
        ]

        meta_by_temp: Dict[str, Dict[str, Any]] = {}

        if agent_targets:
            from app.assistant.ServiceLocator.service_locator import DI

            try:
                agent = DI.agent_factory.create_agent(ENRICHER_AGENT_NAME)
                if agent is None:
                    logger.warning("[kg_pipeline.enrich] could not create meta_data_add agent")
                    agent = None
            except Exception as exc:
                logger.warning("[kg_pipeline.enrich] meta_data_add unavailable: %s", exc)
                agent = None

            if agent is not None:
                node_payloads = [
                    {
                        "temp_id": n.get("temp_id"),
                        "node_type": n.get("node_type"),
                        "label": n.get("label"),
                        "category": n.get("category"),
                        "sentence": n.get("sentence"),
                    }
                    for n in agent_targets
                ]

                sentences = [n.get("sentence") or "" for n in agent_targets]
                combined_sentence = " | ".join(dict.fromkeys(s for s in sentences if s))
                if not combined_sentence:
                    edge_sents = [e.get("sentence") or "" for e in (edges or [])]
                    combined_sentence = " | ".join(dict.fromkeys(s for s in edge_sents if s))

                agent_input = {
                    "nodes": _json.dumps(node_payloads, ensure_ascii=True),
                    "resolved_sentence": combined_sentence,
                    "resolved_window": resolved_window,
                    "message_timestamp": message_timestamp,
                }
                scope = build_pipeline_scope_context(
                    pipeline_id="kg_pipeline", actor_id="enrich_extraction",
                )

                try:
                    resp = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
                    data = resp.data if resp and hasattr(resp, "data") else {}
                except Exception as exc:
                    logger.warning("[kg_pipeline.enrich] meta_data_add call failed: %s", exc)
                    data = {}

                meta_nodes = data.get("Nodes") or [] if isinstance(data, dict) else []
                for mn in meta_nodes:
                    if isinstance(mn, dict) and mn.get("temp_id"):
                        meta_by_temp[str(mn["temp_id"])] = mn

        # Goal nodes: deterministic valid_currently from goal_status. No LLM.
        # Goals carry their lifecycle on goal_status (active|completed|abandoned|dormant).
        # 'completed' and 'abandoned' = no longer pursued → valid_currently=false.
        # 'active' and 'dormant' (and unset) → null (presumed valid).
        for gn in goal_targets:
            gs = (gn.get("goal_status") or "").lower()
            if gs in ("completed", "abandoned"):
                meta_by_temp[str(gn["temp_id"])] = {
                    "temp_id": gn["temp_id"],
                    "valid_currently": False,
                    "validity_reason": f"goal_status={gs}",
                }

        if not meta_by_temp:
            return nodes

        # Merge enrichment back onto original node dicts. Field whitelist
        # reflects the slimmed meta_data_add output (2026-05-12):
        # dropped aliases/hash_tags/valid_during; added valid_currently +
        # validity_reason. goal_status comes from extractor for Goal nodes,
        # not from this agent.
        merged: List[Dict] = []
        for n in nodes:
            tid = str(n.get("temp_id") or "")
            meta = meta_by_temp.get(tid)
            if not meta:
                merged.append(n)
                continue
            combined = dict(n)
            for fld in (
                "start_date", "start_date_confidence",
                "start_date_prose", "end_date", "end_date_confidence",
                "end_date_prose", "valid_currently", "validity_reason",
                "semantic_label", "confidence",
            ):
                val = meta.get(fld)
                # valid_currently is allowed to be False (which is falsy but meaningful).
                if fld == "valid_currently":
                    if val is not None:
                        combined[fld] = val
                    continue
                if val not in (None, "", []):
                    combined[fld] = val
            merged.append(combined)
        return merged

    def _persist_enrichment(
        self,
        extraction_id: str,
        component_index: int,
        enriched_nodes: List[Dict],
        enriched_edges: List[Dict],
    ) -> None:
        get_db_manager().write(
            KGWindowEnrichment(
                window_extraction_id=extraction_id,
                component_index=component_index,
                enriched_nodes=enriched_nodes,
                enriched_edges=enriched_edges,
                enricher_version=ENRICHER_VERSION,
            ),
            op="enrich.persist_enrichment",
        )

    def run_one_cycle(self, ctx) -> int:
        extraction_ids = self._claim_pending_extraction_ids(MAX_EXTRACTIONS_PER_CYCLE)
        if not extraction_ids:
            return 0

        components_written = 0
        for extraction_id in extraction_ids:
            data = self._load_extraction(extraction_id)
            if data is None:
                continue
            all_nodes = data["nodes"]
            all_edges = data["edges"]

            if not all_nodes and not all_edges:
                # Empty extraction — write a single empty enrichment row to mark done
                self._persist_enrichment(extraction_id, 0, [], [])
                components_written += 1
                continue

            for idx, (component_nodes, component_edges) in enumerate(
                split_into_connected_components(all_nodes, all_edges)
            ):
                if not component_nodes and not component_edges:
                    continue
                enriched = self._call_meta_data_add(
                    component_nodes, component_edges,
                    resolved_window=data["resolved_window"],
                    message_timestamp=data["message_timestamp"],
                )
                self._persist_enrichment(extraction_id, idx, enriched, component_edges)
                components_written += 1

        if components_written:
            logger.info(
                "[kg_pipeline.enrich] wrote %d enrichment rows from %d extractions",
                components_written, len(extraction_ids),
            )
        return components_written
