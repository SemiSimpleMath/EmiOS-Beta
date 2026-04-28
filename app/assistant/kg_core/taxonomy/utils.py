"""
Taxonomy Utility Functions

Core functions for manipulating taxonomy structure. These are used by both
the web UI and agent tools.
"""

from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from app.assistant.kg_core.taxonomy.models import Taxonomy, NodeTaxonomyLink
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


def rename_category(session: Session, category_id: int, new_label: str) -> Dict[str, Any]:
    """
    Rename a taxonomy category.
    
    Args:
        session: Database session
        category_id: ID of the category to rename
        new_label: New label for the category
        
    Returns:
        Dict with success status and message
    """
    try:
        node = session.query(Taxonomy).filter(Taxonomy.id == category_id).first()
        
        if not node:
            return {"success": False, "message": f"Category {category_id} not found"}
        
        old_label = node.label
        
        # Normalize label (lowercase, underscores)
        normalized_label = new_label.strip().lower().replace(' ', '_')
        
        node.label = normalized_label
        session.commit()
        
        logger.info(f"Renamed taxonomy {category_id} from '{old_label}' to '{normalized_label}'")
        
        return {
            "success": True,
            "message": f"Renamed '{old_label}' to '{normalized_label}'",
            "category_id": category_id,
            "old_label": old_label,
            "new_label": normalized_label
        }
        
    except Exception as e:
        session.rollback()
        logger.error(f"Error renaming category {category_id}: {e}")
        return {
            "success": False,
            "message": f"Error renaming category: {str(e)}"
        }


def update_description(session: Session, category_id: int, new_description: str) -> Dict[str, Any]:
    """
    Update the description of a taxonomy category.
    
    Args:
        session: Database session
        category_id: ID of the category to update
        new_description: New description text (empty string to clear)
        
    Returns:
        Dict with success status and message
    """
    try:
        node = session.query(Taxonomy).filter(Taxonomy.id == category_id).first()
        
        if not node:
            return {"success": False, "message": f"Category {category_id} not found"}
        
        old_description = node.description
        
        # Empty string clears the description
        node.description = new_description.strip() if new_description.strip() else None
        session.commit()
        
        logger.info(f"Updated description for taxonomy {category_id} ({node.label})")
        
        return {
            "success": True,
            "message": f"Updated description for '{node.label}'",
            "category_id": category_id,
            "label": node.label,
            "old_description": old_description,
            "new_description": node.description
        }
        
    except Exception as e:
        session.rollback()
        logger.error(f"Error updating description for category {category_id}: {e}")
        return {
            "success": False,
            "message": f"Error updating description: {str(e)}"
        }


def move_category(session: Session, category_id: int, new_parent_id: Optional[int]) -> Dict[str, Any]:
    """
    Move a category to a new parent (change its position in the hierarchy).
    
    Args:
        session: Database session
        category_id: ID of the category to move
        new_parent_id: ID of the new parent (None for root level)
        
    Returns:
        Dict with success status and message
    """
    try:
        node = session.query(Taxonomy).filter(Taxonomy.id == category_id).first()
        
        if not node:
            return {"success": False, "message": f"Category {category_id} not found"}
        
        # Validate new parent exists (if not None)
        if new_parent_id is not None:
            new_parent = session.query(Taxonomy).filter(Taxonomy.id == new_parent_id).first()
            
            if not new_parent:
                return {"success": False, "message": f"New parent category {new_parent_id} not found"}
            
            # Check for circular reference (new parent can't be a descendant)
            current = new_parent
            while current.parent_id:
                if current.parent_id == category_id:
                    return {
                        "success": False,
                        "message": "Cannot move a category to be a child of its own descendant"
                    }
                current = session.query(Taxonomy).filter(Taxonomy.id == current.parent_id).first()
                if not current:
                    break
        
        old_parent_id = node.parent_id
        node.parent_id = new_parent_id
        session.commit()
        
        # Build message
        if new_parent_id is None:
            message = f"Moved '{node.label}' to root level"
        else:
            new_parent = session.query(Taxonomy).filter(Taxonomy.id == new_parent_id).first()
            message = f"Moved '{node.label}' under '{new_parent.label}'"
        
        logger.info(f"Moved taxonomy {category_id} ({node.label}) from parent {old_parent_id} to {new_parent_id}")
        
        return {
            "success": True,
            "message": message,
            "category_id": category_id,
            "label": node.label,
            "old_parent_id": old_parent_id,
            "new_parent_id": new_parent_id
        }
        
    except Exception as e:
        session.rollback()
        logger.error(f"Error moving category {category_id}: {e}")
        return {
            "success": False,
            "message": f"Error moving category: {str(e)}"
        }


