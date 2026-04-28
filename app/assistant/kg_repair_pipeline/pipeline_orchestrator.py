"""
KG Repair Pipeline Orchestrator

This orchestrator manages the sequential execution of the KG repair pipeline:
1. Analyzer - Identifies problematic nodes
2. Critic - Validates and filters findings
3. Questioner - Asks user for missing data
4. Implementer - Applies fixes to the knowledge graph
"""

import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from app.assistant.utils.logging_config import get_logger

from app.assistant.kg_repair_pipeline.data_models.pipeline_state import PipelineState, PipelineStage
from app.assistant.kg_repair_pipeline.data_models.problematic_node import ProblematicNode
from app.assistant.kg_repair_pipeline.data_models.node_processing_tracking import NodeProcessingStatus
from app.assistant.kg_repair_pipeline.stages.analyzer import KGAnalyzer
from app.assistant.kg_repair_pipeline.stages.critic import KGCritic
from app.assistant.kg_repair_pipeline.stages.questioner import KGQuestioner
from app.assistant.kg_repair_pipeline.stages.implementer import KGImplementer
from app.assistant.kg_repair_pipeline.utils.node_processing_manager import NodeProcessingManager

logger = get_logger(__name__)

class KGPipelineOrchestrator:
    """
    Orchestrates the KG repair pipeline execution.
    """
    
    def __init__(self, enable_questioning=False, enable_implementation=False):
        """
        Initialize the pipeline orchestrator.
        
        Args:
            enable_questioning: If True, ask user for input. If False, save suggestions to DB.
            enable_implementation: If True, execute repairs immediately. If False, save for later.
        """
        self.pipeline_id = str(uuid.uuid4())
        self.state = PipelineState(
            pipeline_id=self.pipeline_id,
            started_at=datetime.now(timezone.utc)
        )
        
        self.enable_questioning = enable_questioning
        self.enable_implementation = enable_implementation
        
        # Initialize pipeline stages
        self.analyzer = KGAnalyzer()
        self.critic = KGCritic()
        self.questioner = KGQuestioner() if enable_questioning else None
        self.implementer = KGImplementer() if enable_implementation else None
        self.processing_manager = NodeProcessingManager()
        
    def run_pipeline(self, kg_info: Optional[Dict] = None, max_nodes: int = 10) -> PipelineState:
        """
        Execute the complete KG repair pipeline.
        
        Args:
            kg_info: Optional knowledge graph information to analyze
            max_nodes: Maximum number of nodes to process in this run
            
        Returns:
            PipelineState: Final state of the pipeline execution
        """
        try:
            logger.info(f"🚀 Starting KG Repair Pipeline {self.pipeline_id}")
            self.state.input_kg_info = kg_info
            self.state.max_nodes_per_batch = max_nodes
            self.state.last_activity_at = datetime.now(timezone.utc)
            
            # Stage 1: Node Selection
            nodes_to_process = self._select_nodes_to_process()
            if not nodes_to_process:
                logger.info("✅ No nodes to process - pipeline complete")
                self.state.current_stage = PipelineStage.COMPLETED
                return self.state
                
            # Stage 2: Analysis (one node at a time)
            problematic_nodes = self._run_analysis_stage(nodes_to_process)
            if not problematic_nodes:
                logger.info("✅ No problematic nodes found - pipeline complete")
                self.state.current_stage = PipelineStage.COMPLETED
                return self.state
                
            # Stage 3: Critique (validate and get suggestions)
            validated_nodes = self._run_critique_stage(problematic_nodes)
            if not validated_nodes:
                logger.info("✅ No nodes validated - pipeline complete")
                self.state.current_stage = PipelineStage.COMPLETED
                return self.state
                
            # Stage 4: Save suggestions to database (or optionally question user)
            if self.enable_questioning:
                # Original questioning flow (interactive mode)
                questioning_result = self._run_questioning_stage(validated_nodes)
                
                # Handle pause request
                if questioning_result.get('should_pause'):
                    logger.warning("⏸️ Pipeline paused by user request")
                    self.state.current_stage = PipelineStage.PAUSED
                    return self.state
                
                # Get user responses with instructions
                user_responses = questioning_result.get('user_responses', [])
                postponed_nodes = questioning_result.get('postponed_nodes', [])
                
                # Log postponed nodes (future feature: reschedule these)
                if postponed_nodes:
                    logger.info(f"⏰ {len(postponed_nodes)} nodes postponed for later processing")
                    for item in postponed_nodes:
                        logger.info(f"  - {item['node'].id}: postponed until {item['postpone_until']}")
                
                if not user_responses:
                    logger.info("✅ No actionable instructions received - pipeline complete")
                    self.state.current_stage = PipelineStage.COMPLETED
                    return self.state
                    
                # Stage 5: Implementation (if enabled)
                if self.enable_implementation:
                    self._run_implementation_stage(user_responses)
                else:
                    logger.info("⏭️ Implementation disabled - suggestions saved to database")
            else:
                # Non-interactive mode: Save all suggestions to database for bulk review
                logger.info(f"💾 Saving {len(validated_nodes)} repair suggestions to database for bulk review")
                self._save_suggestions_to_database(validated_nodes)
                
            # Pipeline completed successfully
            self.state.current_stage = PipelineStage.COMPLETED
            self.state.completed_at = datetime.now(timezone.utc)
            logger.info(f"✅ KG Repair Pipeline {self.pipeline_id} completed successfully")
            
        except Exception as e:
            logger.error(f"❌ KG Repair Pipeline {self.pipeline_id} failed: {e}")
            self.state.current_stage = PipelineStage.FAILED
            self.state.errors.append(str(e))
            
        return self.state
    
    def _select_nodes_to_process(self) -> List[Dict[str, Any]]:
        """
        Select nodes to process in this pipeline run.
        Gets nodes directly from the KG nodes table, excluding already processed nodes.
        
        Returns:
            List of node information dictionaries
        """
        try:
            logger.info("🎯 Selecting nodes to process from KG...")
            
            from app.assistant.kg_repair_pipeline.utils.kg_operations import KGOperations
            from app.assistant.kg.db.knowledge_graph_db import Node
            from app.models.base import get_session
            from sqlalchemy import desc, asc, func
            
            session = get_session()
            kg_ops = KGOperations()
            
            # Get list of already processed node IDs (optimized query)
            logger.info("🔍 Checking for already processed nodes...")
            processed_node_ids = session.query(NodeProcessingStatus.node_id).distinct().all()
            processed_ids = [row[0] for row in processed_node_ids]
            logger.info(f"📊 Found {len(processed_ids)} already processed nodes")
            
            # Performance optimization: if we have many processed nodes, use a more efficient approach
            if len(processed_ids) > 1000:
                logger.info("⚡ Large dataset detected, using optimized query strategy")
                # For large datasets, we'll use a different approach to avoid memory issues
                # This could be further optimized with pagination or subqueries
            
            # Query nodes directly from the KG, excluding already processed ones
            # Priority: 1) New nodes (recently created), 2) Nodes with missing data, 3) Random nodes
            if processed_ids:
                # Exclude already processed nodes
                logger.info(f"🚫 Excluding {len(processed_ids)} already processed nodes from selection")
                nodes_query = session.query(Node).filter(
                    Node.id.isnot(None),  # Basic filter to get all nodes
                    Node.id.notin_(processed_ids)  # Exclude already processed nodes
                ).order_by(
                    desc(Node.created_at)  # Start with newest nodes
                ).limit(self.state.max_nodes_per_batch)
            else:
                # No processed nodes yet, get newest nodes
                logger.info("🆕 No processed nodes found, selecting newest nodes")
                nodes_query = session.query(Node).filter(
                    Node.id.isnot(None)  # Basic filter to get all nodes
                ).order_by(
                    desc(Node.created_at)  # Start with newest nodes
                ).limit(self.state.max_nodes_per_batch)
            
            kg_nodes = nodes_query.all()
            logger.info(f"🎯 Selected {len(kg_nodes)} new nodes to process")
            
            if not kg_nodes:
                # Check if we have any nodes at all
                total_nodes = session.query(Node).count()
                logger.info(f"📊 Total nodes in KG: {total_nodes}")
                
                if total_nodes == 0:
                    logger.info("📝 No nodes found in KG")
                    return []
                elif len(processed_ids) >= total_nodes:
                    logger.info("🎉 All nodes in KG have been processed! Pipeline is complete.")
                    return []
                else:
                    # Fallback: try random sampling if no new nodes found
                    logger.info("🔄 No new nodes found, trying random sampling...")
                    nodes_query = session.query(Node).filter(
                        Node.id.isnot(None),
                        Node.id.notin_(processed_ids)
                    ).order_by(
                        func.random()  # Random order as fallback
                    ).limit(self.state.max_nodes_per_batch)
                    
                    kg_nodes = nodes_query.all()
                    logger.info(f"🎲 Random sampling found {len(kg_nodes)} nodes")
                    
                    if not kg_nodes:
                        logger.info("📝 No unprocessed nodes available")
                        return []
            
            # Convert to node info dictionaries with full neighborhood data
            nodes_to_process = []
            for kg_node in kg_nodes:
                try:
                    # Get full node info with neighborhood data
                    node_info = kg_ops.get_node_info(str(kg_node.id))

                    if node_info:
                        nodes_to_process.append(node_info)
                        logger.info(f"  ✅ Added node {kg_node.label} ({kg_node.node_type}) for processing")
                    else:
                        logger.warning(f"  ⚠️ Could not get info for node {kg_node.id}")

                except Exception as e:
                    logger.error(f"  ❌ Error getting info for node {kg_node.id}: {e}")
                    continue

            logger.info(f"📋 Selected {len(nodes_to_process)} nodes from KG for processing")
            return nodes_to_process
            
        except Exception as e:
            logger.error(f"❌ Node selection failed: {e}")
            return []
    
    def _run_analysis_stage(self, nodes_to_process: List[Dict[str, Any]]) -> List[ProblematicNode]:
        """Run the analysis stage to identify problematic nodes."""
        try:
            logger.info(f"🔍 Running Analysis Stage on {len(nodes_to_process)} nodes...")
            self.state.current_stage = PipelineStage.ANALYZING
            
            problematic_nodes = []
            
            # Process each node individually
            for node_info in nodes_to_process:
                try:
                    logger.info(f"🔍 Analyzing node: {node_info.get('label', 'Unknown')}")
                    
                    # Note: We already filtered out processed nodes in _select_nodes_to_process()
                    # So we can proceed directly to analysis
                    
                    # Analyze single node with its neighborhood
                    analysis_result = self.analyzer.analyze_single_node(node_info)
                    
                    if analysis_result and analysis_result.get('is_problematic'):
                        # Create processing status for this problematic node
                        processing_status = self.processing_manager.create_node_status(
                           node_id=node_info['id'],
                           problem_description=analysis_result.get('problem_description', 'Unknown problem'),
                           problem_category='data_quality'  # Default category
                        )
                        
                        problematic_node = ProblematicNode(
                            id=node_info['id'],
                            label=node_info['label'],
                            type=node_info['node_type'],
                            category=node_info.get('category', ''),
                            description=node_info['description'],
                            start_date = node_info['start_date'],
                            end_date = node_info['end_date'],
                            start_date_confidence = node_info['start_date_confidence'],
                            end_date_confidence=node_info['end_date_confidence'],
                            valid_during=node_info['valid_during'],
                            node_aliases = node_info['aliases'],
                            full_node_info=node_info,  # Store full context for critic
                            problem_description=analysis_result['problem_description'],
                        )
                        problematic_nodes.append(problematic_node)
                        logger.info(f"  ⚠️ Problematic: {analysis_result['problem_description']}")
                    else:
                        # COMMENTED OUT FOR TESTING - allows repeated testing on same nodes
                        # Mark as not problematic to avoid re-processing
                        # self.processing_manager.update_node_status(
                        #     node_id=node_info['id'],
                        #     status='invalid',
                        #     should_skip_future=True,
                        #     notes='Analyzed and determined not problematic'
                        # )
                        logger.info(f"  ✅ No problems found")
                        
                except Exception as e:
                    logger.error(f"❌ Failed to analyze node {node_info.get('id')}: {e}")
                    continue
                    
            self.state.problematic_nodes = problematic_nodes
            self.state.update_progress()
            
            logger.info(f"✅ Analysis complete - found {len(problematic_nodes)} problematic nodes")
            return problematic_nodes
            
        except Exception as e:
            logger.error(f"❌ Analysis stage failed: {e}")
            self.state.errors.append(f"Analysis failed: {e}")
            return []
    
    def _run_critique_stage(self, problematic_nodes: List[ProblematicNode]) -> List[ProblematicNode]:
        """Run the critique stage to validate findings and get suggestions."""
        try:
            logger.info(f"🎯 Running Critique Stage on {len(problematic_nodes)} nodes...")
            self.state.current_stage = PipelineStage.CRITIQUING
            
            validated_nodes = []
            
            # Critique each problematic node
            for node in problematic_nodes:
                try:
                    logger.info(f"🎯 Critiquing node: {node.label}")
                    
                    # Get critique with actionable suggestions
                    critique_result = self.critic.critique_node_with_suggestions(node)
                    
                    # Log detailed critique results
                    logger.info(f"  📊 Critique: is_valid={critique_result.get('is_valid')}, "
                              f"is_problematic={critique_result.get('is_problematic')}, "
                              f"analyzer_correct={critique_result.get('analyzer_is_correct')}")
                    
                    if critique_result['is_valid']:
                        node.status = "validated"
                        node.resolution_notes = critique_result.get('suggestions', '')
                        validated_nodes.append(node)
                        logger.info(f"  ✅ Validated via is_valid: {critique_result.get('suggestions', 'No specific suggestions')}")
                    elif critique_result.get('is_problematic'):  # Use .get() for safety
                        node.status = "validated"
                        node.resolution_notes = critique_result.get('suggestions', '')
                        validated_nodes.append(node)
                        logger.info(f"  ✅ Validated via is_problematic (analyzer was wrong but node IS broken): {critique_result.get('suggestions', 'No specific suggestions')}")
                    else:
                        node.status = "skipped"
                        node.resolution_notes = critique_result.get('reason', 'Not a valid problem')
                        logger.info(f"  ⏭️ Skipped: {critique_result.get('reason', 'Not a valid problem')}")
                        
                except Exception as e:
                    logger.error(f"❌ Failed to critique node {node.label}: {e}")
                    node.status = "error"
                    node.resolution_notes = f"Critique error: {e}"
                    continue
                    
            self.state.problematic_nodes = validated_nodes
            self.state.update_progress()
            
            logger.info(f"✅ Critique complete - {len(validated_nodes)} nodes validated")
            return validated_nodes
            
        except Exception as e:
            logger.error(f"❌ Critique stage failed: {e}")
            self.state.errors.append(f"Critique failed: {e}")
            return []
    
    def _run_questioning_stage(self, validated_nodes: List[ProblematicNode]) -> Dict[str, Any]:
        """
        Run the questioning stage to get user input and prepare instructions.
        
        Returns dict with:
        - user_responses: List of response dicts
        - should_pause: bool - if pipeline should stop
        - postponed_nodes: List of nodes to process later
        """
        try:
            logger.info(f"❓ Running Questioning Stage on {len(validated_nodes)} nodes...")
            self.state.current_stage = PipelineStage.QUESTIONING
            
            user_responses = []
            postponed_nodes = []
            should_pause_pipeline = False
            
            # Ask user about each validated node
            for node in validated_nodes:
                try:
                    logger.info(f"❓ Questioning user about node: {node.id}")
                    
                    # Ask user and get structured response
                    questioner_result = self.questioner.ask_user_with_instructions(node)
                    
                    if questioner_result:
                        # Check if user wants to pause entire pipeline
                        if questioner_result.get('pause_entire_pipeline'):
                            logger.warning(f"⏸️ User requested to pause entire pipeline")
                            should_pause_pipeline = True
                            break  # Stop processing more nodes
                        
                        # Check if user wants to skip this node
                        if questioner_result.get('skip_this_node'):
                            logger.info(f"  ⏭️ User requested to skip node {node.id}")
                            node.status = "skipped"
                            node.resolution_notes = "User requested to skip"
                            self.state.nodes_skipped += 1
                            continue
                        
                        # Check if user wants to postpone this node
                        if questioner_result.get('postpone_until'):
                            postpone_time = questioner_result['postpone_until']
                            logger.info(f"  ⏰ User requested to postpone node {node.id} until: {postpone_time}")
                            postponed_nodes.append({
                                'node': node,
                                'postpone_until': postpone_time
                            })
                            node.status = "postponed"
                            node.resolution_notes = f"Postponed until {postpone_time}"
                            continue
                        
                        # User provided instructions
                        if questioner_result.get('instructions'):
                            instructions = questioner_result['instructions']
                            logger.info(f"  ✅ User provided instructions for node {node.id}")
                            logger.info(f"  📋 Instructions: {instructions[:100]}...")
                            
                            node.user_response = questioner_result['raw_response']
                            node.user_data = instructions
                            node.status = "questioned"
                            
                            user_responses.append(questioner_result)
                            
                        else:
                            logger.warning(f"  ⚠️ No instructions or action specified for node {node.id}")
                            node.status = "skipped"
                            node.resolution_notes = "No user instructions provided"
                            self.state.nodes_skipped += 1
                            
                    else:
                        logger.warning(f"  ⚠️ No response received for node {node.id}")
                        
                except Exception as e:
                    logger.error(f"❌ Failed to question node {node.id}: {e}")
                    node.status = "error"
                    self.state.errors.append(f"Questioning failed for {node.id}: {e}")
                    continue
                    
            self.state.nodes_questioned = len(user_responses)
            self.state.update_progress()
            
            logger.info(f"✅ Questioning complete - processed {len(user_responses)} nodes with instructions")
            if postponed_nodes:
                logger.info(f"  ⏰ {len(postponed_nodes)} nodes postponed for later")
            if should_pause_pipeline:
                logger.warning(f"  ⏸️ Pipeline pause requested by user")
            
            return {
                'user_responses': user_responses,
                'should_pause': should_pause_pipeline,
                'postponed_nodes': postponed_nodes
            }
            
        except Exception as e:
            logger.error(f"❌ Questioning stage failed: {e}")
            self.state.errors.append(f"Questioning failed: {e}")
            return []
    
    def _save_suggestions_to_database(self, validated_nodes: List[ProblematicNode]) -> bool:
        """
        Save repair suggestions to kg_reviews table for bulk review (non-interactive mode).
        
        Args:
            validated_nodes: List of validated problematic nodes with critic suggestions
            
        Returns:
            bool: True if successful
        """
        try:
            from app.assistant.kg_review.review_manager import KGReviewManager
            
            logger.info(f"💾 Saving {len(validated_nodes)} repair suggestions to kg_reviews...")
            review_manager = KGReviewManager()
            saved_count = 0
            
            for node in validated_nodes:
                try:
                    # Extract edge information with source sentences for context
                    edges_info = []
                    if node.full_node_info and 'edges' in node.full_node_info:
                        edges = node.full_node_info['edges']
                        for edge in edges[:5]:  # Limit to 5 edges for display
                            relationship = edge.get('relationship', 'connected')
                            target = edge.get('target_label', 'unknown')
                            sentence = edge.get('sentence', '')
                            
                            # Format: "relationship → target | Source: sentence"
                            edge_desc = {
                                'relationship': relationship,
                                'target': target,
                                'sentence': sentence
                            }
                            edges_info.append(edge_desc)
                    
                    # Create KG review with enhanced context
                    review = review_manager.create_review(
                        node_id=str(node.id),
                        problem_description=node.problem_description,
                        source='repair_pipeline',
                        finding_type='quality',  # Default, could be refined
                        node_label=node.label,
                        node_type=node.type,
                        node_category=node.category,
                        analyzer_suggestion=node.problem_description,  # From analyzer
                        critic_suggestion=node.resolution_notes,       # From critic
                        priority='medium',
                        edge_count=getattr(node, 'edge_count', None),
                        context_data={
                            'full_node_info': node.full_node_info,
                            'description': node.description,
                            'start_date': str(node.start_date) if node.start_date else None,
                            'end_date': str(node.end_date) if node.end_date else None,
                            'start_date_confidence': node.start_date_confidence,
                            'end_date_confidence': node.end_date_confidence,
                            'valid_during': node.valid_during,
                            'edges_summary': edges_info,
                            'status': node.full_node_info.get('status') if node.full_node_info else None
                        },
                        source_pipeline_id=self.pipeline_id
                    )
                    
                    saved_count += 1
                    logger.info(f"  💾 Saved review for node: {node.label} (review_id: {review.id})")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to save review for node {node.id}: {e}")
                    continue
            
            logger.info(f"✅ Saved {saved_count} repair suggestions to kg_reviews table")
            self.state.nodes_questioned = saved_count  # Track as "processed"
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to save suggestions: {e}")
            return False
    
    def _run_implementation_stage(self, user_responses: List[Dict[str, Any]]) -> bool:
        """Run the implementation stage to apply fixes using multi-tool operations."""
        try:
            logger.info(f"🔧 Running Implementation Stage on {len(user_responses)} responses...")
            self.state.current_stage = PipelineStage.IMPLEMENTING
            
            implemented_count = 0
            
            # Process each user response with its implementation instructions
            for response_dict in user_responses:
                try:
                    node_id = response_dict['node_id']
                    instructions = response_dict['instructions']
                    logger.info(f"🔧 Implementing fixes for node: {node_id}")
                    
                    # Get the corresponding problematic node
                    node = next((n for n in self.state.problematic_nodes if n.id == node_id), None)
                    if not node:
                        logger.warning(f"⚠️ No node found for response {node_id}")
                        continue
                        
                    # Create a user_response-like object for the implementer
                    user_response_obj = type('UserResponse', (), {
                        'node_id': node_id,
                        'raw_response': response_dict['raw_response'],
                        'provided_data': {'instructions': instructions}
                    })()
                        
                    # Apply multi-tool implementation
                    implementation_result = self.implementer.implement_with_multi_tools(node, user_response_obj)
                    
                    if implementation_result['success']:
                        node.status = "resolved"
                        node.resolution_notes = implementation_result.get('notes', 'Successfully implemented')
                        node.resolved_at = datetime.now(timezone.utc)
                        implemented_count += 1
                        logger.info(f"  ✅ Successfully implemented: {implementation_result.get('notes')}")
                    else:
                        node.status = "failed"
                        node.resolution_notes = implementation_result.get('error', 'Implementation failed')
                        logger.error(f"  ❌ Implementation failed: {implementation_result.get('error')}")
                        
                except Exception as e:
                    logger.error(f"❌ Failed to implement fixes for node {node_id}: {e}")
                    if node:
                        node.status = "error"
                        node.resolution_notes = f"Implementation error: {e}"
                    
            self.state.update_progress()
            logger.info(f"✅ Implementation complete - fixed {implemented_count} nodes")
            return True
            
        except Exception as e:
            logger.error(f"❌ Implementation stage failed: {e}")
            self.state.errors.append(f"Implementation failed: {e}")
            return False
    
    def get_pipeline_status(self) -> Dict[str, Any]:
        """Get current pipeline status for monitoring."""
        return {
            'pipeline_id': self.pipeline_id,
            'current_stage': self.state.current_stage,
            'progress': {
                'total_nodes': self.state.total_nodes_identified,
                'validated': self.state.nodes_validated,
                'questioned': self.state.nodes_questioned,
                'resolved': self.state.nodes_resolved,
                'skipped': self.state.nodes_skipped
            },
            'errors': self.state.errors,
            'last_activity': self.state.last_activity_at
        }
    
    @staticmethod
    def get_pending_suggestions(limit: int = 100) -> List[Dict[str, Any]]:
        """
        Retrieve pending repair suggestions from database.
        
        Args:
            limit: Maximum number of suggestions to retrieve
            
        Returns:
            List of pending repair suggestions with node info
        """
        try:
            from app.models.base import get_session
            from app.assistant.kg_repair_pipeline.data_models.node_processing_tracking import NodeProcessingStatus
            
            session = get_session()
            
            # Query for pending suggestions
            pending_statuses = session.query(NodeProcessingStatus).filter(
                NodeProcessingStatus.current_stage == 'pending_review',
                NodeProcessingStatus.implementation_status == 'pending',
                NodeProcessingStatus.should_skip_future == False
            ).order_by(
                NodeProcessingStatus.first_identified_at.desc()
            ).limit(limit).all()
            
            suggestions = []
            for status in pending_statuses:
                suggestions.append({
                    'id': str(status.id),
                    'node_id': str(status.node_id),
                    'problem_description': status.problem_description,
                    'problem_category': status.problem_category,
                    'critic_suggestions': status.user_instructions,
                    'implementation_instructions': status.implementation_instructions,
                    'identified_at': status.first_identified_at,
                    'pipeline_id': status.pipeline_id,
                    'notes': status.notes
                })
            
            logger.info(f"📋 Retrieved {len(suggestions)} pending repair suggestions")
            return suggestions
            
        except Exception as e:
            logger.error(f"❌ Failed to retrieve pending suggestions: {e}")
            return []
    
    @staticmethod
    def execute_repair_suggestion(node_id: str, user_instructions: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute a saved repair suggestion.
        
        Args:
            node_id: The node ID to repair
            user_instructions: Optional user-provided instructions (overrides critic suggestions)
            
        Returns:
            Dict containing execution results
        """
        try:
            from app.assistant.kg_repair_pipeline.utils.kg_operations import KGOperations
            
            logger.info(f"🔧 Executing repair for node: {node_id}")
            
            # Get the processing status
            processing_manager = NodeProcessingManager()
            status = processing_manager.get_node_status(node_id)
            
            if not status:
                return {
                    'success': False,
                    'error': f'No processing status found for node {node_id}'
                }
            
            # Get node info from KG
            kg_ops = KGOperations()
            node_info = kg_ops.get_node_info(node_id)
            
            if not node_info:
                return {
                    'success': False,
                    'error': f'Node {node_id} not found in knowledge graph'
                }
            
            # Create problematic node object
            problematic_node = ProblematicNode(
                id=node_id,
                label=node_info['label'],
                type=node_info['node_type'],
                category=node_info.get('category', ''),
                description=node_info['description'],
                start_date=node_info['start_date'],
                end_date=node_info['end_date'],
                start_date_confidence=node_info['start_date_confidence'],
                end_date_confidence=node_info['end_date_confidence'],
                valid_during=node_info['valid_during'],
                node_aliases=node_info['aliases'],
                full_node_info=node_info,
                problem_description=status.problem_description
            )
            
            # Use provided instructions or fall back to critic suggestions
            instructions = user_instructions or status.user_instructions
            
            if not instructions:
                return {
                    'success': False,
                    'error': 'No repair instructions available'
                }
            
            # Create user response object
            user_response_obj = type('UserResponse', (), {
                'node_id': node_id,
                'raw_response': instructions,
                'provided_data': {'instructions': instructions}
            })()
            
            # Execute the repair using implementer
            implementer = KGImplementer()
            result = implementer.implement_with_multi_tools(problematic_node, user_response_obj)
            
            # Update processing status
            if result['success']:
                processing_manager.update_node_status(
                    node_id=node_id,
                    status='completed',
                    implementation_status='completed',
                    implementation_result=result.get('notes', 'Successfully repaired'),
                    notes=f"Repair executed: {result.get('notes', 'Success')}"
                )
                logger.info(f"✅ Successfully repaired node {node_id}")
            else:
                processing_manager.update_node_status(
                    node_id=node_id,
                    status='failed',
                    implementation_status='failed',
                    error_details=result.get('error', 'Unknown error'),
                    notes=f"Repair failed: {result.get('error', 'Unknown error')}"
                )
                logger.error(f"❌ Failed to repair node {node_id}: {result.get('error')}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Error executing repair for node {node_id}: {e}")
            return {
                'success': False,
                'error': str(e)
            }


if __name__ == "__main__":
    from app.assistant.utils.logging_config import get_logger

    logger = get_logger(__name__)

    try:
        logger.info("🚀 Starting KG Repair Pipeline...")
        kg_pipeline_orchestrator = KGPipelineOrchestrator()

        # Run with some configuration
        result = kg_pipeline_orchestrator.run_pipeline(
            kg_info=None,  # Let it discover the KG
            max_nodes=5    # Start small for testing
        )

        logger.info(f"✅ Pipeline completed: {result.current_stage}")
        logger.info(f"📊 Processed {result.total_nodes_identified} nodes")
        logger.info(f"✅ Resolved {result.nodes_resolved} nodes")

    except Exception as e:
        logger.error(f"❌ Pipeline failed: {e}")
        import traceback
        traceback.print_exc()