"""
Context Enricher Prep Node

For each admitted artifact:
1. Searches the KG using the item's own text (summary, sender, subject)
2. Includes the user's key people/entities as standing context
3. Calls the context_enricher agent (single-shot, no tools) to reason
4. Writes enrichment annotations onto item metadata

The prep node provides raw KG search results. The agent does ALL the reasoning.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MAX_ITEMS_PER_CYCLE = 8
_SEMANTIC_THRESHOLD = 0.40
_SEMANTIC_TOP_K = 8


def _get_meta(item: Dict[str, Any]) -> Dict[str, Any]:
    meta = item.get("metadata") if isinstance(item, dict) else {}
    return meta if isinstance(meta, dict) else {}


def _get_primary_user_label() -> str:
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        rm = getattr(DI, "resource_manager", None)
        if rm:
            res = rm._get_cached_resource("resource_user_data")
            if isinstance(res, dict):
                return str(res.get("first_name") or "").strip()
    except Exception:
        pass
    return ""


def _get_user_people(session) -> List[str]:
    """Get names of key people in the user's life from the KG.

    Returns labels of Entity-type nodes within 2 hops of the user.
    These are always searched so the agent has family/people context.
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge

    user_label = _get_primary_user_label()
    if not user_label:
        return []

    try:
        user_node = session.query(Node).filter(
            Node.label == user_label, Node.node_type == "Entity"
        ).first()
        if not user_node:
            return []

        # Hop 1: all connected nodes.
        hop1_ids = set()
        for edge, node in (
            session.query(Edge, Node).join(Node, Edge.target_id == Node.id)
            .filter(Edge.source_id == user_node.id).all()
            + session.query(Edge, Node).join(Node, Edge.source_id == Node.id)
            .filter(Edge.target_id == user_node.id).all()
        ):
            if node.id != user_node.id:
                hop1_ids.add(node.id)

        # Hop 2: Entity nodes connected through hop-1.
        people = []
        for h1_id in list(hop1_ids)[:30]:
            for edge, node in (
                session.query(Edge, Node).join(Node, Edge.target_id == Node.id)
                .filter(Edge.source_id == h1_id).limit(5).all()
                + session.query(Edge, Node).join(Node, Edge.source_id == Node.id)
                .filter(Edge.target_id == h1_id).limit(5).all()
            ):
                if node.node_type == "Entity" and node.id != user_node.id:
                    if node.label not in people:
                        people.append(node.label)
        return people[:20]
    except Exception as e:
        logger.warning("context_enricher_prep: get_user_people failed: %s", e)
        return []


def _extract_search_terms(summary: str, email_sender: str, email_subject: str) -> List[str]:
    """Extract search terms from the item's own text. No category guessing."""
    terms = []

    # Split summary on delimiters.
    for text in [summary, email_subject]:
        if not text:
            continue
        parts = re.split(r'[:\-–—,;|/]+', text)
        for part in parts:
            cleaned = part.strip()
            if len(cleaned) > 3 and cleaned not in terms:
                terms.append(cleaned)

    # Add sender if present.
    if email_sender and email_sender not in terms:
        terms.append(email_sender)

    return terms[:6]


def _build_kg_context(search_terms: List[str], session) -> str:
    """Run semantic search for each term and format results with neighborhoods.

    Edge endpoints may be pod URIs (no kg_node row by design). We use
    outerjoin + pod_store fallback so pod-endpoint edges still surface.
    """
    from app.assistant.kg_core.kg_utils.kg_tools import semantic_find_node_by_text
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from app.assistant.pod_store.pod_uri import POD_URI_RE

    _pod_cache: Dict[str, tuple] = {}

    def _resolve_endpoint(node_obj, endpoint_id: str):
        """Return (label, node_type) for an edge endpoint; hydrate pod URIs from pod_store."""
        if node_obj is not None:
            return node_obj.label, node_obj.node_type
        if not endpoint_id or not POD_URI_RE.fullmatch(endpoint_id):
            return None, None
        if endpoint_id in _pod_cache:
            return _pod_cache[endpoint_id]
        from app.assistant.pod_store.pod_store import PodStore
        pod = PodStore().get(endpoint_id)
        if pod is None:
            _pod_cache[endpoint_id] = (None, None)
        else:
            _pod_cache[endpoint_id] = ((pod.one_liner or endpoint_id)[:200], "Pod")
        return _pod_cache[endpoint_id]

    lines = []
    seen = set()
    for term in search_terms:
        try:
            results = semantic_find_node_by_text(
                term, session, threshold=_SEMANTIC_THRESHOLD, k=_SEMANTIC_TOP_K,
            )
            for node, score in (results or []):
                if node.id in seen:
                    continue
                seen.add(node.id)
                desc = f" — {node.description[:150]}" if node.description else ""
                lines.append(f"- [{node.node_type}] **{node.label}** (match={score:.2f}){desc}")

                for edge, target in (
                    session.query(Edge, Node).outerjoin(Node, Edge.target_id == Node.id)
                    .filter(Edge.source_id == node.id).limit(5).all()
                ):
                    t_label, t_type = _resolve_endpoint(target, edge.target_id)
                    if t_label is None:
                        continue
                    lines.append(f"    → {edge.relationship_type} → {t_label} [{t_type}]")
                for edge, source in (
                    session.query(Edge, Node).outerjoin(Node, Edge.source_id == Node.id)
                    .filter(Edge.target_id == node.id).limit(5).all()
                ):
                    s_label, s_type = _resolve_endpoint(source, edge.source_id)
                    if s_label is None:
                        continue
                    lines.append(f"    ← {s_label} [{s_type}] → {edge.relationship_type}")
        except Exception as e:
            logger.warning("context_enricher_prep: search failed for %r: %s", term, e)

    return "\n".join(lines)