def merge_categories(session: Session, source_id: int, destination_id: int) -> Dict[str, Any]:
    """
    Merge one category into another.
    
    This moves all children and all node classifications from source to destination,
    then deletes the source category.
    
    Args:
        session: Database session
        source_id: ID of the category to merge (will be deleted)
        destination_id: ID of the category to merge into (will be kept)
        
    Returns:
        Dict with success status and message
    """
    try:
        source = session.query(Taxonomy).filter(Taxonomy.id == source_id).first()
        destination = session.query(Taxonomy).filter(Taxonomy.id == destination_id).first()
        
        if not source:
            return {"success": False, "message": f"Source category {source_id} not found"}
        
        if not destination:
            return {"success": False, "message": f"Destination category {destination_id} not found"}
        
        if source_id == destination_id:
            return {"success": False, "message": "Cannot merge a category into itself"}
        
        source_label = source.label
        dest_label = destination.label
        
        # Step 1: Move all children from source to destination
        children = session.query(Taxonomy).filter(Taxonomy.parent_id == source_id).all()
        children_count = len(children)
        
        for child in children:
            child.parent_id = destination_id
        
        logger.info(f"Moved {children_count} children from '{source_label}' to '{dest_label}'")
        
        # Step 2: Move all node classifications from source to destination
        # Get all node_taxonomy_links pointing to source
        links = session.query(NodeTaxonomyLink).filter(
            NodeTaxonomyLink.taxonomy_id == source_id
        ).all()
        
        links_count = len(links)
        
        for link in links:
            # Check if node already has a link to destination
            existing_link = session.query(NodeTaxonomyLink).filter(
                NodeTaxonomyLink.node_id == link.node_id,
                NodeTaxonomyLink.taxonomy_id == destination_id
            ).first()
            
            if existing_link:
                # Node already linked to destination, just delete source link
                session.delete(link)
            else:
                # Update link to point to destination
                link.taxonomy_id = destination_id
        
        logger.info(f"Moved {links_count} node classifications from '{source_label}' to '{dest_label}'")
        
        # Flush changes to ensure all links are updated before deleting source
        session.flush()
        
        # Step 3: Delete the source category
        session.delete(source)
        session.commit()
        
        logger.info(f"Merged taxonomy {source_id} ('{source_label}') into {destination_id} ('{dest_label}')")
        
        return {
            "success": True,
            "message": f"Merged '{source_label}' into '{dest_label}'",
            "source_id": source_id,
            "source_label": source_label,
            "destination_id": destination_id,
            "destination_label": dest_label,
            "children_moved": children_count,
            "classifications_moved": links_count
        }
        
    except Exception as e:
        session.rollback()
        logger.error(f"Error merging category {source_id} into {destination_id}: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "message": f"Error merging categories: {str(e)}"
        }


