"""
Taxonomy Orchestrator - Hierarchical Beam Search Taxonomy Classification.

Each node is classified independently based on what it IS, not its relationships.
Uses beam search to navigate the taxonomy tree and find the best classification path.
"""
from typing import Dict, List, Optional, Tuple
from app.assistant.kg_core.taxonomy.manager import TaxonomyManager
from app.assistant.kg_core.taxonomy.models import Taxonomy
from app.assistant.utils.pydantic_classes import Message
from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
from app.assistant.kg_core.user_identity import get_primary_user_name
from app.assistant.utils.assistant_name import get_assistant_name
from app.assistant.utils.logging_config import get_logger
import math

logger = get_logger(__name__)

# Configuration - Viterbi Beam Search with Insertions
BEAM_WIDTH = 3  # Keep top-k beams at each depth (smaller for efficiency)
ALPHA_MARGIN = 0.04  # Keep additional beams within this margin of best
CHILD_FANOUT = 8  # Max children to consider per parent (by similarity)
EPSILON_GAIN = 0.01  # Early stop if gain < this
MAX_DEPTH = 7  # Maximum taxonomy depth to explore
WINDOW_SIZE = 2  # Allow matching hints out-of-order within this window

# Scoring weights - Two-phase beam search
W_SIM = 1.0    # Semantic similarity (always active)
W_PRIOR = 0.5  # Usage frequency
W_HINT = 0.8   # Bonus for matching a hint
W_ORDER_BONUS = 0.1   # Bonus for hints appearing in LLM order
W_ORDER_PENALTY = 0.15  # Penalty for severe out-of-order (inversions)

# Position weights for hints (c1 matters more than c3 - LLM ranked importance)
HINT_POS_WEIGHTS = [1.0, 0.7, 0.5, 0.35, 0.25]

# Hint matching threshold
HINT_MATCH_THRESHOLD = 0.25  # Minimum similarity to consider a hint match

# Priors
BETA_SMOOTHING = 1.0  # Dirichlet smoothing


