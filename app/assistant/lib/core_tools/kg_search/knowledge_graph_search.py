from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Union, Tuple
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import or_, desc
from sqlalchemy.orm import Session

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolResult, ToolMessage, Message

from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
from app.assistant.kg_core.kg_utils.kg_tools import semantic_find_node_by_text, short_describe_node, describe_edge
from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils

from app.assistant.utils.logging_config import get_logger
from app.assistant.kg_core.kg_utils.kg_tools import recency_score
from app.assistant.utils.vector_utils import cosine_similarity
from app.assistant.utils.time_utils import get_local_time_str
from app.models.base import get_session

logger = get_logger(__name__)


class SearchFilters(BaseModel):
    """Filters to restrict search scope with proper validation and guidance."""
    node_types: Union[List[str], None] = Field(None, description="List of node types to filter by (e.g., 'Entity', 'Goal', 'State')")
    start_date: Union[str, None] = Field(None, description="ISO date string - only include nodes valid after this date (e.g., '2024-01-01T00:00:00Z')")
    end_date: Union[str, None] = Field(None, description="ISO date string - only include nodes valid before this date (e.g., '2024-12-31T23:59:59Z')")
    node_id: Union[str, None] = Field(None, description="UUID of a single node to restrict search to its neighborhood")
    max_hops: Union[int, None] = Field(None, description="Integer - expand from node_id to neighbors within this many hops (requires node_id, default=all connected)")
    text: Union[str, None] = Field(None, description="Text to filter nodes by (searches in node labels)")
    taxonomy_paths: Union[List[str], None] = Field(None, description="List of taxonomy paths to filter by (e.g., 'entity > person', 'state > educational_status'). Returns union of nodes from all specified paths.")


class kg_find_node_args(BaseModel):
    """Arguments for the find_node tool call."""
    text: str
    threshold: float
    k: int
    node_id: str
    edges_k: int
    node_types: List[str]
    start_date: str
    end_date: str
    max_hops: int
    taxonomy_paths: List[str] = Field(default_factory=list)


class ask_kg_args(BaseModel):
    """
    Fast KG RAG Q&A.

    Two modes:
    - Node-anchored (node provided): BFS neighborhood around the named node.
    - Global (node omitted / empty): direct embedding search across all KG edges,
      then fall back to the primary-user node if Chroma returns no results.
    """
    question: str = Field(..., description="The question to answer using KG evidence")
    node: Optional[str] = Field(
        None,
        description=(
            "Base node name/title (e.g. 'Jukka', 'Katy', 'Orora'). "
            "Leave empty to search the whole graph."
        ),
    )


def _safe_int(val: Any, default: int) -> int:
    try:
        i = int(val)
        return i
    except Exception:
        logger.debug("_safe_int: could not convert %r to int, returning default %r", val, default, exc_info=True)
        return default


