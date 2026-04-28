"""
KG Operations Utilities

Provides utility functions for knowledge graph operations in the repair pipeline.
"""

from typing import Dict, List, Any, Optional
from app.models.base import get_session
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
from app.assistant.kg_core.kg_utils.kg_tools import inspect_node_neighborhood
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

class KGOperations:
    """
    Utility class for knowledge graph operations.
    """
    
    def __init__(self):
        # Don't store session - get fresh one per operation to avoid concurrency issues
        pass
        
    def get_node_info(self, node_id: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific node.
        
        Args:
            node_id: ID of the node to retrieve
            
        Returns:
            Dict containing node information, or None if not found
        """
        # Get fresh session for this operation
        session = get_session()
        try:
            logger.info(f"🔍 Retrieving info for node {node_id}")
            
            # Query the node from database (SQLite: use string directly)
            node = session.query(Node).filter(Node.id == str(node_id)).first()
            if not node:
                logger.warning(f"⚠️ Node {node_id} not found")
                return None
                
            # Get neighborhood information
            node_info, all_edges = inspect_node_neighborhood(node_id, session)
            
            # Build comprehensive node info
            node_data = {
                "id": str(node.id),
                "label": node.label,
                "semantic_label": node.semantic_label,
                "node_type": node.node_type,
                "description": node.description,
                "aliases": node.aliases or [],
                "category": node.category,
                "attributes": node.attributes or {},
                "start_date": node.start_date.isoformat() if node.start_date else None,
                "end_date": node.end_date.isoformat() if node.end_date else None,
                "start_date_confidence": node.start_date_confidence,
                "end_date_confidence": node.end_date_confidence,
                "valid_during": node.valid_during,
                "confidence": node.confidence,
                "importance": node.importance,
                "source": node.source,
                "edge_count": len(all_edges),
                "neighborhood": node_info,
                "connections": all_edges
            }
            
            logger.info(f"✅ Retrieved info for node {node.label} ({node.node_type})")
            return node_data
            
        except Exception as e:
            logger.error(f"❌ Failed to get node info for {node_id}: {e}")
            return None
    
    def update_node(self, node_id: str, attributes: Dict[str, Any]) -> bool:
        """
        Update a node's attributes.
        
        Args:
            node_id: ID of the node to update
            attributes: Dictionary of attributes to update
            
        Returns:
            True if successful, False otherwise
        """
        # Get fresh session for this operation
        session = get_session()
        try:
            logger.info(f"📝 Updating node {node_id} with attributes: {list(attributes.keys())}")
            
            # Get the node
            node = session.query(Node).filter(Node.id == node_id).first()
            if not node:
                logger.error(f"❌ Node {node_id} not found")
                return False
                
            # Update node attributes based on the attribute keys
            for key, value in attributes.items():
                if hasattr(node, key):
                    setattr(node, key, value)
                    logger.info(f"  ✅ Updated {key}: {value}")
                else:
                    # Store in the flexible attributes JSONB field
                    if not node.attributes:
                        node.attributes = {}
                    node.attributes[key] = value
                    logger.info(f"  ✅ Updated attributes.{key}: {value}")
                    
            # Commit the changes
            session.commit()
            logger.info(f"✅ Successfully updated node {node.label}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to update node {node_id}: {e}")
            session.rollback()
            return False
    
    def create_connections(self, node_id: str, connections: List[Dict[str, Any]]) -> bool:
        """
        Create connections for a node.
        
        Args:
            node_id: ID of the node to connect
            connections: List of connection dictionaries
            
        Returns:
            True if successful, False otherwise
        """
        # Get fresh session for this operation
        session = get_session()
        try:
            logger.info(f"🔗 Creating connections for node {node_id}: {len(connections)} connections")
            
            # Get the source node
            source_node = session.query(Node).filter(Node.id == node_id).first()
            if not source_node:
                logger.error(f"❌ Source node {node_id} not found")
                return False
                
            created_count = 0
            for connection in connections:
                try:
                    # Extract connection details
                    target_id = connection.get('target_id')
                    relationship_type = connection.get('relationship_type', 'related_to')
                    relationship_descriptor = connection.get('relationship_descriptor')
                    sentence = connection.get('sentence', '')
                    confidence = connection.get('confidence')
                    attributes = connection.get('attributes', {})
                    
                    if not target_id:
                        logger.warning(f"⚠️ Skipping connection without target_id: {connection}")
                        continue
                        
                    # Get the target node
                    target_node = session.query(Node).filter(Node.id == target_id).first()
                    if not target_node:
                        logger.warning(f"⚠️ Target node {target_id} not found, skipping connection")
                        continue
                        
                    # Create the edge
                    edge = Edge(
                        source_id=node_id,
                        target_id=target_id,
                        relationship_type=relationship_type,
                        relationship_descriptor=relationship_descriptor,
                        sentence=sentence,
                        confidence=confidence,
                        attributes=attributes
                    )
                    
                    session.add(edge)
                    created_count += 1
                    logger.info(f"  ✅ Created connection: {source_node.label} -> {target_node.label} ({relationship_type})")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to create connection {connection}: {e}")
                    continue
                    
            # Commit all connections
            session.commit()
            logger.info(f"✅ Successfully created {created_count} connections for node {source_node.label}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to create connections for node {node_id}: {e}")
            session.rollback()
            return False
    
    def validate_node_data(self, node_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate data before applying it to a node.
        
        Args:
            node_id: ID of the node
            data: Data to validate
            
        Returns:
            Dict containing validation results
        """
        try:
            validation_results = {
                "is_valid": True,
                "errors": [],
                "warnings": [],
                "validated_data": data
            }
            
            # Validate date fields
            if "start_date" in data:
                if not self._validate_date(data["start_date"]):
                    validation_results["errors"].append("Invalid start_date format")
                    validation_results["is_valid"] = False
                    
            if "end_date" in data:
                if not self._validate_date(data["end_date"]):
                    validation_results["errors"].append("Invalid end_date format")
                    validation_results["is_valid"] = False
                    
            # Validate confidence score
            if "confidence" in data:
                try:
                    confidence = float(data["confidence"])
                    if not 0 <= confidence <= 1:
                        validation_results["errors"].append("Confidence must be between 0 and 1")
                        validation_results["is_valid"] = False
                except (ValueError, TypeError):
                    validation_results["errors"].append("Confidence must be a number")
                    validation_results["is_valid"] = False
                    
            # Validate description
            if "description" in data:
                if not isinstance(data["description"], str) or len(data["description"].strip()) == 0:
                    validation_results["warnings"].append("Description is empty or not a string")
                    
            return validation_results
            
        except Exception as e:
            logger.error(f"❌ Validation failed for node {node_id}: {e}")
            return {
                "is_valid": False,
                "errors": [f"Validation error: {e}"],
                "warnings": [],
                "validated_data": data
            }
    
    def _validate_date(self, date_value: Any) -> bool:
        """
        Validate a date value.
        
        Args:
            date_value: The date value to validate
            
        Returns:
            True if valid, False otherwise
        """
        try:
            if isinstance(date_value, str):
                # Try to parse as ISO date
                from datetime import datetime
                datetime.fromisoformat(date_value.replace('Z', '+00:00'))
                return True
            elif hasattr(date_value, 'isoformat'):
                # Already a datetime object
                return True
            else:
                return False
        except (ValueError, AttributeError):
            return False
    
    def get_kg_statistics(self) -> Dict[str, Any]:
        """
        Get statistics about the knowledge graph.

        Returns:
            Dict containing KG statistics
        """
        session = get_session()
        try:
            from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
            node_count = session.query(Node).count()
            edge_count = session.query(Edge).count()
            from sqlalchemy import func
            node_types = dict(
                session.query(Node.node_type, func.count(Node.id))
                .group_by(Node.node_type).all()
            )
            return {
                "total_nodes": node_count,
                "total_edges": edge_count,
                "node_types": node_types,
                "edge_types": {},
                "last_updated": None
            }
        except Exception as e:
            logger.error(f"Failed to get KG statistics: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def backup_kg_state(self, backup_name: str) -> bool:
        """
        Create a backup of the current KG state.
        
        Args:
            backup_name: Name for the backup
            
        Returns:
            True if successful, False otherwise
        """
        raise NotImplementedError(
            "backup_kg_state is not yet implemented. "
            "Back up the SQLite database file directly for now."
        )
    
    def restore_kg_state(self, backup_name: str) -> bool:
        """
        Restore KG state from a backup.
        
        Args:
            backup_name: Name of the backup to restore
            
        Returns:
            True if successful, False otherwise
        """
        raise NotImplementedError(
            "restore_kg_state is not yet implemented. "
            "Restore the SQLite database file directly for now."
        )