def _call_enricher_agent(task_text: str, kg_context: str, scope_context) -> Dict[str, Any]:
    """Call the context_enricher agent (single-shot, no tools)."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    agent = DI.agent_factory.create_agent("context_enricher::planner")
    if not agent:
        raise RuntimeError("Failed to create context_enricher::planner agent")

    result = agent.action_handler(Message(
        agent_input={"task": task_text, "information": kg_context},
        scope_context=scope_context,
    ))

    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else {}


class ContextEnricherPrepNode(ControlNode):
    """Enrich admitted artifacts with KG context via single-shot LLM agent."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        admitted = self.blackboard.get_state_value("admitted_artifacts", []) or []
        if not isinstance(admitted, list):
            admitted = []

        if not admitted:
            logger.info("[%s] no admitted artifacts to enrich, skipping.", self.name)
            self.blackboard.update_state_value("next_agent", "context_enricher_persist_node")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        from app.models.base import get_session
        from app.assistant.pipelines.scope_policy import build_pipeline_scope_context

        scope_context = build_pipeline_scope_context(
            pipeline_id="dayflow",
            actor_id=f"{self.name}_runner",
            room_id="dayflow_orchestrator",
            room_surface="ui",
        )

        enrichments: List[Dict[str, str]] = []

        with get_session() as session:
            # Get user's key people — always searched for standing context.
            user_people = _get_user_people(session)

            for item in admitted[:_MAX_ITEMS_PER_CYCLE]:
                meta = _get_meta(item)
                item_id = str(meta.get("item_id") or item.get("id") or "").strip()
                summary = str(meta.get("summary") or "").strip()
                source_type = str(meta.get("source_type") or "").strip()

                if not item_id or not summary:
                    continue

                email_sender = str(meta.get("email_sender") or "").strip()
                email_subject = str(meta.get("email_subject") or "").strip()
                # First ~2000 chars of the email body — gives the agent the
                # salutation ("Dear Parent of <name>") and first paragraph,
                # which is the most reliable disambiguating signal for who an
                # email is about (vs. inferring from summary alone).
                email_body_excerpt = str(meta.get("email_body_excerpt") or "").strip()

                # Search terms from the item's own text.
                search_terms = _extract_search_terms(summary, email_sender, email_subject)

                # Add user's key people as search terms.
                for person in user_people:
                    if person not in search_terms:
                        search_terms.append(person)

                # Build KG context from semantic search.
                kg_context = _build_kg_context(search_terms, session)

                if not kg_context:
                    logger.info("[%s] no KG context for item %s, skipping.", self.name, item_id)
                    continue

                # Build task description for the agent.
                task_lines = [f"Source: {source_type}", f"Summary: {summary}"]
                if email_sender:
                    task_lines.append(f"From: {email_sender}")
                if email_subject:
                    task_lines.append(f"Subject: {email_subject}")
                if email_body_excerpt:
                    task_lines.append(f"Body excerpt:\n{email_body_excerpt}")
                task_text = "\n".join(task_lines)

                try:
                    result = _call_enricher_agent(task_text, kg_context, scope_context)
                    enrichment_text = str(result.get("enrichment") or "").strip()
                    if enrichment_text:
                        enrichments.append({"item_id": item_id, "enrichment": enrichment_text})
                        logger.info("[%s] enriched %s: %s", self.name, item_id, enrichment_text[:80])
                except Exception as e:
                    logger.warning("[%s] enrichment failed for %s: %s", self.name, item_id, e)

        self.blackboard.update_state_value("enrichments", enrichments)
        self.blackboard.update_state_value("last_agent", self.name)

        logger.info("[%s] enriched %d of %d admitted artifacts.", self.name, len(enrichments), len(admitted))