def _clamp_int(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _short(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    if n <= 0:
        return ""
    return s if len(s) <= n else (s[: max(0, n - 1)] + "…")


def _pick_node_by_label(session: Session, label: str) -> Optional[Node]:
    """
    Deterministically pick the "best" node for a label.
    If multiple nodes share the same label, choose the one with highest degree.
    """
    if not label:
        return None
    candidates: List[Node] = session.query(Node).filter(Node.label == label).all()
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    best: Optional[Node] = None
    best_deg = -1
    for n in candidates:
        try:
            nid = str(n.id)
            indeg = session.query(Edge).filter(Edge.target_id == nid).count()
            outdeg = session.query(Edge).filter(Edge.source_id == nid).count()
            deg = indeg + outdeg
        except Exception:
            logger.error("Could not compute degree for node %s", nid, exc_info=True)
            deg = 0
        if deg > best_deg:
            best = n
            best_deg = deg
        elif deg == best_deg and best is not None:
            # Tie-break: stable by UUID string
            if str(n.id) < str(best.id):
                best = n
    return best or candidates[0]


def _expand_neighborhood_fast(
    session: Session,
    base_node_id: str,
    *,
    max_hops: int,
    max_nodes: int,
    max_edges: int,
) -> Tuple[Dict[str, int], List[Edge]]:
    """
    Fast-ish BFS neighborhood expansion using batched SQL queries per hop.
    Returns:
      - distance_by_node_id: {node_id: hop_distance}
      - edges: list of unique edges discovered (bounded)
    """
    base_node_id = str(base_node_id)
    max_hops = _clamp_int(_safe_int(max_hops, 3), 0, 6)
    max_nodes = _clamp_int(_safe_int(max_nodes, 250), 1, 5000)
    max_edges = _clamp_int(_safe_int(max_edges, 800), 1, 20000)

    distance_by_node_id: Dict[str, int] = {base_node_id: 0}
    frontier: List[str] = [base_node_id]

    seen_edge_ids: set[str] = set()
    collected_edges: List[Edge] = []

    for hop in range(0, max_hops):
        if not frontier:
            break
        if len(distance_by_node_id) >= max_nodes:
            break
        if len(collected_edges) >= max_edges:
            break

        remaining_edges = max_edges - len(collected_edges)
        # Query edges touching any node in the frontier (both directions).
        q = (
            session.query(Edge)
            .filter(or_(Edge.source_id.in_(frontier), Edge.target_id.in_(frontier)))
            .order_by(desc(Edge.updated_at), desc(Edge.created_at), Edge.id)
            .limit(max(remaining_edges, 1))
        )
        edges = q.all()
        if not edges:
            break

        next_frontier: List[str] = []

        for e in edges:
            if e is None:
                continue
            eid = str(e.id)
            if eid in seen_edge_ids:
                continue
            seen_edge_ids.add(eid)
            collected_edges.append(e)

            src = str(e.source_id)
            tgt = str(e.target_id)

            # Expand from any frontier endpoint; allow adding both endpoints.
            for neighbor in (src, tgt):
                if neighbor in distance_by_node_id:
                    continue
                # Determine if neighbor is reachable in hop+1 by any frontier touch.
                # (We are iterating only edges connected to frontier, so this is safe.)
                distance_by_node_id[neighbor] = hop + 1
                next_frontier.append(neighbor)
                if len(distance_by_node_id) >= max_nodes:
                    break
            if len(distance_by_node_id) >= max_nodes or len(collected_edges) >= max_edges:
                break

        frontier = next_frontier

    return distance_by_node_id, collected_edges



class KnowledgeGraphSearch(BaseTool):
    """
    Tool for querying and inspecting knowledge graph data,
    including semantic node search and node description.
    """

    def __init__(self):
        super().__init__('knowledge_graph_search')
        self.do_lazy_init = True
        self.session = None

    def lazy_init(self):
        self.session = get_session()
        self.do_lazy_init = False

    def _release_session(self) -> None:
        """Close the handler session. Called before the RAG LLM step (a
        session must not ride an LLM call — db_manager discipline, 2026-07-08
        KG audit G6) and at the end of execute() (previously nothing closed
        it at all outside the __main__ demo — one leaked session per call).
        A later handler call re-opens via lazy_init."""
        if self.session is not None:
            try:
                self.session.close()
            finally:
                self.session = None
                self.do_lazy_init = True

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        if self.do_lazy_init:
            self.lazy_init()

        try:
            logger.info("Executing KnowledgeGraphSearch")
            arguments = tool_message.tool_data.get('arguments', {})
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'

            if not tool_name:
                raise ValueError("Missing tool_name in tool_data.")

            handler_method = getattr(self, f"handle_{tool_name}", None)
            if not handler_method:
                raise ValueError(f"Unsupported tool_name: {tool_name}")

            return handler_method(arguments, tool_message)

        except Exception as e:
            logger.error("KnowledgeGraphTool execution failed")
            logger.debug("knowledgeGraphTool execution failed exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="kg_search_execute_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )
        finally:
            self._release_session()

    def publish_result(self, result: ToolResult) -> ToolResult:
        return result

    def publish_error(self, error_result: ToolResult) -> ToolResult:
        return error_result

    # ---------------------- HANDLERS ----------------------

    def handle_kg_find_node(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        try:
            # Validate arguments with the Pydantic model
            args = kg_find_node_args(**arguments)

            relaxed_filters_list = []
            results = []

            # Create filters from flattened arguments, handling empty values
            current_filters = SearchFilters(
                node_types=args.node_types if args.node_types else None,
                start_date=args.start_date if args.start_date else None,
                end_date=args.end_date if args.end_date else None,
                node_id=args.node_id if args.node_id else None,
                max_hops=args.max_hops if args.max_hops else None,
                taxonomy_paths=args.taxonomy_paths if args.taxonomy_paths else None
            )

            # 1. Initial search attempt with all filters
            logger.info(f"Attempting search with initial filters: {current_filters.model_dump_json(exclude_none=True)}")
            results = semantic_find_node_by_text(
                text=args.text,
                session=self.session,
                threshold=args.threshold,
                k=args.k,
                filters=current_filters
            )

            # 2. If no results, begin the relaxation process
            if not results:
                # Corrected relaxation order based on your ranking
                relaxation_plan = ["start_date", "end_date"]

                for filter_to_relax in relaxation_plan:
                    if getattr(current_filters, filter_to_relax) is not None:
                        setattr(current_filters, filter_to_relax, None)
                        relaxed_filters_list.append(filter_to_relax)

                        logger.info(f"Relaxed '{filter_to_relax}'. Retrying with filters: {current_filters.model_dump_json(exclude_none=True)}")

                        results = semantic_find_node_by_text(
                            text=args.text, session=self.session, threshold=args.threshold, k=args.k, filters=current_filters
                        )

                        if results:
                            break

            # --- Advanced Relaxation for max_hops ---
            # If there are still no results, you could implement logic to handle max_hops.
            # This would involve checking if max_hops was initially set, increasing it,
            # and re-running the search. For example:
            #
            # if not results and args.filters and args.filters.max_hops and args.filters.max_hops == 1:
            #     logger.info("Dropping filters failed. Now trying to expand max_hops to 2.")
            #     current_filters.max_hops = 2
            #     relaxed_filters_list.append("expanded 'max_hops' to 2")
            #     # NOTE: This assumes `semantic_find_node_by_text` can use max_hops.
            #     results = semantic_find_node_by_text(...)

            # 3. Build the output node cards
            output_nodes = []
            is_semantic_search = bool(args.text and args.text.strip())
            match_type = "semantic" if is_semantic_search else "filtered"
            
            for node, score in results:
                minimal_card = {
                    "node_id": str(node.id),
                    "label": node.label,
                    "description": node.description if hasattr(node, 'description') and node.description else None,
                    "node_type": getattr(node, 'node_type', None) or getattr(node, 'type_name', None),
                    "match": {"type": match_type, "score": round(float(score), 4) if is_semantic_search else None},
                    "start_date": node.start_date.isoformat() if node.start_date else None,
                    "end_date": node.end_date.isoformat() if node.end_date else None,
                    "start_date_confidence": node.start_date_confidence,
                    "end_date_confidence": node.end_date_confidence,
                }
                output_nodes.append(minimal_card)

            # 4. Construct the final human-readable message
            if is_semantic_search:
                content_message = f"Found {len(output_nodes)} node(s) for '{args.text}'"
            else:
                content_message = f"Found {len(output_nodes)} node(s) matching filters (no text search)"
            
            if relaxed_filters_list:
                constraints_str = ", ".join([f"'{f}'" for f in relaxed_filters_list])
                content_message += f". Note: The following constraints were relaxed to get results: {constraints_str}."

            # 5. Create the final data payload for the agent
            final_data_payload = {
                "nodes": output_nodes,
                "relaxed_filters": relaxed_filters_list
            }

            return self.publish_result(
                ToolResult(
                    result_type="semantic_node_search",
                    content=content_message,
                    data=final_data_payload
                )
            )

        except Exception as e:
            logger.error("Error in handle_kg_find_node")
            logger.debug("error in handle_kg_find_node exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="kg_find_node_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )

    def handle_ask_kg(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """
        Answer a question using KG evidence.

        Two modes:
        - Node-anchored (node provided): BFS neighborhood around the named node.
        - Global (node omitted / empty): direct Chroma edge search across the whole
          graph, no BFS. Falls back to primary-user node if Chroma returns nothing.
        """
        try:
            if not isinstance(arguments, dict):
                arguments = {}
            cleaned_arguments = {k: v for k, v in arguments.items() if v is not None}
            args = ask_kg_args(**cleaned_arguments)
            question = (args.question or "").strip()
            if not question:
                raise ValueError("Missing required 'question'.")

            node_query = (args.node or "").strip()
            global_mode = not node_query

            # Fixed internals
            max_hops = 2
            max_nodes = 250
            max_edges = 800
            k_edges = 20
            min_similarity = 0.18
            max_description_chars = 400
            include_attributes = True
            engine = "gpt-5.6-luna"
            timeout = 60
            return_evidence = False

            kg_utils = KnowledgeGraphUtils(self.session)
            try:
                q_emb = kg_utils.create_embedding(question)
            except Exception as e:
                raise RuntimeError(
                    "Embedding model unavailable. Ensure chromadb is installed "
                    "to use ask_kg."
                ) from e

            # ── GLOBAL MODE ────────────────────────────────────────────────
            if global_mode:
                from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
                from app.assistant.kg_core.user_identity import get_primary_user_name

                chroma = get_chroma_manager()
                # Pass threshold=0.0: Chroma uses L2 distance converted to 1/(1+d), a different
                # scale than cosine min_similarity. We take all top-k candidates and rely on
                # the result ordering (highest similarity first) rather than a threshold cut.
                chroma_results = chroma.search_similar_edges(q_emb, k=k_edges * 2, threshold=0.0)
                # chroma_results: List[Tuple[edge_id, similarity]]

                if not chroma_results:
                    # Fallback: anchor to primary user and do BFS
                    logger.debug("[ask_kg] global Chroma search returned nothing; falling back to primary user node")
                    node_query = get_primary_user_name()
                    global_mode = False
                else:
                    # Hydrate edges from SQLite
                    edge_ids = [r[0] for r in chroma_results]
                    sim_by_id = {r[0]: r[1] for r in chroma_results}

                    raw_edges: List[Edge] = (
                        self.session.query(Edge)
                        .filter(Edge.id.in_(edge_ids))
                        .all()
                    )
                    edge_by_id: Dict[str, Edge] = {str(e.id): e for e in raw_edges if e}

                    # Collect all node ids referenced
                    all_node_ids: set[str] = set()
                    for e in raw_edges:
                        if e:
                            all_node_ids.add(str(e.source_id))
                            all_node_ids.add(str(e.target_id))

                    nodes_list: List[Node] = []
                    if all_node_ids:
                        nodes_list = self.session.query(Node).filter(Node.id.in_(all_node_ids)).all()
                    node_by_id: Dict[str, Node] = {str(n.id): n for n in nodes_list if n}

                    scored_edges: List[Dict[str, Any]] = []
                    for edge_id, sim in chroma_results:
                        e = edge_by_id.get(edge_id)
                        if not e or not e.sentence:
                            continue
                        src_id = str(e.source_id)
                        tgt_id = str(e.target_id)
                        src = node_by_id.get(src_id)
                        tgt = node_by_id.get(tgt_id)
                        ts = e.updated_at or e.created_at
                        scored_edges.append({
                            "edge_id": edge_id,
                            "sim": round(float(sim), 4),
                            "type": e.relationship_type,
                            "source_id": src_id,
                            "target_id": tgt_id,
                            "source_label": src.label if src else None,
                            "source_type": src.node_type if src else None,
                            "target_label": tgt.label if tgt else None,
                            "target_type": tgt.node_type if tgt else None,
                            "sentence": e.sentence,
                            "updated_at": ts.isoformat() if ts else None,
                            "attributes": (e.attributes or {}) if include_attributes else {},
                            "source_hops": None,
                            "target_hops": None,
                        })

                    scored_edges.sort(key=lambda x: (x["sim"], x["updated_at"] or "", x["edge_id"]), reverse=True)
                    top_edges = scored_edges[:k_edges]

                    evidence_node_ids: set[str] = set()
                    for se in top_edges:
                        evidence_node_ids.add(str(se.get("source_id")))
                        evidence_node_ids.add(str(se.get("target_id")))

                    evidence_nodes: List[Dict[str, Any]] = []
                    for nid in sorted([n for n in evidence_node_ids if n]):
                        n = node_by_id.get(nid)
                        if not n:
                            continue
                        evidence_nodes.append({
                            "node_id": nid,
                            "label": n.label,
                            "type": n.node_type,
                            "hops": None,
                            "description": _short(n.description, max_description_chars),
                            "aliases": list(n.aliases or []),
                            "attributes": (n.attributes or {}) if include_attributes else {},
                        })

                    evidence = {
                        "base_node": {"label": "global", "node_id": None, "type": "global"},
                        "neighborhood_stats": {
                            "mode": "global_chroma",
                            "chroma_candidates": len(chroma_results),
                            "selected_edge_evidence": len(top_edges),
                            "evidence_ranker": "embedding_global_chroma",
                        },
                        "nodes": evidence_nodes,
                        "edges": top_edges,
                    }

                    return self._run_rag_agent(
                        question=question,
                        top_edges=top_edges,
                        evidence=evidence,
                        base_node_label="global",
                        base_node_id=None,
                        engine=engine,
                        timeout=timeout,
                        return_evidence=return_evidence,
                        min_similarity=min_similarity,
                    )

            # ── NODE-ANCHORED MODE ─────────────────────────────────────────
            base_node: Optional[Node] = None
            base_node_id = node_query

            base_node = self.session.get(Node, base_node_id)
            if not base_node:
                base_node = _pick_node_by_label(self.session, node_query)
            if not base_node:
                # Last resort: try primary user
                from app.assistant.kg_core.user_identity import get_primary_user_name
                fallback = get_primary_user_name()
                base_node = _pick_node_by_label(self.session, fallback)
                if not base_node:
                    raise ValueError(f"Base node not found for node='{node_query}'.")
                logger.debug("[ask_kg] node '%s' not found; falling back to primary user '%s'", node_query, fallback)

            base_node_id = str(base_node.id)

            try:
                base_degree = self.session.query(Edge).filter(
                    or_(Edge.source_id == base_node_id, Edge.target_id == base_node_id)
                ).count()
            except Exception:
                logger.debug("Could not compute base_degree for node %s", base_node_id, exc_info=True)
                base_degree = 0
            if isinstance(base_degree, int) and base_degree > 0:
                max_nodes = max(int(max_nodes), min(5000, int(base_degree) + 1))
                max_edges = max(int(max_edges), min(20000, int(base_degree) + 250))

            distance_by_node_id, edges = _expand_neighborhood_fast(
                self.session,
                base_node_id,
                max_hops=max_hops,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )

            hop_hist: Dict[int, int] = {}
            for _, d in distance_by_node_id.items():
                if isinstance(d, int):
                    hop_hist[d] = hop_hist.get(d, 0) + 1
            max_distance = max(distance_by_node_id.values()) if distance_by_node_id else 0

            node_ids = list(distance_by_node_id.keys())
            nodes_list: List[Node] = []
            if node_ids:
                nodes_list = self.session.query(Node).filter(Node.id.in_(node_ids)).all()
            node_by_id: Dict[str, Node] = {str(n.id): n for n in nodes_list if n is not None}

            scored_edges_local: List[Dict[str, Any]] = []
            missing_embedding_count = 0

            for e in edges:
                if not e or not e.sentence:
                    continue
                emb = e.sentence_embedding
                if emb is None:
                    missing_embedding_count += 1
                    continue
                try:
                    sim = float(cosine_similarity(q_emb, emb))
                except Exception:
                    logger.debug("cosine_similarity failed for edge", exc_info=True)
                    continue
                if sim < float(min_similarity):
                    continue
                src_id = str(e.source_id)
                tgt_id = str(e.target_id)
                src = node_by_id.get(src_id)
                tgt = node_by_id.get(tgt_id)
                ts = e.updated_at or e.created_at
                scored_edges_local.append({
                    "edge_id": str(e.id),
                    "sim": round(float(sim), 4),
                    "type": e.relationship_type,
                    "source_id": src_id,
                    "target_id": tgt_id,
                    "source_label": src.label if src else None,
                    "source_type": src.node_type if src else None,
                    "target_label": tgt.label if tgt else None,
                    "target_type": tgt.node_type if tgt else None,
                    "sentence": e.sentence,
                    "updated_at": ts.isoformat() if ts else None,
                    "attributes": (e.attributes or {}) if include_attributes else {},
                    "source_hops": distance_by_node_id.get(src_id),
                    "target_hops": distance_by_node_id.get(tgt_id),
                })

            scored_edges_local.sort(key=lambda x: (x["sim"], x["updated_at"] or "", x["edge_id"]), reverse=True)
            top_edges = scored_edges_local[:k_edges]

            evidence_node_ids_local: set[str] = {base_node_id}
            for se in top_edges:
                evidence_node_ids_local.add(str(se.get("source_id")))
                evidence_node_ids_local.add(str(se.get("target_id")))

            evidence_nodes: List[Dict[str, Any]] = []
            for nid in sorted([n for n in evidence_node_ids_local if n]):
                n = node_by_id.get(nid)
                if not n:
                    continue
                evidence_nodes.append({
                    "node_id": nid,
                    "label": n.label,
                    "type": n.node_type,
                    "hops": distance_by_node_id.get(nid),
                    "description": _short(n.description, max_description_chars),
                    "aliases": list(n.aliases or []),
                    "attributes": (n.attributes or {}) if include_attributes else {},
                })

            evidence = {
                "base_node": {
                    "node_id": base_node_id,
                    "label": base_node.label,
                    "type": base_node.node_type,
                    "description": _short(base_node.description, max_description_chars),
                    "aliases": list(base_node.aliases or []),
                    "attributes": (base_node.attributes or {}) if include_attributes else {},
                },
                "neighborhood_stats": {
                    "mode": "node_anchored_bfs",
                    "max_hops": int(max_hops),
                    "visited_nodes": len(distance_by_node_id),
                    "collected_edges": len(edges),
                    "selected_edge_evidence": len(top_edges),
                    "evidence_ranker": "embedding_local_neighborhood",
                    "max_distance_reached": int(max_distance),
                    "hit_max_nodes": bool(len(distance_by_node_id) >= int(max_nodes)),
                    "hit_max_edges": bool(len(edges) >= int(max_edges)),
                    "hop_histogram": {str(k): v for k, v in sorted(hop_hist.items(), key=lambda kv: kv[0])},
                    "missing_edge_embedding_sample_count": int(missing_embedding_count),
                },
                "nodes": evidence_nodes,
                "edges": top_edges,
            }

            return self._run_rag_agent(
                question=question,
                top_edges=top_edges,
                evidence=evidence,
                base_node_label=base_node.label,
                base_node_id=base_node_id,
                engine=engine,
                timeout=timeout,
                return_evidence=return_evidence,
                min_similarity=min_similarity,
            )

        except Exception as e:
            logger.error("Error in handle_ask_kg")
            logger.debug("error in handle_ask_kg exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="ask_kg_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )

    def _run_rag_agent(
        self,
        *,
        question: str,
        top_edges: List[Dict[str, Any]],
        evidence: Dict[str, Any],
        base_node_label: str,
        base_node_id: Optional[str],
        engine: str,
        timeout: int,
        return_evidence: bool,
        min_similarity: float,
    ) -> ToolResult:
        """Shared LLM step for both node-anchored and global ask_kg modes."""
        # Evidence is fully gathered (plain strings) — release the handler
        # session so it doesn't ride the RAG agent call.
        self._release_session()
        if not top_edges:
            msg = (
                f"No embedded edge evidence found for '{base_node_label}' "
                f"(min_similarity={min_similarity})."
            )
            return self.publish_error(
                make_tool_error(
                    error_code="ask_kg_no_evidence",
                    message=msg,
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )

        rag_agent = DI.agent_factory.create_agent("shared::ask_kg")
        if rag_agent is None:
            return self.publish_error(
                make_tool_error(
                    error_code="ask_kg_agent_missing",
                    message="Could not instantiate shared::ask_kg agent.",
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )

        rag_msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "task": "Answer the question from KG evidence only.",
                "question": question,
                "evidence": evidence,
                "engine": engine,
                "timeout": int(timeout),
            }
        )
        raw = rag_agent.action_handler(rag_msg)

        answer = None
        confidence = None
        citations = None
        if isinstance(raw, ToolResult):
            payload = raw.data if isinstance(raw.data, dict) else {}
            if getattr(raw, "result_type", None) == "error":
                return self.publish_error(
                    make_tool_error(
                        error_code="ask_kg_agent_error",
                        message=str(getattr(raw, "content", "") or "ask_kg agent returned error"),
                        abort_policy="abort_tool",
                        retryable=False,
                        details=payload if payload else None,
                    )
                )
            if isinstance(payload, dict):
                answer = payload.get("answer")
                confidence = payload.get("confidence")
                citations = payload.get("citations")
        elif isinstance(raw, dict):
            answer = raw.get("answer")
            confidence = raw.get("confidence")
            citations = raw.get("citations")
        if not answer:
            answer = str(raw)

        data: Dict[str, Any] = {
            "answer": answer,
            "confidence": confidence,
            "citations": citations or [],
            "base_node_id": base_node_id,
            "base_node_label": base_node_label,
            "engine": engine,
        }
        if return_evidence:
            data["evidence"] = evidence

        return self.publish_result(ToolResult(result_type="ask_kg", content=answer, data=data))
    def handle_kg_describe_node(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        try:
            node_id = arguments.get("node_id")
            if not node_id:
                raise ValueError("Missing 'node_id' in arguments.")
            
            filters = arguments.get("filters", None)
            max_edges_value = arguments.get("max_edges", 50)
            max_edges = int(max_edges_value) if max_edges_value is not None else 50

            # Use the updated describe_node function that handles temporal filtering
            from app.assistant.kg_core.kg_utils.kg_tools import describe_node, parse_filters_from_pydantic
            
            # Parse filters if provided
            parsed_filters = None
            if filters:
                parsed_filters = parse_filters_from_pydantic(filters)
            
            # Call the describe_node function with temporal filtering support
            # Convert string node_id to UUID if needed
            if isinstance(node_id, str):
                node_id = uuid.UUID(node_id)
            description = describe_node(node_id, self.session, parsed_filters, max_edges)

            return self.publish_result(
                ToolResult(
                    result_type="node_description",
                    content=f"Description of node '{description['label']}'",
                    data=description
                )
            )

        except Exception as e:
            logger.error("Error in handle_describe_node")
            logger.debug("error in handle_describe_node exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="kg_get_edges_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )

    def handle_kg_short_describe_node(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        node_id = arguments.get("node_id")
        if not node_id:
            raise ValueError("Missing 'node_id'.")
        data = short_describe_node(node_id, self.session, k_edges=5)
        return self.publish_result(ToolResult(result_type="node_short_description", content=f"Short description for {data['label']}", data=data))


    def handle_kg_find_edge(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        try:
            node_id = arguments.get("node_id")
            if not node_id:
                raise ValueError("Missing 'node_id'.")

            direction = arguments.get("dir", "both")  # 'in', 'out', or 'both'
            k_value = arguments.get("k", 5)
            k = int(k_value) if k_value is not None else 5
            text = (arguments.get("text") or "").strip().lower()
            type_filter = arguments.get("type_filter")  # str or List[str]
            start_time_after = arguments.get("start_time_after")  # ISO string optional
            trivial_types = set(arguments.get("trivial_types", ["has_email"]))
            importance_default_value = arguments.get("importance_default", 0.5)
            importance_default = float(importance_default_value) if importance_default_value is not None else 0.5

            node = self.session.get(Node, node_id)
            if not node:
                raise ValueError(f"Node {node_id} not found.")

            # Normalize type_filter
            if isinstance(type_filter, str):
                type_filter = [type_filter]
            type_filter = set(type_filter) if type_filter else None

            # Base queries
            q_in = self.session.query(Edge).filter(Edge.target_id == node.id)
            q_out = self.session.query(Edge).filter(Edge.source_id == node.id)

            if type_filter:
                q_in = q_in.filter(Edge.relationship_type.in_(type_filter))
                q_out = q_out.filter(Edge.relationship_type.in_(type_filter))

            if start_time_after:
                try:
                    sta = datetime.fromisoformat(start_time_after.replace("Z", "+00:00"))
                    q_in = q_in.filter(or_(Edge.updated_at >= sta, Edge.created_at >= sta))
                    q_out = q_out.filter(or_(Edge.updated_at >= sta, Edge.created_at >= sta))
                except Exception as e:
                    logger.debug(f"Ignoring invalid start_time_after='{start_time_after}': {e}", exc_info=True)

            q_in = q_in.order_by(desc(Edge.updated_at), desc(Edge.created_at)).limit(max(k * 6, 24))
            q_out = q_out.order_by(desc(Edge.updated_at), desc(Edge.created_at)).limit(max(k * 6, 24))

            edges: List[tuple] = []
            if direction in ("in", "both"):
                edges += [("in", e) for e in q_in.all()]
            if direction in ("out", "both"):
                edges += [("out", e) for e in q_out.all()]

            # Score edges
            scored = []
            for d, e in edges:
                other = e.source_node if d == "in" else e.target_node
                if not other:
                    continue

                # Importance: per-edge attribute > Edge.importance column > default
                edge_imp_val = None
                if e.attributes:
                    edge_imp_val = e.attributes.get("importance")
                    if isinstance(edge_imp_val, str):
                        try:
                            edge_imp_val = float(edge_imp_val)
                        except ValueError:
                            edge_imp_val = None
                if edge_imp_val is None and e.importance is not None:
                    edge_imp_val = e.importance
                imp = float(edge_imp_val) if edge_imp_val is not None else importance_default

                # Recency
                ts = e.updated_at or e.created_at
                rec = recency_score(ts)

                # Text relevance, cheap heuristic (no embed here)
                txt_score = 0.0
                if text:
                    lbl = (other.label or "").lower()
                    etname = (e.relationship_type or "").lower()
                    aliases = [a.lower() for a in (other.aliases or [])]
                    if text == lbl:
                        txt_score = 1.0
                    elif any(text == a for a in aliases):
                        txt_score = 0.95
                    elif text in lbl:
                        txt_score = 0.8
                    elif any(text in a for a in aliases):
                        txt_score = 0.7
                    elif text in etname:
                        txt_score = 0.6

                # Blend
                score = 0.45 * txt_score + 0.35 * imp + 0.20 * rec

                scored.append({
                    "edge_id": str(e.id),
                    "direction": d,
                    "edge_type": e.relationship_type,
                    "other_node_id": str(other.id),
                    "other_node_label": other.label,
                    "importance": round(float(imp), 4),
                    "updated_at": ts.isoformat() if ts else None,
                    "score": round(float(score), 4),
                    "why": [s for s in [
                        f"importance {imp:.2f}" if imp else None,
                        "recent" if rec > 0.5 else None,
                        f"text≈{txt_score:.2f}" if txt_score > 0 else None
                    ] if s]
                })

            # Sort by score then recency
            scored.sort(key=lambda x: (x["score"], x["updated_at"] or ""), reverse=True)

            # De-dup inverse duplicates and enforce diversity
            seen_pair = set()  # (edge_type, other_node_id)
            seen_per_type: Dict[str, int] = {}
            result = []
            for s in scored:
                pair = (s["edge_type"], s["other_node_id"])
                if pair in seen_pair:
                    continue
                cap = 1 if s["edge_type"] in trivial_types else 2
                if seen_per_type.get(s["edge_type"], 0) >= cap:
                    continue
                result.append(s)
                seen_pair.add(pair)
                seen_per_type[s["edge_type"]] = seen_per_type.get(s["edge_type"], 0) + 1
                if len(result) >= k:
                    break

            return self.publish_result(
                ToolResult(
                    result_type="edge_search",
                    content=f"Top {len(result)} edge(s) for node '{node.label}'",
                    data_list=result
                )
            )

        except Exception as e:
            logger.error("Error in handle_find_edges")
            logger.debug("error in handle_find_edges exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="ask_kg_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )



    def handle_kg_describe_edge(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        try:
            edge_id = arguments.get("edge_id")
            if not edge_id:
                raise ValueError("Missing 'edge_id' in arguments.")

            # Call your describe function
            data = describe_edge(edge_id=edge_id, session=self.session)

            # Optional perspective to make direction explicit
            perspective_node_id = arguments.get("perspective_node_id")
            if perspective_node_id and data.get("source") and data.get("target"):
                src_id = str(data["source"].get("node_id"))
                tgt_id = str(data["target"].get("node_id"))
                pid = str(perspective_node_id)
                if pid == src_id:
                    data["direction_from_perspective"] = "out"
                elif pid == tgt_id:
                    data["direction_from_perspective"] = "in"
                else:
                    data["direction_from_perspective"] = "none"

            return self.publish_result(
                ToolResult(
                    result_type="edge_description",
                    content=f"Description of edge '{data.get('type', '')}'",
                    data=data,
                )
            )

        except Exception as e:
            logger.error("Error in handle_describe_edge")
            logger.debug("error in handle_describe_edge exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="kg_search_generic_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )


if __name__ == "__main__":
    import sys
    from uuid import UUID

    tool = KnowledgeGraphSearch()

    print("\n=== Testing semantic_find_node_by_text ===")
    search_msg = ToolMessage(
        tool_name="kg_find_node",
        tool_data={
            "tool_name": "kg_find_node",
            "arguments": {"text": "Katy", "threshold": 0.75, "k": 3, "edges_k": 3},
        },
    )
    result = tool.execute(search_msg)
    print(result)

    if not result.data_list:
        print("\nNo nodes found in semantic search. Skipping follow-ups.")
        sys.exit(0)

    first_node_id = result.data_list[0]["node_id"]
    try:
        UUID(first_node_id)
    except ValueError:
        print(f"Invalid node_id format: {first_node_id}")
        sys.exit(1)

    # Short describe via handler, not direct function call
    print("\n=== Testing short_describe_node ===")
    short_msg = ToolMessage(
        tool_name="kg_short_describe_node",  # add a handler for this as suggested
        tool_data={
            "tool_name": "kg_short_describe_node",
            "arguments": {"node_id": first_node_id, "k_edges": 5},
        },
    )
    short_res = tool.execute(short_msg)
    print(short_res)

    # Find edges by text from the node
    print("\n=== Testing find_edges (text='work') ===")
    edges_msg = ToolMessage(
        tool_name="kg_find_edges",
        tool_data={
            "tool_name": "kg_find_edges",
            "arguments": {
                "node_id": first_node_id,
                "text": "work",     # try also "email" or "married"
                "dir": "both",
                "k": 5,
                "type_filter": None,   # or ["works_for", "has_email"]
                "start_time_after": None, # or "2024-01-01T00:00:00Z"
            },
        },
    )
    edges_res = tool.execute(edges_msg)
    print(edges_res)

    if edges_res.data_list:
        first_edge_id = edges_res.data_list[0]["edge_id"]

        print(f"\n=== Testing describe_edge ({first_edge_id}) ===")
        edge_msg = ToolMessage(
            tool_name="kg_describe_edge",
            tool_data={
                "tool_name": "kg_describe_edge",
                "arguments": {
                    "edge_id": first_edge_id
                }
            }
        )
        edge_res = tool.execute(edge_msg)
        print(edge_res)
    else:
        print("\nNo edges found to describe.")

    # Clean up
    if tool.session:
        tool.session.close()