def create_category(session: Session, parent_id: int, new_label: str, description: str = "") -> Dict[str, Any]:
    """
    Create a new taxonomy category under a parent.
    
    Args:
        session: Database session
        parent_id: ID of the parent category
        new_label: Label for the new category
        description: Description for the new category (optional)
        
    Returns:
        Dict with success status, message, and new category_id
    """
    try:
        # Validate parent exists
        parent = session.query(Taxonomy).filter(Taxonomy.id == parent_id).first()
        
        if not parent:
            return {"success": False, "message": f"Parent category {parent_id} not found"}
        
        # Normalize label (lowercase, underscores)
        normalized_label = new_label.strip().lower().replace(' ', '_')
        
        # Check if category already exists under this parent
        existing = session.query(Taxonomy).filter(
            Taxonomy.parent_id == parent_id,
            Taxonomy.label == normalized_label
        ).first()
        
        if existing:
            return {
                "success": False,
                "message": f"Category '{normalized_label}' already exists under '{parent.label}' (ID: {existing.id})",
                "existing_category_id": existing.id
            }
        
        # Create new category
        new_category = Taxonomy(
            label=normalized_label,
            description=description.strip() if description.strip() else None,
            parent_id=parent_id
        )
        
        session.add(new_category)
        session.commit()
        
        logger.info(f"Created new taxonomy '{normalized_label}' (ID: {new_category.id}) under '{parent.label}' (ID: {parent_id})")
        
        return {
            "success": True,
            "message": f"Created '{normalized_label}' under '{parent.label}'",
            "category_id": new_category.id,
            "label": normalized_label,
            "parent_id": parent_id,
            "parent_label": parent.label,
            "description": new_category.description
        }
        
    except Exception as e:
        session.rollback()
        logger.error(f"Error creating category under {parent_id}: {e}")
        return {
            "success": False,
            "message": f"Error creating category: {str(e)}"
        }


def get_category_info(session: Session, category_id: int) -> Dict[str, Any]:
    """
    Get information about a taxonomy category.
    
    Args:
        session: Database session
        category_id: ID of the category
        
    Returns:
        Dict with category information or error
    """
    try:
        node = session.query(Taxonomy).filter(Taxonomy.id == category_id).first()
        
        if not node:
            return {"success": False, "message": f"Category {category_id} not found"}
        
        # Get parent info
        parent_label = None
        if node.parent_id:
            parent = session.query(Taxonomy).filter(Taxonomy.id == node.parent_id).first()
            if parent:
                parent_label = parent.label
        
        # Count children
        children_count = session.query(Taxonomy).filter(Taxonomy.parent_id == category_id).count()
        
        # Count node classifications
        classifications_count = session.query(NodeTaxonomyLink).filter(
            NodeTaxonomyLink.taxonomy_id == category_id
        ).count()
        
        return {
            "success": True,
            "category_id": category_id,
            "label": node.label,
            "description": node.description,
            "parent_id": node.parent_id,
            "parent_label": parent_label,
            "children_count": children_count,
            "classifications_count": classifications_count
        }
        
    except Exception as e:
        logger.error(f"Error getting category info for {category_id}: {e}")
        return {
            "success": False,
            "message": f"Error getting category info: {str(e)}"
        }


def get_taxonomy_by_path(session: Session, path: str) -> Optional[Taxonomy]:
    """
    Find a taxonomy category by its full path (e.g., 'entity > person' or 'state > educational_status').
    
    Args:
        session: Database session
        path: Full path to the taxonomy category (e.g., 'entity > person')
        
    Returns:
        Taxonomy object if found, None otherwise
    """
    try:
        # Split path by ' > ' to get individual labels
        path_parts = [part.strip() for part in path.split(' > ')]
        
        if not path_parts:
            return None
            
        # Find the root category (first part of path)
        root_label = path_parts[0]
        root_category = session.query(Taxonomy).filter(
            Taxonomy.label == root_label,
            Taxonomy.parent_id == None
        ).first()
        
        if not root_category:
            return None
            
        # If only one part, return the root
        if len(path_parts) == 1:
            return root_category
            
        # Navigate down the hierarchy
        current_category = root_category
        for label in path_parts[1:]:
            child_category = session.query(Taxonomy).filter(
                Taxonomy.label == label,
                Taxonomy.parent_id == current_category.id
            ).first()
            
            if not child_category:
                return None
                
            current_category = child_category
            
        return current_category
        
    except Exception as e:
        return None