class TaxonomyOrchestrator:
    """
    Orchestrates taxonomy classification using hierarchical beam search.
    
    Each node is classified independently based on what it IS, not its relationships.
    Workflow:
      1) Start at node_type root (Event, Entity, State, etc.)
      2) Use beam search to navigate taxonomy tree
      3) LLM tie-breaker picks best path from top candidates
    """
    
    def __init__(self, manager: Optional[TaxonomyManager] = None):
        self.manager = manager
        self.kg_utils = None  # Lazy init

    def _release_db_before_llm(self, session=None) -> None:
        """
        IMPORTANT (SQLite):
        Do not hold open SQLAlchemy sessions/connections across LLM calls.

        Even read sessions can keep a transaction open; and any accidental write transaction
        held during a slow LLM call can starve other writers (e.g., AFK active_segments).
        """
        candidates = []
        if session is not None:
            candidates.append(session)
        try:
            candidates.append(getattr(self.manager, "session", None))
        except Exception:
            candidates.append(None)
        try:
            candidates.append(getattr(self.kg_utils, "session", None))
        except Exception:
            candidates.append(None)

        for s in candidates:
            try:
                if s is not None and hasattr(s, "close"):
                    s.close()
            except Exception:
                continue
    
    def classify_node(
        self,
        node: Dict,
        path_generator_agent=None,  # For concept extraction
        verifier_agent=None,  # For final validation
        branch_selector_agent=None,  # For smart pruning at each level
        session=None,
        return_top_paths=False  # NEW: Return top 3 paths for path corrector optimization
    ) -> Tuple[Optional[int], float, str]:
        """
        Classify a node using HYBRID approach: LLM-guided descent + Beam search fallback.
        
        Strategy:
        1. Try LLM-guided descent (more accurate, interpretable)
        2. If that fails or low confidence, fall back to beam search
        3. Compare results and pick best
        
        Args:
            node: Dict with label, sentence, node_type
            path_generator_agent: For concept extraction
            branch_selector_agent: For LLM-guided descent
            session: Database session
            
        Returns:
            Tuple of (taxonomy_id, confidence, source)
            - taxonomy_id: ID of the taxonomy type (or None if no match)
            - confidence: Normalized score (0.0-1.0)
            - source: "llm_guided", "beam_search", or "hybrid"
        """
        # Initialize
        if not self.manager:
            self.manager = TaxonomyManager(session)
        if not self.kg_utils:
            self.kg_utils = KnowledgeGraphUtils(session)
        
        label = node.get("label", "")
        sentence = node.get("sentence", "")
        node_type = node.get("node_type", "")
        category = node.get("category", "")
        
        if not label:
            logger.warning("Node missing label, cannot classify")
            return (None, 0.0, "error")
        
        # SYSTEM ENTITIES: Hardcoded classifications for known entities
        if node_type == "Entity":
            label_lower = label.lower().strip()
            if label_lower == get_primary_user_name().lower():
                user_tax = session.query(Taxonomy).filter_by(label="user").first()
                if user_tax:
                    path = self.manager.get_taxonomy_path(user_tax.id)
                    logger.debug(f"Classifying '{label}' as system entity (primary user): {' > '.join(path)}")
                    return (user_tax.id, 1.0, "matched")
            
            elif label_lower == get_assistant_name().lower():
                assistant_tax = session.query(Taxonomy).filter_by(label="assistant").first()
                if assistant_tax:
                    path = self.manager.get_taxonomy_path(assistant_tax.id)
                    logger.debug(f"Classifying '{label}' as system entity (assistant): {' > '.join(path)}")
                    return (assistant_tax.id, 1.0, "matched")
        
        # Get taxonomy root (needed for both methods)
        root = node_type.lower()
        root_taxonomy = session.query(Taxonomy).filter(
            Taxonomy.label == root,
            Taxonomy.parent_id.is_(None)
        ).first()
        
        if not root_taxonomy:
            logger.error(f"No taxonomy root found for node_type '{node_type}'")
            return self._create_placeholder(label, sentence, session)
        
        # ========================================================================
        # HYBRID APPROACH: Run BOTH methods, let critic choose best
        # ========================================================================
        
        llm_result = None
        llm_path_str = None
        llm_confidence = 0.0
        
        # METHOD 1: LLM-Guided Descent (if agent available)
        llm_top_paths = []  # Store top paths for path corrector optimization
        llm_candidates = []  # Store ALL candidate paths for critic
        if branch_selector_agent:
            try:
                llm_result = self._llm_guided_descent(
                    label=label,
                    sentence=sentence,
                    node_type=node_type,
                    category=category,
                    root_id=root_taxonomy.id,
                    branch_selector_agent=branch_selector_agent,
                    session=session,
                    return_top_paths=True  # Always get top paths for critic
                )
                
                # Handle dict format with top_paths
                if isinstance(llm_result, dict) and 'top_paths' in llm_result:
                    llm_top_paths = llm_result['top_paths']
                    # Store for path corrector optimization
                    self.last_top_paths = llm_top_paths
                    
                    # Convert to candidate format for critic
                    for path_info in llm_top_paths[:5]:  # Top 5 candidates
                        taxonomy_id = path_info['taxonomy_id']
                        score = path_info['score']
                        path = self.manager.get_taxonomy_path(taxonomy_id)
                        path_str = " > ".join(path)
                        llm_candidates.append({
                            'taxonomy_id': taxonomy_id,
                            'path': path_str,
                            'score': score,
                            'method': 'llm_guided'
                        })
                else:
                    pass
            except Exception as e:
                logger.warning(f"LLM-guided descent failed: {e}")
        # METHOD 2: Beam Search (always run for comparison)
        # Step 1: Extract type hints (concepts) from label + sentence
        type_hints = self._extract_type_concepts(label, sentence, node_type, path_generator_agent)
        # Step 2: Compute embeddings for hints (used for alignment scoring)
        hint_embeds = self._compute_multi_query_embeddings(type_hints)
        
        # Step 3: Run Viterbi beam search with hint alignment
        best_paths = self._viterbi_beam_search(
            hints=type_hints,  # List of hint strings (e.g., ["tv show", "competition", "combat"])
            hint_embeds=hint_embeds,  # List of embeddings for hints
            root_id=root_taxonomy.id,
            label=label,
            sentence=sentence,
            node_type=node_type,
            category=category,
            branch_selector_agent=branch_selector_agent,  # For smart pruning at depth 0
            session=session
        )
        
        beam_candidates = []  # Store ALL candidate paths for critic
        
        if best_paths:
            # Step 4: Validate paths - filter by minimum score threshold
            validated_paths = self._validate_scored_paths(best_paths, hint_embeds, session)
            
            if validated_paths:
                # Convert to candidate format for critic (top 5)
                for path_info in validated_paths[:5]:
                    beam_taxonomy_id = path_info["path"][-1]  # Leaf node
                    beam_score = min(1.0, path_info["avg_score"])  # Cap at 1.0
                    beam_path = self.manager.get_taxonomy_path(beam_taxonomy_id)
                    beam_path_str = " > ".join(beam_path)
                    beam_candidates.append({
                        'taxonomy_id': beam_taxonomy_id,
                        'path': beam_path_str,
                        'score': beam_score,
                        'method': 'beam_search'
                    })
            else:
                pass
        else:
            pass
        # ========================================================================
        # RETURN ALL CANDIDATES: Let the pipeline's critic choose
        # ========================================================================
        
        # Combine all candidates from both methods
        all_candidates = llm_candidates + beam_candidates
        
        # If we have NO results from either method, create placeholder
        if not all_candidates:
            logger.warning(f"Both methods failed for '{label}'")
            return self._create_placeholder(label, sentence, session)
        
        # Sort by score (best first)
        all_candidates.sort(key=lambda x: x['score'], reverse=True)
        # Return in new format: dict with 'candidates' list
        return {
            'candidates': all_candidates,
            'llm_top_paths': llm_top_paths  # For path corrector optimization
        }
    
    def _extract_type_concepts(self, label: str, sentence: str, node_type: str, agent) -> List[str]:
        """
        Use LLM to extract TYPE concepts from label + sentence.
        
        For "BattleBots" + "topic of discussion" → ["tv show", "competition", "robots", "entertainment"]
        For "Sam" + "is discussing" → ["person", "user"]
        
        Returns: List of 1-5 concept strings
        """
        if not agent:
            # Fallback: just use the label
            return [label]
        
        # Use the path_generator agent to extract concepts (repurposing it)
        agent_input = {
            "node_label": label,
            "node_sentence": sentence or "",
            "node_type": node_type,
            "mode": "extract_concepts"  # Signal to extract, not generate path
        }
        
        try:
            self._release_db_before_llm()
            response = agent.action_handler(Message(agent_input=agent_input))
            result = getattr(response, "data", {}) or {}
            
            # The agent should return sub_path as concepts
            concepts = result.get("sub_path", [])
            
            if not concepts:
                # Fallback to label
                return [label]
            
            return concepts[:5]  # Cap at 5 concepts
            
        except Exception as e:
            logger.warning(f"Concept extraction failed: {e}, using label as fallback")
            return [label]
    
    def _compute_multi_query_embeddings(self, concepts: List[str]):
        """
        Compute embeddings for multiple type concepts.
        
        Returns: List of normalized embeddings (one per concept)
        """
        import numpy as np
        
        embeds = []
        for concept in concepts:
            embed = np.array(self.kg_utils.create_embedding(concept))
            # L2 normalize
            norm = np.linalg.norm(embed)
            if norm > 0:
                embed = embed / norm
            embeds.append(embed)
        
        return embeds
    
    def _compute_query_embedding(self, label: str, sentence: str, node_type: str):
        """Compute weighted query embedding based on node type."""
        import numpy as np
        
        # Get embeddings and convert to numpy arrays
        label_embed = np.array(self.kg_utils.create_embedding(label))
        
        # Choose weights based on node type
        if node_type == "Entity":
            w_label, w_sent = ENTITY_WEIGHTS
        else:
            w_label, w_sent = OTHER_WEIGHTS
        
        # Weighted average
        if sentence and sentence.strip():
            sent_embed = np.array(self.kg_utils.create_embedding(sentence))
            query_embed = w_label * label_embed + w_sent * sent_embed
        else:
            query_embed = label_embed
        
        # L2 normalize
        norm = np.linalg.norm(query_embed)
        if norm > 0:
            query_embed = query_embed / norm
        
        return query_embed
    
    def _viterbi_beam_search(
        self,
        hints: List[str],
        hint_embeds: List,
        root_id: int,
        label: str,
        sentence: str,
        node_type: str,
        category: str,
        branch_selector_agent,
        session
    ) -> List[Dict]:
        """
        Two-phase beam search with global hint assignment.
        
        Phase 1 (Exploration): Build paths, track hint scores at each node
        Phase 2 (Assignment): Globally assign hints to best nodes, compute order bonuses
        
        This treats hints as an unordered bag of concepts, not a sequence.
        
        Returns: List of completed paths sorted by score (best first)
        """

        num_hints = len(hints)
        # Initialize beam with root state
        # No hint consumption tracking - that happens in Phase 2!
        beams = [{
            "node_id": root_id,
            "score": 0.0,
            "path": [root_id],
            "hint_scores_by_node": {},  # {node_id: {hint_idx: score, ...}}
            "depth": 0
        }]
        
        completed_paths = []
        
        for depth in range(MAX_DEPTH):
            next_beams = []
            
            for beam in beams:
                current_id = beam["node_id"]
                current_score = beam["score"]
                hint_scores_so_far = beam["hint_scores_by_node"]
                
                # Get children
                children = session.query(Taxonomy).filter(
                    Taxonomy.parent_id == current_id
                ).all()
                
                if not children:
                    # Leaf node - save as completed path
                    completed_paths.append(beam.copy())
                    continue
                
                # SMART PRUNING: At depth 0, use LLM to select top branches
                if depth == 0 and branch_selector_agent and len(children) > 5:
                    children = self._llm_select_branches(
                        children=children,
                        label=label,
                        sentence=sentence,
                        node_type=node_type,
                        category=category,
                        current_path=self._get_path_string(beam["path"], session),
                        agent=branch_selector_agent
                    )
                # Pre-filter children by semantic similarity (keep top CHILD_FANOUT)
                children_before = len(children)
                children = self._prefilter_children_by_similarity(children, hint_embeds)
                # Expand beam to all children
                for child in children:
                    # Compute base scores
                    sem_sim = self._compute_semantic_sim(child, hint_embeds)
                    prior_score = self._get_usage_prior(child.id, current_id, session)
                    base_score = W_SIM * sem_sim + W_PRIOR * prior_score
                    
                    # Compute similarity to ALL hints (Phase 1: exploratory)
                    child_hint_scores = self._compute_all_hint_scores(child, hints, hint_embeds)
                    
                    # Debug at depth 0
                    if depth == 0:
                        best_hint_idx = max(child_hint_scores, key=child_hint_scores.get) if child_hint_scores else None
                    # For this hop, use MAX hint score (don't consume yet!)
                    max_hint_score = max(child_hint_scores.values()) if child_hint_scores else 0.0
                    hop_score = current_score + base_score + max_hint_score  # Temp score for beam pruning
                    
                    # Copy and update hint scores metadata
                    new_hint_scores = hint_scores_so_far.copy()
                    if child_hint_scores:
                        new_hint_scores[child.id] = child_hint_scores
                    
                    next_beams.append({
                        "node_id": child.id,
                        "score": hop_score,  # Temporary (will be recomputed in Phase 2)
                        "path": beam["path"] + [child.id],
                        "hint_scores_by_node": new_hint_scores,
                        "depth": depth + 1
                    })
            
            if not next_beams:
                break
            
            # Prune beams: keep top-K by score + alpha-margin
            next_beams.sort(key=lambda x: x["score"], reverse=True)
            
            if len(next_beams) > BEAM_WIDTH:
                kept = next_beams[:BEAM_WIDTH]
                best_score = kept[0]["score"]
                
                # Also keep beams within alpha-margin
                for beam in next_beams[BEAM_WIDTH:]:
                    if beam["score"] >= best_score - ALPHA_MARGIN:
                        kept.append(beam)
                
                next_beams = kept
            
            # Early stopping: if last gain was small
            if depth > 2 and beams:
                best_old = max(b["score"] for b in beams[:3])
                best_new = next_beams[0]["score"]
                gain = best_new - best_old
                
                if gain < EPSILON_GAIN:
                    # Save top beams as completed
                    for beam in next_beams[:BEAM_WIDTH * 2]:  # Save more for Phase 2
                        completed_paths.append(beam.copy())
                    break
            
            beams = next_beams
        
        # Add remaining beams to completed paths
        for beam in beams:
            completed_paths.append(beam.copy())
        # === PHASE 2: Global hint assignment ===
        for path in completed_paths:
            # Globally assign hints to best nodes in this path
            hint_assignments, hint_bonus = self._assign_hints_globally(
                path["hint_scores_by_node"], hints, num_hints
            )
            
            # Compute ordering bonus/penalty
            order_score = self._compute_ordering_score(hint_assignments, num_hints)
            
            # Recompute final score
            # Base score was W_SIM + W_PRIOR per hop (already in path["score"])
            # Now add global hint bonuses + ordering
            path["hint_bonus"] = hint_bonus
            path["order_score"] = order_score
            path["final_score"] = path["score"] + hint_bonus + order_score
            path["hint_assignments"] = hint_assignments  # For debugging
            path["hints_matched"] = len(hint_assignments)
            path["depth"] = len(path["path"])
        
        # Sort by final score
        completed_paths.sort(key=lambda x: x["final_score"], reverse=True)
        
        # Normalize for output compatibility
        for path in completed_paths:
            path["avg_score"] = path["final_score"] / path["depth"] if path["depth"] > 0 else 0.0
        
        # Debug output
        if completed_paths:
            for i, p in enumerate(completed_paths[:5], 1):
                path_labels = [self.manager.get_taxonomy_by_id(tid).label for tid in p["path"]]
                matched_hints = [hints[h_idx] for _, h_idx, _ in p["hint_assignments"]]
                matched_str = f" [{', '.join(matched_hints[:3])}]" if matched_hints else ""
        return completed_paths
    
    def _compute_all_hint_scores(self, child: Taxonomy, hints: List[str], hint_embeds: List) -> Dict[int, float]:
        """
        Compute similarity scores between child and ALL hints.
        
        Returns: {hint_idx: score} for hints above threshold
        """
        import numpy as np
        
        if child.label_embedding is None:
            return {}
        
        child_embed = np.array(child.label_embedding)
        scores = {}
        
        for hint_idx in range(len(hints)):
            # Semantic similarity
            hint_sim = np.dot(child_embed, hint_embeds[hint_idx])
            
            # Lexical similarity
            child_tokens = set(child.label.lower().replace("_", " ").split())
            hint_tokens = set(hints[hint_idx].lower().split())
            
            if child_tokens and hint_tokens:
                jaccard = len(child_tokens & hint_tokens) / len(child_tokens | hint_tokens)
                combined_sim = 0.8 * hint_sim + 0.2 * jaccard
            else:
                combined_sim = hint_sim
            
            # Only store if above threshold
            if combined_sim > HINT_MATCH_THRESHOLD:
                scores[hint_idx] = combined_sim
        
        return scores
    
    def _assign_hints_globally(
        self,
        hint_scores_by_node: Dict[int, Dict[int, float]],
        hints: List[str],
        num_hints: int
    ) -> Tuple[List[Tuple[int, int, float]], float]:
        """
        Globally assign hints to best nodes, resolving conflicts.
        
        Args:
            hint_scores_by_node: {node_id: {hint_idx: score}}
            hints: List of hint strings
            num_hints: Total number of hints
            
        Returns:
            (assignments, total_bonus)
            assignments: [(node_id, hint_idx, score), ...]
            total_bonus: Sum of W_HINT * score * pos_weight
        """
        # Find best location for each hint
        best_locations = {}  # hint_idx → (node_id, score)
        for hint_idx in range(num_hints):
            best_node = None
            best_score = 0.0
            
            for node_id, hint_scores in hint_scores_by_node.items():
                score = hint_scores.get(hint_idx, 0.0)
                if score > best_score:
                    best_score = score
                    best_node = node_id
            
            if best_node is not None:
                best_locations[hint_idx] = (best_node, best_score)
        
        # Resolve conflicts: multiple hints want same node
        # Sort hints by score (descending) and assign greedily
        node_assignments = {}  # node_id → (hint_idx, score)
        
        for hint_idx, (node_id, score) in sorted(
            best_locations.items(),
            key=lambda x: x[1][1],  # Sort by score
            reverse=True
        ):
            if node_id not in node_assignments:
                node_assignments[node_id] = (hint_idx, score)
        
        # Build final assignments list
        assignments = [
            (node_id, hint_idx, score)
            for node_id, (hint_idx, score) in node_assignments.items()
        ]
        
        # Compute total hint bonus (with position weights)
        total_bonus = 0.0
        for node_id, hint_idx, score in assignments:
            pos_weight = HINT_POS_WEIGHTS[hint_idx] if hint_idx < len(HINT_POS_WEIGHTS) else 0.2
            total_bonus += W_HINT * score * pos_weight
        
        return assignments, total_bonus
    
    def _compute_ordering_score(self, assignments: List[Tuple[int, int, float]], num_hints: int) -> float:
        """
        Compute bonus/penalty based on hint ordering in path.
        
        If hints appear in LLM order (hint[0] before hint[1] before hint[2]), bonus.
        If badly out of order (many inversions), penalty.
        
        Args:
            assignments: [(node_id, hint_idx, score), ...]
            
        Returns:
            score: positive for in-order, negative for out-of-order
        """
        if len(assignments) < 2:
            return 0.0
        
        # Sort by node_id (assumes node_ids increase along path)
        assignments_sorted = sorted(assignments, key=lambda x: x[0])
        hint_indices = [hint_idx for _, hint_idx, _ in assignments_sorted]
        
        # Count inversions (pairs where i < j but hint[i] > hint[j])
        inversions = 0
        in_order = 0
        
        for i in range(len(hint_indices)):
            for j in range(i + 1, len(hint_indices)):
                if hint_indices[i] < hint_indices[j]:
                    in_order += 1
                else:
                    inversions += 1
        
        # Bonus if mostly in order, penalty if many inversions
        total_pairs = in_order + inversions
        if total_pairs == 0:
            return 0.0
        
        order_ratio = in_order / total_pairs
        
        if order_ratio > 0.7:
            # Mostly in order - bonus
            return W_ORDER_BONUS * order_ratio
        elif order_ratio < 0.3:
            # Badly out of order - penalty
            return -W_ORDER_PENALTY * (1.0 - order_ratio)
        else:
            # Mixed - neutral
            return 0.0
    
    def _prefilter_children_by_similarity(self, children: List[Taxonomy], hint_embeds: List) -> List[Taxonomy]:
        """
        Pre-filter children by semantic similarity to hints.
        Keep top CHILD_FANOUT children.
        """
        import numpy as np
        
        if len(children) <= CHILD_FANOUT:
            return children
        
        scored = []
        for child in children:
            if child.label_embedding is not None:
                child_embed = np.array(child.label_embedding)
                # Max similarity to any hint
                sims = [np.dot(child_embed, h_embed) for h_embed in hint_embeds]
                max_sim = max(sims) if sims else 0.0
                scored.append((child, max_sim))
            else:
                scored.append((child, 0.0))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        return [child for child, sim in scored[:CHILD_FANOUT]]
    
    def _compute_semantic_sim(self, child: Taxonomy, hint_embeds: List) -> float:
        """
        Compute semantic similarity between child and hints.
        Returns max similarity to any hint.
        """
        import numpy as np
        
        if child.label_embedding is None:
            return 0.1
        
        child_embed = np.array(child.label_embedding)
        sims = [np.dot(child_embed, h_embed) for h_embed in hint_embeds]
        return max(sims) if sims else 0.1
    
    def _find_hint_matches(
        self,
        child: Taxonomy,
        hints: List[str],
        hint_embeds: List,
        j: int,
        window: int
    ) -> List[Tuple[int, float]]:
        """
        Find which hints (if any) this child matches within the window.
        
        Args:
            child: Taxonomy node to match
            hints: List of hint strings
            hint_embeds: List of hint embeddings
            j: Current hint index (hints consumed so far)
            window: How many hints ahead to check
            
        Returns:
            List of (hint_index, similarity_score) tuples for matching hints
        """
        import numpy as np
        
        if child.label_embedding is None:
            return []
        
        child_embed = np.array(child.label_embedding)
        matches = []
        
        # Debug info for creative_work at depth 0
        debug_creative_work = (child.label == "creative_work" and j == 0)
        
        # Check hints in window [j, j+window)
        for idx in range(j, min(j + window, len(hints))):
            hint_sim = np.dot(child_embed, hint_embeds[idx])
            
            # Also check lexical similarity
            child_tokens = set(child.label.lower().replace("_", " ").split())
            hint_tokens = set(hints[idx].lower().split())
            
            if child_tokens and hint_tokens:
                jaccard = len(child_tokens & hint_tokens) / len(child_tokens | hint_tokens)  # Fixed: was child_tokens | child_tokens
                # Combine semantic and lexical
                combined_sim = 0.8 * hint_sim + 0.2 * jaccard
            else:
                combined_sim = hint_sim
            
            # Threshold for considering it a match (lowered to 0.3 for more flexibility)
            if combined_sim > 0.3:
                matches.append((idx, combined_sim))
        
        # Sort by similarity (best first)
        matches.sort(key=lambda x: x[1], reverse=True)
        
        return matches
    
    def _llm_select_branches(
        self,
        children: List[Taxonomy],
        label: str,
        sentence: str,
        node_type: str,
        category: str,
        current_path: str,
        agent
    ) -> List[Taxonomy]:
        """
        Use LLM to intelligently select top 1-3 branches to explore with relevance scores.
        
        If one branch has very high relevance (>0.85) and others are low (<0.5), 
        only explore the high-scoring one.
        
        Returns: Filtered list of children (1-3 items)
        """
        # Format children for LLM with descriptions
        child_descriptions = []
        for child in children:
            desc = child.description if child.description else "Description unavailable"
            child_descriptions.append(f"{child.label} - {desc}")
        
        agent_input = {
            "node_label": label,
            "node_category": category,
            "node_sentence": sentence or "",
            "node_type": node_type,
            "current_path": current_path,
            "child_descriptions": child_descriptions
        }
        
        # Debug: Log what we're passing to the agent
        logger.info(f"🔍 DEBUG (_llm_select_branches) - Passing to branch_selector_agent:")
        logger.info(f"  node_label: {label}")
        logger.info(f"  child_descriptions type: {type(child_descriptions)}")
        logger.info(f"  child_descriptions: {child_descriptions}")
        logger.info(f"  agent_input keys: {agent_input.keys()}")
        
        try:
            self._release_db_before_llm()
            response = agent.action_handler(Message(agent_input=agent_input))
            result = getattr(response, "data", {}) or {}
            
            selected_branches = result.get("selected_branches", [])
            reasoning = result.get("reasoning", "")
            
            if not selected_branches:
                logger.warning("LLM branch selection returned no branches, using all")
                return children
            
            # Parse branches with scores
            branch_scores = []
            for branch in selected_branches:
                if isinstance(branch, dict):
                    lbl = branch.get("label", "")
                    rel = branch.get("relevance", 0.5)
                    branch_scores.append((lbl, rel))
                else:
                    # Fallback for old format
                    branch_scores.append((branch, 0.5))
            
            # Sort by relevance (descending)
            branch_scores.sort(key=lambda x: x[1], reverse=True)
            
            # Smart pruning: If top branch is very strong (>0.85) and others are weak (<0.5), only use top
            if len(branch_scores) > 1:
                top_score = branch_scores[0][1]
                second_score = branch_scores[1][1]
                
                if top_score > 0.85 and second_score < 0.5:
                    branch_scores = [branch_scores[0]]  # Only keep the clear winner
            # Display selected branches
            # Filter children to only selected ones
            # Extract just the label part (before " - ") from descriptions
            selected_labels = [lbl.split(" - ")[0] if " - " in lbl else lbl for lbl, rel in branch_scores]
            selected_children = [c for c in children if c.label in selected_labels]
            
            if not selected_children:
                logger.warning("No matching children found for LLM selections, using all")
                return children
            
            return selected_children
            
        except Exception as e:
            logger.error(f"LLM branch selection failed: {e}", exc_info=True)
            return children  # Fallback to all children
    
    def _get_path_string(self, path_ids: List[int], session) -> str:
        """Convert path of IDs to string like 'entity > person'."""
        labels = [self.manager.get_taxonomy_by_id(tid).label for tid in path_ids]
        return " > ".join(labels)
    
    def _prefilter_by_similarity(self, children: List[Taxonomy], query_embeds: List, k: int) -> List[Taxonomy]:
        """
        Pre-filter children by AVERAGE semantic similarity across all query concepts.
        
        This prevents good matches from being drowned out by dozens of low-scoring siblings.
        Multi-concept scoring rewards children that match MULTIPLE type hints.
        
        Returns: Top-K children by average embedding similarity
        """
        import numpy as np
        
        scored = []
        for child in children:
            if child.label_embedding is not None:
                child_embed = np.array(child.label_embedding)
                # Compute similarity to each query concept, then average
                sims = [np.dot(q_embed, child_embed) for q_embed in query_embeds]
                avg_sim = np.mean(sims)
                scored.append((child, avg_sim))
            else:
                # No embedding - give it a small score so it's not completely excluded
                scored.append((child, 0.1))
        
        # Sort by average similarity and take top-K
        scored.sort(key=lambda x: x[1], reverse=True)
        top_k = [child for child, sim in scored[:k]]
        
        return top_k
    
    def _get_usage_prior(self, child_id: int, parent_id: int, session) -> float:
        """
        Get log P(child | parent) from NodeTaxonomyLink counts.
        
        Starts with uniform Dirichlet smoothing, learns from usage.
        """
        from app.assistant.kg_core.taxonomy.models import NodeTaxonomyLink
        
        # Count how many nodes have been classified to this child
        child_count = session.query(NodeTaxonomyLink).filter(
            NodeTaxonomyLink.taxonomy_id == child_id
        ).count()
        
        # Count total classifications under this parent (all siblings)
        siblings = session.query(Taxonomy).filter(
            Taxonomy.parent_id == parent_id
        ).all()
        
        total_count = 0
        for sibling in siblings:
            sib_count = session.query(NodeTaxonomyLink).filter(
                NodeTaxonomyLink.taxonomy_id == sibling.id
            ).count()
            total_count += sib_count
        
        # Dirichlet smoothing: log P = log((count + β) / (total + β*C))
        num_siblings = len(siblings)
        log_prob = math.log((child_count + BETA_SMOOTHING) / (total_count + BETA_SMOOTHING * num_siblings))
        
        # Clip to prevent overly negative priors for unused types
        # log(1/50) ≈ -3.9, which is too punishing. Clip to -1.0 min.
        return max(log_prob, -1.0)
    
    def _validate_scored_paths(self, paths: List[Dict], query_embeds: List, session) -> List[Dict]:
        """
        Simple threshold-based validation for Viterbi scored paths.
        
        For Viterbi, scores can be negative if many gaps. We validate based on:
        1. At least some hints were consumed
        2. Path depth is reasonable (>= 2)
        """
        validated = []
        
        for p in paths:
            # Must have matched at least 1 hint OR have reasonable depth
            hints_matched = p.get("hints_matched", 0)
            depth = p.get("depth", 0)
            final_score = p.get("final_score", p.get("score", -999))
            
            # Accept if: matched hints OR (reasonable depth AND decent score)
            if hints_matched > 0 or (depth >= 2 and final_score > 0.5):
                validated.append(p)
        return validated if validated else paths[:3]  # Fallback: return top 3 if none passed
    
    def _llm_verify_path(
        self,
        node_label: str,
        node_sentence: str,
        node_type: str,
        candidate_paths: List[Dict],
        agent,
        session
    ) -> Tuple[Optional[int], float]:
        """
        Final LLM verification of beam search candidates.
        
        Presents top-3 paths to LLM and asks it to pick the best semantic match,
        or reject all if none are appropriate.
        
        Returns: (taxonomy_id, confidence) or (None, 0.0) if rejected
        """
        if not candidate_paths:
            return (None, 0.0)
        
        # Format candidates for LLM
        formatted_candidates = []
        for i, path_data in enumerate(candidate_paths, 1):
            path_ids = path_data["path"]
            path_labels = [self.manager.get_taxonomy_by_id(tid).label for tid in path_ids]
            formatted_candidates.append({
                "rank": i,
                "path": " > ".join(path_labels),
                "leaf": path_labels[-1],
                "score": path_data["avg_score"],
                "depth": path_data["depth"],
                "taxonomy_id": path_ids[-1]
            })
        
        # Build agent input
        agent_input = {
            "node_label": node_label,
            "node_sentence": node_sentence or "",
            "node_type": node_type,
            "candidates": formatted_candidates
        }
        
        try:
            self._release_db_before_llm()
            response = agent.action_handler(Message(agent_input=agent_input))
            result = getattr(response, "data", {}) or {}
            
            chosen_rank = result.get("chosen_rank", 0)  # 1-based, 0 = reject all
            confidence = result.get("confidence", 0.5)
            reasoning = result.get("reasoning", "")
            if chosen_rank > 0 and chosen_rank <= len(formatted_candidates):
                chosen = formatted_candidates[chosen_rank - 1]
                return (chosen["taxonomy_id"], confidence)
            else:
                logger.info(f"LLM rejected all candidates for '{node_label}'")
                return (None, 0.0)
                
        except Exception as e:
            logger.error(f"LLM verification failed: {e}")
            return (None, 0.0)
    
    def _find_path_in_taxonomy(
        self,
        path: List[str],
        session
    ) -> Tuple[Optional[int], int]:
        """
        Search for a taxonomy path in the database.
        
        Args:
            path: List of taxonomy labels (e.g., ["entity", "person", "assistant"])
            session: Database session
            
        Returns:
            Tuple of (taxonomy_id, match_depth):
            - taxonomy_id: ID of the deepest matching node (or None if no match)
            - match_depth: How many levels matched (0 if no match)
        """
        if not path:
            return (None, 0)
        
        # Start with root
        current_node = session.query(Taxonomy).filter(
            Taxonomy.label == path[0],
            Taxonomy.parent_id.is_(None)
        ).first()
        
        if not current_node:
            logger.warning(f"Root '{path[0]}' not found in taxonomy")
            return (None, 0)
        
        matched_depth = 1
        
        # Walk down the path
        for i in range(1, len(path)):
            label = path[i]
            
            # Look for this label as a child of current_node
            child = session.query(Taxonomy).filter(
                Taxonomy.label == label,
                Taxonomy.parent_id == current_node.id
            ).first()
            
            if not child:
                # Path ends here - return deepest match
                logger.debug(f"Path stops at depth {matched_depth}: '{label}' not found under '{current_node.label}'")
                return (current_node.id, matched_depth)
            
            current_node = child
            matched_depth += 1
        
        # Reached end of path - complete match
        return (current_node.id, matched_depth)
    
    def _llm_disambiguate(
        self,
        node_label: str,
        node_sentence: str,
        candidate_paths: List[Dict],
        tie_breaker_agent
    ) -> Optional[Dict]:
        """
        Use LLM to select the best taxonomy path from candidates.
        
        Returns:
            Best candidate dict or None if LLM fails
        """
        if not candidate_paths:
            return None
        
        # Format candidates for LLM
        candidates_str = []
        for i, cand in enumerate(candidate_paths, 1):
            path_str = " > ".join(cand["path_labels"])
            score = cand["normalized_score"]
            depth = cand["depth"]
            reason = cand["stopped_reason"]
            
            candidates_str.append(
                f"{i}. {path_str}\n"
                f"   Score: {score:.3f}, Depth: {depth}, Stopped: {reason}"
            )
        
        agent_input = {
            "node_label": node_label,
            "node_sentence": node_sentence,
            "candidate_paths": "\n\n".join(candidates_str),
            "task": (
                "Evaluate all candidate taxonomy paths and select the MOST semantically appropriate one.\n\n"
                "Consider:\n"
                "1. Semantic fit: Does the path accurately describe this node?\n"
                "2. Specificity: Is it specific enough without being wrong?\n"
                "3. Taxonomy design: Does it follow good hierarchy principles?\n"
                "4. Context: Cultural conventions, common sense\n\n"
                "Return the path number (1-5) and explain your reasoning."
            )
        }
        
        try:
            self._release_db_before_llm()
            response = tie_breaker_agent.action_handler(Message(agent_input=agent_input))
            result = getattr(response, "data", {}) or {}
            
            selected_index = result.get("selected_path", 1) - 1  # Convert to 0-indexed
            reasoning = result.get("reasoning", "")
            
            logger.debug(f"LLM tie-breaker selected path {selected_index + 1}: {reasoning}")
            
            if 0 <= selected_index < len(candidate_paths):
                return candidate_paths[selected_index]
            else:
                logger.warning(f"LLM selected invalid index {selected_index}, using top candidate")
                return candidate_paths[0]
        except Exception as e:
            logger.error(f"LLM tie-breaker failed: {e}")
            # Fallback to top candidate
            return candidate_paths[0] if candidate_paths else None
    
    def _extract_concepts(
        self,
        label: str,
        sentence: str,
        node_type: str,
        concept_extractor_agent
    ) -> List[str]:
        """
        Use the concept extractor agent to get searchable keywords.
        
        Returns:
            List of 1-5 concept keywords
        """
        agent_input = {
            "node_label": label,
            "node_sentence": sentence or "No context provided.",
            "node_type": node_type
        }
        
        try:
            self._release_db_before_llm()
            agent_response = concept_extractor_agent.action_handler(Message(agent_input=agent_input))
            agent_data = getattr(agent_response, "data", {}) or {}
            
            concepts = agent_data.get("concepts", [])
            reasoning = agent_data.get("reasoning", "")
            
            logger.debug(f"Concept extractor reasoning: {reasoning}")
            
            return concepts if concepts else []
        except Exception as e:
            logger.error(f"Error extracting concepts: {e}")
            return []
    
    def _search_by_concepts(self, concepts: List[str], node_type: str = None) -> List[Dict]:
        """
        Search taxonomy using multiple concept keywords.
        Restricts search to the appropriate root branch (entity/event/state/etc).
        Combines results, deduplicates, and sorts by best similarity.
        
        Args:
            concepts: List of concept keywords to search
            node_type: Node type (Entity/Event/State/Goal/Concept/Property) to restrict search
        
        Returns:
            List of candidates with id, label, description, similarity
        """
        all_candidates = {}  # Dict to deduplicate by ID
        
        # Map node_type to root taxonomy label
        root_type = self._map_node_type_to_root(node_type)
        
        for concept in concepts:
            results = self.manager.semantic_search_taxonomy(
                text=concept,
                k=5,  # Top 5 for each concept
                parent_label=root_type  # Restrict to this root branch
            )
            
            for candidate in results:
                cand_id = candidate["id"]
                # Keep best similarity if seen multiple times
                if cand_id not in all_candidates or candidate["similarity"] > all_candidates[cand_id]["similarity"]:
                    all_candidates[cand_id] = candidate
        
        # Convert back to list and sort by similarity
        candidates = list(all_candidates.values())
        candidates.sort(key=lambda x: x["similarity"], reverse=True)
        
        # Filter by minimum similarity and limit total
        candidates = [c for c in candidates if c["similarity"] >= MIN_SIMILARITY]
        candidates = candidates[:20]  # Max 20 candidates
        
        return candidates
    
    def _map_node_type_to_root(self, node_type: str) -> Optional[str]:
        """
        Map node type to root taxonomy label.
        
        Entity → entity
        Event → event
        State → state
        Goal → goal
        Concept → concept
        Property → property
        
        Returns lowercase root label or None if unknown.
        """
        if not node_type:
            return None
        
        # Normalize to lowercase
        node_type_lower = node_type.lower()
        
        # Valid root types
        valid_roots = {"entity", "event", "state", "goal", "concept", "property"}
        
        if node_type_lower in valid_roots:
            return node_type_lower
        
        # Log warning if unknown type
        logger.warning(f"Unknown node_type '{node_type}', will search all taxonomy")
        return None
    
    def _create_placeholder(
        self,
        label: str,
        sentence: str,
        session
    ) -> Tuple[int, float, str]:
        """
        Create a placeholder taxonomy entry for later refinement.
        
        Returns:
            Tuple of (taxonomy_id, confidence=0.5, source="placeholder")
        """
        # Normalize label for taxonomy (lowercase, underscores)
        normalized_label = self._normalize_label_for_taxonomy(label)
        
        # Check if placeholder already exists
        existing = self.manager.get_taxonomy_by_label(normalized_label)
        if existing:
            logger.info(f"Placeholder already exists for '{normalized_label}' (id={existing.id})")
            return (existing.id, 0.5, "placeholder")
        
        # Create new placeholder
        tax = self.manager.create_taxonomy_placeholder(
            label=normalized_label,
            description=f"Auto-created from: {sentence[:100]}..." if sentence else None,
            parent_id=None  # To be determined by researcher agent
        )
        
        logger.info(f"📝 Created placeholder taxonomy '{normalized_label}' (id={tax.id})")
        
        return (tax.id, 0.5, "placeholder")
    
    def _create_probationary(
        self,
        label: str,
        original_label: str,
        sentence: Optional[str],
        parent_id: Optional[int],
        confidence: float,
        session
    ) -> Tuple[int, float, str]:
        """
        Create a probationary taxonomy entry based on agent suggestion.
        
        These are agent-proposed types that had low confidence in existing matches,
        so the agent suggested a new subcategory. They're stored with a parent hint
        for later curation.
        
        Args:
            label: Agent's suggested type (e.g., "Household")
            original_label: Original node label (e.g., "User's household")
            sentence: Context sentence
            parent_id: Poor match ID to use as parent hint
            confidence: Agent's confidence
            session: DB session
            
        Returns:
            Tuple of (taxonomy_id, confidence, source="probationary")
        """
        # Normalize suggestion (already done by _normalize_taxonomy_label)
        normalized_label = self._normalize_taxonomy_label(label)
        
        # Check if probationary entry already exists
        existing = self.manager.get_taxonomy_by_label(normalized_label)
        if existing:
            logger.info(f"Probationary entry already exists for '{normalized_label}' (id={existing.id})")
            return (existing.id, confidence, "probationary")
        
        # Create new probationary entry with parent hint
        tax = self.manager.create_taxonomy_placeholder(
            label=normalized_label,
            description=f"Agent-suggested type for '{original_label}'. Context: {sentence[:80]}..." if sentence else f"Agent-suggested type for '{original_label}'",
            parent_id=parent_id  # Use poor match as parent hint for researcher
        )
        
        logger.info(f"🔬 Created PROBATIONARY taxonomy '{normalized_label}' (id={tax.id}, parent_hint={parent_id})")
        
        return (tax.id, confidence, "probationary")
    
    def _normalize_label_for_taxonomy(self, label: str) -> str:
        """
        Normalize a label for use as a taxonomy type.
        
        Examples:
            "Sam's father" → "sams_father"
            "Ownership" → "ownership"
            "Birthday Party" → "birthday_party"
        """
        return self._normalize_taxonomy_label(label)
    
    def _normalize_taxonomy_label(self, label: str) -> str:
        """
        Normalize any label to lowercase snake_case for taxonomy storage.
        
        This is used for both placeholder creation and suggestion normalization.
        
        Examples:
            "Birthday Party" → "birthday_party"
            "Software Developer" → "software_developer"
            "Dog" → "dog"
            "Sam's father" → "sams_father"
        """
        # Lowercase
        normalized = label.lower()
        
        # Replace spaces and special chars with underscores
        import re
        normalized = re.sub(r"['\"]", "", normalized)  # Remove quotes
        normalized = re.sub(r"[^a-z0-9]+", "_", normalized)  # Replace non-alphanumeric with _
        normalized = normalized.strip("_")  # Remove leading/trailing underscores
        
        return normalized
    
    def _format_candidates_for_agent(self, candidates: List[Dict]) -> str:
        """
        Format candidates as a human-readable list for the agent.
        Includes hierarchy path to show depth/specificity.
        
        NOTE: Similarity scores are HIDDEN from the agent to prevent anchoring bias.
        The agent should evaluate semantic fit, not rely on numeric similarity.
        
        Returns:
            Formatted string like:
            1. father (id: 42) -> entity > person > parent > father
            2. parent (id: 38) -> entity > person > parent
            ...
        """
        lines = []
        for i, cand in enumerate(candidates, 1):
            # Get hierarchy path
            path = self.manager.get_taxonomy_path(cand['id'])
            path_str = " > ".join(path) if path else cand['label']
            
            # Format: number. label (id: X) -> hierarchy
            # NOTE: similarity score removed to prevent anchoring bias
            desc = f" - {cand.get('description', '')}" if cand.get('description') else ""
            lines.append(
                f"{i}. {cand['label']} (id: {cand['id']}) -> {path_str}{desc}"
            )
        
        return "\n".join(lines)
    
    def link_node_to_taxonomy(
        self,
        node_id: str,
        taxonomy_id: int,
        confidence: float,
        source: str,
        session
    ):
        """
        Create a link between a node and its taxonomy classification.
        
        Args:
            node_id: UUID of the node
            taxonomy_id: ID of the taxonomy type
            confidence: Confidence score
            source: "matched" or "placeholder"
            session: Database session
        """
        if not self.manager:
            self.manager = TaxonomyManager(session)
        
        link = self.manager.link_node_to_taxonomy(
            node_id=node_id,
            taxonomy_id=taxonomy_id,
            confidence=confidence,
            source=source
        )
        
        return link
    
    def _llm_guided_descent(
        self,
        label: str,
        sentence: str,
        node_type: str,
        category: str,
        root_id: int,
        branch_selector_agent,
        session,
        max_depth: int = 7,
        return_top_paths: bool = False  # NEW: Return top 3 paths
    ) -> Optional[Tuple[Optional[int], float, str]]:
        """
        LLM-guided taxonomy descent - at each level, LLM picks top 2 branches.
        Returns (taxonomy_id, confidence, source) or None if failed.
        """
        try:
            # Start at root
            all_paths = []
            
            def explore(parent_id, current_path, depth, cumulative_relevance):
                if depth > max_depth:
                    return
                
                # Get children of current node
                children = session.query(Taxonomy).filter(
                    Taxonomy.parent_id == parent_id
                ).all()
                
                if not children:
                    # Leaf node - add to results
                    all_paths.append({
                        'path': current_path,
                        'cumulative_relevance': cumulative_relevance
                    })
                    return
                
                # Ask LLM to select top 2 children
                path_str = " > ".join([
                    session.query(Taxonomy).filter(Taxonomy.id == pid).first().label
                    for pid in current_path
                ])
                
                # Build child descriptions with labels and descriptions
                child_descriptions = []
                for child in children:
                    desc = child.description if child.description else "Description unavailable"
                    child_descriptions.append(f"{child.label} - {desc}")
                
                agent_input = {
                    'node_label': label,
                    'node_category': category,
                    'node_sentence': sentence or "",
                    'node_type': node_type,
                    'current_path': path_str,
                    'child_descriptions': child_descriptions,
                    'mode': 'select_top_2'
                }
                
                # Debug: Log what we're passing to the agent
                logger.info(f"🔍 DEBUG - Passing to branch_selector_agent:")
                logger.info(f"  node_label: {label}")
                logger.info(f"  child_descriptions type: {type(child_descriptions)}")
                logger.info(f"  child_descriptions: {child_descriptions}")
                logger.info(f"  agent_input keys: {agent_input.keys()}")
                
                # Call LLM
                self._release_db_before_llm(session)
                response = branch_selector_agent.action_handler(Message(agent_input=agent_input))
                result = getattr(response, "data", {}) or {}
                
                selected = result.get('selected_branches', [])
                confidence = result.get('confidence', 0.0)
                
                # If LLM rejected all children (returned empty list), stop here and use current path
                if not selected:
                    all_paths.append({
                        'path': current_path,
                        'cumulative_relevance': cumulative_relevance
                    })
                    return
                
                # Map selected labels to children
                label_to_child = {child.label: child for child in children}
                
                for branch_info in selected[:2]:
                    branch_label = branch_info['label']
                    # Strip description if agent returned full format
                    if " - " in branch_label:
                        branch_label = branch_label.split(" - ")[0]
                    
                    relevance = branch_info['relevance']
                    
                    if branch_label in label_to_child:
                        child = label_to_child[branch_label]
                        new_relevance = cumulative_relevance * relevance
                        explore(child.id, current_path + [child.id], depth + 1, new_relevance)
            
            # Start exploration from root
            explore(root_id, [root_id], 0, 1.0)
            
            if not all_paths:
                return None
            
            # Score all paths: 70% LLM relevance + 30% semantic similarity
            scored_paths = []
            for path_info in all_paths:
                path_ids = path_info['path']
                cumulative_relevance = path_info['cumulative_relevance']
                
                # Get leaf node
                leaf_id = path_ids[-1]
                leaf = session.query(Taxonomy).filter(Taxonomy.id == leaf_id).first()
                
                # Compute semantic similarity
                label_embed = self.kg_utils.create_embedding(label)
                leaf_embed = self.kg_utils.create_embedding(leaf.label)
                leaf_sim = float(self.kg_utils.cosine_similarity(label_embed, leaf_embed))
                
                # Combined score
                final_score = 0.7 * cumulative_relevance + 0.3 * leaf_sim
                
                scored_paths.append({
                    'taxonomy_id': leaf_id,
                    'score': final_score,
                    'llm_relevance': cumulative_relevance,
                    'leaf_sim': leaf_sim,
                    'path_ids': path_ids  # NEW: Store path IDs with score
                })
            
            # Sort by score
            scored_paths.sort(key=lambda x: x['score'], reverse=True)
            
            if scored_paths:
                best = scored_paths[0]
                
                # NEW: If return_top_paths is True, return dict with top 3 paths
                if return_top_paths:
                    top_3 = scored_paths[:3]
                    return {
                        'best_taxonomy_id': best['taxonomy_id'],
                        'best_score': best['score'],
                        'source': "llm_guided",
                        'top_paths': [
                            {
                                'taxonomy_id': p['taxonomy_id'],
                                'score': p['score'],
                                'path_ids': p['path_ids']  # Full path IDs (now stored in scored_paths)
                            }
                            for p in top_3
                        ]
                    }
                
                return (best['taxonomy_id'], best['score'], "llm_guided")
            
            return None
            
        except Exception as e:
            logger.error(f"LLM-guided descent error: {e}")
            return None
    
    def _critic_compare_methods(
        self,
        node_label: str,
        node_sentence: str,
        node_type: str,
        llm_option: Dict,
        beam_option: Dict,
        agent,
        session
    ) -> Optional[Tuple[int, float, str]]:
        """
        Let the critic choose between LLM-guided and beam search results.
        
        Returns (taxonomy_id, confidence, source) where source indicates which method won.
        """
        try:
            # Build agent input
            agent_input = {
                "node_label": node_label,
                "node_sentence": node_sentence,
                "node_type": node_type,
                "llm_guided_path": llm_option['path'],
                "llm_guided_confidence": llm_option['confidence'],
                "beam_search_path": beam_option['path'],
                "beam_search_confidence": beam_option['confidence'],
                "mode": "compare_methods"
            }
            
            # Call critic
            self._release_db_before_llm(session)
            response = agent.action_handler(Message(agent_input=agent_input))
            result = getattr(response, "data", {}) or {}
            
            action = result.get('action', '')
            validated_path = result.get('validated_path', '')
            reasoning = result.get('reasoning', '')
            confidence = result.get('confidence', 0.5)
            # Parse the critic's decision
            if action == "ACCEPT_LLM":
                return (llm_option['taxonomy_id'], confidence, "llm_guided")
            elif action == "ACCEPT_BEAM":
                return (beam_option['taxonomy_id'], confidence, "beam_search")
            elif action == "CORRECT_PATH" and validated_path:
                # Find or create the corrected path
                tax_id = self.manager.find_taxonomy_by_path(validated_path)
                if tax_id:
                    return (tax_id, confidence, "critic_corrected")
            
            # Fallback: pick highest confidence
            if llm_option['confidence'] >= beam_option['confidence']:
                return (llm_option['taxonomy_id'], llm_option['confidence'], "llm_guided")
            else:
                return (beam_option['taxonomy_id'], beam_option['confidence'], "beam_search")
            
        except Exception as e:
            logger.error(f"Critic comparison error: {e}")
            # Fallback to highest confidence
            if llm_option['confidence'] >= beam_option['confidence']:
                return (llm_option['taxonomy_id'], llm_option['confidence'], "llm_guided")
            else:
                return (beam_option['taxonomy_id'], beam_option['confidence'], "beam_search")

